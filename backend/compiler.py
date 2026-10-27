"""조회 명세(AST) → SQL. 조합하고 검증만 한다 — 무엇을 어떻게 계산할지는 LLM 과 지표가 정한다.

예외는 하나: UTC 로 저장된 시각 컬럼(utc_offset)은 현지 시각으로 자동 보정한다. AST 의 시각은 모두 현지 시각이다.
  - 컬럼을 쓰면 (컬럼 + 시차) 로 바꾼다 — 날짜 묶음·표시가 현지 기준이 된다.
  - 컬럼을 상수와 비교하면 반대로 상수를 UTC 로 바꾼다 — 컬럼을 감싸지 않아야 인덱스를 탄다.

AST 모양
  {"with": [{"name": "이름", "query": AST}],   맨 바깥에만. 정의한 이름은 from · joins 에서 테이블처럼 쓴다
   "from": "테이블" | {"table": "테이블", "as": "별칭"} | {"query": AST, "as": "별칭"},
   "joins": [{"table": "테이블" | "query": AST | "tree": 계층, "as": "별칭", "type": "inner|left", "on": [["a.컬럼", "b.컬럼"]]}],
   "select": [{"expr": 식, "as": "이름"}],        {"expr": {"col": "*"}} 는 모든 컬럼
   "distinct": true, "where": [조건], "group_by": [식], "having": [조건],
   "order_by": [{"expr": 식, "dir": "asc|desc"}], "limit": 숫자}
  식: {"col": "컬럼|테이블.컬럼"} | {"num": 1} | {"str": "문자"} | {"fn": "함수", "args": [식]}
  조건: {"expr": 식, "op": "=", "value": 값 또는 식} | {"or": [조건]} | {"and": [조건]}  — 목록은 AND
        in · not_in 의 value 는 {"query": AST} (select 하나) 도 된다
  계층: {"table": "테이블", "key": "id", "parent": "parent_id", "start": [조건], "direction": "down|up"}
        시작 행에서 자식(down) 또는 부모(up) 쪽으로 따라간 행 전부 + depth(시작 = 0). from 이나 joins 에 {"tree": 계층, "as": "별칭"}.
"""

import datetime
import json

AGGREGATES = {"SUM", "AVG", "MIN", "MAX", "COUNT", "COUNT_DISTINCT"}
ARITHMETIC = {"ADD": "+", "SUB": "-", "MUL": "*"}
SCALARS = {  # 이름: (최소, 최대 인자 수)
    "ABS": (1, 1),
    "ROUND": (1, 2),
    "COALESCE": (2, 8),
    "NULLIF": (2, 2),
    "GREATEST": (2, 8),
    "LEAST": (2, 8),
}
OTHER_FUNCTIONS = {"DIV", "DATE", "DATE_TRUNC", "ADD_HOURS", "ADD_DAYS", "ADD_MINUTES", "ADD_SECONDS", "NOW", "CURRENT_DATE", "ROW_NUMBER", "CASE"}
INTERVAL_FUNCTIONS = {"ADD_SECONDS": "seconds", "ADD_MINUTES": "minutes", "ADD_HOURS": "hours", "ADD_DAYS": "days"}
TRUNC_UNITS = ("hour", "day", "week", "month", "year")
_ORACLE_TRUNC = {"hour": "HH24", "day": "DD", "week": "IW", "month": "MM", "year": "YYYY"}
COMPARE = {"=": "=", "!=": "<>", "<>": "<>", ">": ">", ">=": ">=", "<": "<", "<=": "<="}
CONDITION_OPS = tuple(COMPARE) + ("in", "not_in", "like", "not_like", "between", "is_null", "is_not_null")
QUERY_KEYS = {"from", "joins", "select", "distinct", "where", "group_by", "having", "order_by", "limit"}
MAX_LIMIT = 10000
MAX_DEPTH = 6  # 하위 쿼리 깊이. 등록 SQL 의 WITH 를 펼치면 IN (하위 쿼리) 와 겹쳐 깊어진다
MAX_TREE_DEPTH = 20  # 계층을 따라가는 최대 단계. 부모가 자기 자신이거나 고리가 있어도 여기서 끝난다
TREE_KEYS = {"table", "key", "parent", "start", "direction"}


def temporal_kind(data_type: str, driver: str) -> str | None:
    """컬럼이 날짜형인지, 시각을 포함하는지 판정한다.

    'datetime' = 시·분·초를 포함, 'date' = 날짜뿐, None = 날짜 컬럼이 아님.
    Oracle 의 DATE 는 시각을 포함한다 — 다른 두 드라이버의 date 와 다르다.
    """
    text = (data_type or "").strip().lower()
    if not text:
        return None
    if text.startswith(("timestamp", "datetime")):
        return "datetime"
    if text.split("(")[0].strip() == "date":
        return "datetime" if driver == "oracle" else "date"
    return None


def type_family(data_type: str) -> str:
    """타입 갈래 (number | text | time | 그 밖은 타입 이름). 갈래가 다른 컬럼끼리 비교하면 문자로 바꿔 비교한다."""
    t = str(data_type or "").lower()
    for word in ("int", "num", "dec", "float", "double", "real", "serial", "money"):
        if word in t:
            return "number"
    for word in ("char", "text", "string", "clob"):
        if word in t:
            return "text"
    for word in ("timestamp", "date", "time"):
        if word in t:
            return "time"
    return t.split("(")[0].strip()


def parse_datetime(value) -> datetime.datetime | None:
    """'YYYY-MM-DD' · 'YYYY-MM-DD HH:MM[:SS]' 이면 시각으로, 아니면 None"""
    text = str(value or "").strip().replace("T", " ")
    if len(text) < 10:
        return None
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


class AstError(ValueError):
    pass


class Dialect:
    """드라이버마다 다른 문법"""

    def __init__(self, driver: str):
        self.driver = driver or "postgresql"
        self.quote_char = "`" if self.driver == "mysql" else '"'

    def q(self, name: str) -> str:
        text = str(name)
        if self.quote_char in text or "\x00" in text:
            raise AstError(f"이름에 {self.quote_char} 나 NUL 을 쓸 수 없습니다 -> {name!r}")
        return f"{self.quote_char}{text}{self.quote_char}"

    def table(self, name: str, schema: str = "") -> str:
        """테이블 이름. 기본 스키마가 아닌 테이블(이름이 '스키마.테이블')은 스키마와 테이블을 따로 감싼다."""
        if schema and name.startswith(schema + "."):
            return f"{self.q(schema)}.{self.q(name[len(schema) + 1:])}"
        return self.q(name)

    def literal(self, value) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float)):
            return repr(value)
        text = str(value).replace("'", "''")
        if self.driver == "mysql":
            text = text.replace("\\", "\\\\")
        return f"'{text}'"

    def add_interval(self, sql: str, n: float, unit: str) -> str:
        """unit: seconds | minutes | hours | days"""
        sign, n = ("+", n) if n >= 0 else ("-", -n)
        amount = f"{n:g}"
        word = {"seconds": "SECOND", "minutes": "MINUTE", "hours": "HOUR", "days": "DAY"}[unit]
        if self.driver == "mysql":
            interval = f"INTERVAL {amount} {word}"
        elif self.driver == "oracle":
            interval = f"INTERVAL '{amount}' {word}"
        else:
            interval = f"INTERVAL '{amount} {unit}'"
        return f"({sql} {sign} {interval})"

    def timestamp(self, moment: datetime.datetime) -> str:
        return f"TIMESTAMP '{moment:%Y-%m-%d %H:%M:%S}'"

    def date(self, sql: str) -> str:
        if self.driver == "mysql":
            return f"DATE({sql})"
        if self.driver == "oracle":
            return f"TRUNC({sql})"
        return f"CAST({sql} AS DATE)"

    def trunc(self, unit: str, sql: str) -> str:
        if self.driver == "oracle":
            return f"TRUNC({sql}, '{_ORACLE_TRUNC[unit]}')"
        if self.driver == "mysql":
            return {
                "hour": f"DATE_FORMAT({sql}, '%Y-%m-%d %H:00:00')",
                "day": f"DATE({sql})",
                "week": f"DATE_SUB(DATE({sql}), INTERVAL WEEKDAY({sql}) DAY)",
                "month": f"DATE_FORMAT({sql}, '%Y-%m-01')",
                "year": f"DATE_FORMAT({sql}, '%Y-01-01')",
            }[unit]
        return f"DATE_TRUNC('{unit}', {sql})"

    def current_date(self) -> str:
        return "TRUNC(SYSDATE)" if self.driver == "oracle" else "CURRENT_DATE"

    def limit(self, n: int) -> str:
        return f"FETCH FIRST {n} ROWS ONLY" if self.driver == "oracle" else f"LIMIT {n}"

    def text(self, sql: str) -> str:
        """값을 문자로 (타입 갈래가 다른 컬럼끼리 비교할 때)"""
        if self.driver == "oracle":
            return f"TO_CHAR({sql})"
        return f"CAST({sql} AS {'CHAR' if self.driver == 'mysql' else 'VARCHAR'})"

    def recursive(self) -> str:
        """재귀 WITH 머리. Oracle 은 RECURSIVE 를 쓰지 않는다"""
        return "WITH" if self.driver == "oracle" else "WITH RECURSIVE"


class Scope:
    """FROM 절이 보이는 이름들. {별칭: {컬럼: UTC 시차(없으면 0)}}"""

    def __init__(self, dialect: Dialect):
        self.dialect = dialect
        self.sources: dict[str, dict[str, int]] = {}
        self.real: dict[str, str] = {}  # 별칭 → 실제 테이블 (하위 쿼리는 없다)
        self.families: dict[str, dict[str, str]] = {}  # 별칭 → {컬럼: 타입 갈래} (모르면 비어 있다)

    def add(self, alias: str, columns: dict, families: dict | None = None):
        if alias in self.sources:
            raise AstError(f"FROM 의 테이블·별칭이 겹칩니다 -> {alias}")
        self.sources[alias] = dict(columns)
        self.families[alias] = dict(families or {})

    def resolve(self, ref) -> tuple[str, str, int]:
        """→ (별칭, 컬럼, 시차)"""
        # on 에 {"col": "a.컬럼"}, {"expr": {"col": "a.컬럼"}} 처럼 식으로 감싸 적어도 받는다
        while isinstance(ref, dict) and len(ref) == 1 and ("col" in ref or "expr" in ref):
            ref = ref.get("col", ref.get("expr"))
        if not isinstance(ref, str):
            raise AstError(f"컬럼은 '테이블.컬럼' 문자열로 적습니다 -> {json.dumps(ref, ensure_ascii=False)}")
        ref = ref.strip()
        if "." in ref:
            # 별칭에도 점이 있을 수 있다 ('스키마.테이블') — 가장 길게 맞는 별칭을 고른다
            alias = ref.split(".", 1)[0]
            for name in self.sources:
                if ref.startswith(name + ".") and len(name) > len(alias):
                    alias = name
            if alias not in self.sources:
                # 별칭을 붙인 테이블을 실제 이름으로 가리켰다 — 그 테이블의 별칭이 하나뿐이면 그 별칭으로 본다
                table = ""
                for real in self.real.values():
                    if ref.startswith(real + ".") and len(real) > len(table):
                        table = real
                aliases = []
                for name, real in self.real.items():
                    if table and real == table:
                        aliases.append(name)
                if len(aliases) == 1:
                    return self.resolve(f"{aliases[0]}.{ref[len(table) + 1:]}")
                known = []
                for name in self.sources:
                    known.append(name if self.real.get(name, name) == name else f"{name}({self.real[name]})")
                raise AstError(f"FROM 에 없는 테이블입니다 -> {alias} (있는 것: {', '.join(known)})")
            col = ref[len(alias) + 1:]
            if col not in self.sources[alias]:
                raise AstError(f"{alias} 에 없는 컬럼입니다 -> {col}")
            return alias, col, self.sources[alias][col]
        owners = []
        for alias, cols in self.sources.items():
            if ref in cols:
                owners.append(alias)
        if not owners:
            known = sorted(set().union(*self.sources.values())) if self.sources else []
            raise AstError(f"없는 컬럼입니다 -> {ref} (쓸 수 있는 것: {', '.join(known)})")
        if len(owners) > 1:
            raise AstError(f"여러 테이블에 있는 컬럼입니다 -> {ref} ('테이블.컬럼' 으로 적으십시오)")
        return owners[0], ref, self.sources[owners[0]][ref]

    def raw(self, alias: str, col: str) -> str:
        q = self.dialect.q
        return q(col) if len(self.sources) == 1 else f"{q(alias)}.{q(col)}"


def _info(agg=False, window=False, cols=False, raw=(), calc=()) -> dict:
    """식의 성질. raw: 집계하지 않은 [계산 필요] 컬럼, calc: 집계한 [계산 필요] 컬럼 — (테이블, 컬럼) 목록"""
    return {"agg": agg, "window": window, "cols": cols, "raw": list(raw), "calc": list(calc)}


def _merge(parts) -> dict:
    merged = _info()
    for _, info in parts:
        for key in ("agg", "window", "cols"):
            if info[key]:
                merged[key] = True
        for key in ("raw", "calc"):
            for ref in info[key]:
                if ref not in merged[key]:
                    merged[key].append(ref)
    return merged


class CalcColumnError(AstError):
    """[계산 필요] 컬럼을 지표에 없는 방식으로 계산했다. table, column: 그 컬럼"""

    def __init__(self, message: str, table: str, column: str):
        super().__init__(message)
        self.table = table
        self.column = column


def _metric_aggregates(node, out: dict):
    """지표 AST 에서 컬럼마다 쓴 집계 함수. {컬럼: {"MAX", "MIN"}} ('테이블.' 은 뺀다)

    식을 그대로 비교하지 않는다 — a - b - c 와 (a - b) - c 처럼 같은 계산이 여러 모양으로 적힌다.
    """
    if isinstance(node, list):
        for x in node:
            _metric_aggregates(x, out)
    elif isinstance(node, dict):
        fn = str(node.get("fn") or "").upper()
        args = node.get("args")
        if fn in AGGREGATES and isinstance(args, list) and len(args) == 1 and isinstance(args[0], dict):
            col = args[0].get("col")
            if isinstance(col, str):
                out.setdefault(col.split(".")[-1], set()).add(fn)
        for value in node.values():
            _metric_aggregates(value, out)


def _sqls(parts) -> list[str]:
    """(SQL, info) 목록에서 SQL 만"""
    out = []
    for sql, _ in parts:
        out.append(sql)
    return out


class Compiler:
    def __init__(self, contract: dict, metrics: list | None = None, utc_now: datetime.datetime | None = None):
        """metrics 를 주면 [계산 필요] 컬럼의 계산을 그 지표들의 식으로만 허용한다 (질문 처리).
        주지 않으면 검사하지 않는다 (지표 등록 — 지표가 곧 그 계산이다).
        utc_now 는 UTC 컬럼과 비교한 CURRENT_DATE 를 오늘 날짜로 바꿀 때의 기준 (주지 않으면 지금)."""
        self.utc_now = utc_now
        self.dialect = Dialect(contract.get("driver") or "postgresql")
        self.tables: dict[str, dict[str, int]] = {}
        self.schemas: dict[str, str] = {}  # 기본 스키마가 아닌 테이블 → 스키마
        self.families: dict[str, dict[str, str]] = {}  # 테이블 → {컬럼: 타입 갈래}
        self.calc_columns: dict[str, set] = {}
        self.trees = 0  # 계층 CTE 이름을 겹치지 않게
        self.level = 0  # 지금 컴파일하는 쿼리의 깊이 (조건 안 하위 쿼리의 깊이를 센다)
        self.ctes: dict[str, tuple[dict, dict]] = {}  # with 이름 → ({컬럼: 0}, {컬럼: 타입 갈래})
        self.output_families: dict[str, str | None] = {}  # 마지막으로 컴파일한 쿼리의 {select 이름: 타입 갈래}
        for t in contract.get("tables", []):
            cols, calc, families = {}, set(), {}
            for c in t.get("columns", []):
                families[c["name"]] = type_family(c.get("type", ""))
                hours = c.get("utc_offset") or 0
                if hours and temporal_kind(c.get("type", ""), self.dialect.driver) != "datetime":
                    hours = 0
                cols[c["name"]] = int(hours)
                if c.get("needs_calc"):
                    calc.add(c["name"])
            self.tables[t["name"]] = cols
            self.schemas[t["name"]] = t.get("schema") or ""
            self.families[t["name"]] = families
            self.calc_columns[t["name"]] = calc
        self.allowed: dict | None = None  # {컬럼: 지표가 그 컬럼에 쓴 집계 함수}
        if metrics is not None:
            self.allowed = {}
            for m in metrics:
                _metric_aggregates(m.get("ast"), self.allowed)

    def compile(self, ast: dict) -> str:
        self.ctes = {}
        if not isinstance(ast, dict) or "with" not in ast:
            sql, _ = self._query(ast, 0)
            return sql
        # with: 한 번 정의한 하위 쿼리를 여러 곳에서 쓴다 (예: 일별 집계를 상위 N 고르기와 상세에 함께)
        items = ast["with"]
        if not isinstance(items, list) or not items:
            raise AstError('with 는 [{"name": "이름", "query": AST}] 목록입니다')
        d, parts = self.dialect, []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
                raise AstError('with 항목은 {"name": "이름", "query": AST} 입니다')
            name = item["name"].strip()
            if name in self.tables or name in self.ctes:
                raise AstError(f"with 이름이 테이블이나 앞의 with 와 겹칩니다 -> {name}")
            inner_sql, names = self._query(item.get("query"), 1)
            self.ctes[name] = (dict.fromkeys(names, 0), dict(self.output_families))
            parts.append(f"{d.q(name)} AS ({inner_sql})")
        body = {}
        for key, value in ast.items():
            if key != "with":
                body[key] = value
        sql, _ = self._query(body, 0)
        return f"WITH {', '.join(parts)} {sql}"

    # ── 쿼리 한 단계. from 에 AST 를 넣으면 하위 쿼리가 된다 ──────────────

    def _query(self, ast, depth: int) -> tuple[str, list[str]]:
        if not isinstance(ast, dict):
            raise AstError("AST 는 JSON 객체입니다")
        if depth > MAX_DEPTH:
            raise AstError(f"하위 쿼리는 {MAX_DEPTH}단계까지입니다")
        unknown = set(ast) - QUERY_KEYS
        if unknown:
            raise AstError(f"모르는 키입니다 -> {', '.join(sorted(unknown))} (쓸 수 있는 키: {', '.join(sorted(QUERY_KEYS))})")
        d = self.dialect
        scope = Scope(d)
        outer_level, self.level = self.level, depth

        source = ast.get("from")
        if isinstance(source, str):
            from_sql = self._table(source, scope)
        elif isinstance(source, dict) and "query" in source:
            from_sql = self._subquery(source["query"], str(source.get("as") or "sub").strip(), scope, depth)
        elif isinstance(source, dict) and "tree" in source:
            from_sql = self._tree(source["tree"], source.get("as"), scope)
        elif isinstance(source, dict) and "table" in source:
            from_sql = self._table(source["table"], scope, source.get("as"))
        else:
            raise AstError(
                'from 은 테이블 이름이나 {"table": "테이블", "as": "별칭"}, {"query": AST, "as": "별칭"}, '
                '{"tree": 계층, "as": "별칭"} 입니다'
            )

        for join in ast.get("joins") or []:
            if not isinstance(join, dict):
                raise AstError('joins 항목은 {"table", "type", "on"} 또는 {"query", "as", "type", "on"} 입니다')
            kind = {"inner": "JOIN", "left": "LEFT JOIN"}.get(str(join.get("type", "inner")).lower())
            if kind is None:
                raise AstError("join type 은 inner 나 left 입니다")
            # 하위 쿼리 조인 (예: 합계 상위 5대만 남기기). {"table": {"query", "as"}} 로 적어도 받는다
            target = join.get("table") if isinstance(join.get("table"), dict) else join
            if "query" in target:
                alias = str(target.get("as") or "").strip()
                if not alias:
                    raise AstError('하위 쿼리 조인에는 "as"(별칭)가 있어야 합니다')
                table_sql = self._subquery(target["query"], alias, scope, depth)
                label = alias
            elif "tree" in target:
                table_sql = self._tree(target["tree"], target.get("as"), scope)
                label = target.get("as") or "tree"
            else:
                table_sql = self._table(join.get("table"), scope, join.get("as"))
                label = join.get("table")
            pairs = join.get("on") or []
            if not pairs:
                raise AstError(f"{label} 조인에 on 이 없습니다")
            on = []
            for pair in pairs:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    raise AstError('on 은 [["a.컬럼", "b.컬럼"]] 모양입니다')
                sides, families = [], []
                for ref in pair:
                    alias, col, _ = scope.resolve(ref)
                    sides.append(scope.raw(alias, col))
                    families.append(scope.families.get(alias, {}).get(col))
                if families[0] and families[1] and families[0] != families[1]:
                    # 예: 정수 id 와 문자 parent_id — PostgreSQL 은 그대로 비교하면 오류다
                    sides = [d.text(sides[0]), d.text(sides[1])]
                on.append(f"{sides[0]} = {sides[1]}")
            from_sql += f" {kind} {table_sql} ON {' AND '.join(on)}"

        select = ast.get("select") or []
        if not isinstance(select, list) or not select:
            raise AstError("select 가 비었습니다")
        names, items, has_agg = [], [], False
        families: dict[str, str | None] = {}
        for item in select:
            if not isinstance(item, dict) or "expr" not in item:
                raise AstError('select 항목은 {"expr": 식, "as": "이름"} 입니다')
            if item["expr"] == {"col": "*"}:
                for alias, cols in scope.sources.items():
                    for col in cols:
                        if col in names:
                            raise AstError(f"* 로 펼친 컬럼 이름이 겹칩니다 -> {col} (컬럼을 하나씩 적으십시오)")
                        names.append(col)
                        families[col] = scope.families.get(alias, {}).get(col)
                        items.append((self._column(alias, col, scope), _info(cols=True), col))
                continue
            name = str(item.get("as") or "").strip()
            if not name:
                raise AstError(f"select 항목에 as(결과 이름)가 없습니다 -> {item['expr']!r}")
            if name in names:
                raise AstError(f"select 이름이 겹칩니다 -> {name}")
            names.append(name)
            families[name] = self._family_of(item["expr"], scope)
            sql, info = self._expr(item["expr"], scope)
            has_agg |= info["agg"]
            items.append((sql, info, name))

        group_sql = []
        for e in ast.get("group_by") or []:
            sql, info = self._expr(e, scope)
            if info["agg"] or info["window"]:
                raise AstError("group_by 에 집계·윈도 함수를 쓸 수 없습니다")
            group_sql.append(sql)
        if has_agg or group_sql:
            for sql, info, name in items:
                if not info["agg"] and not info["window"] and info["cols"] and sql not in group_sql:
                    raise AstError(f"집계하지 않은 select 항목은 group_by 에 같은 식으로 있어야 합니다 -> {name}")

        head = "SELECT DISTINCT " if ast.get("distinct") else "SELECT "
        columns = []
        for sql, _, name in items:
            columns.append(f"{sql} AS {d.q(name)}")
        parts = [head + ", ".join(columns), "FROM " + from_sql]
        where = self._conditions(ast.get("where"), scope, allow_agg=False)
        if where:
            parts.append("WHERE " + where)
        if group_sql:
            parts.append("GROUP BY " + ", ".join(group_sql))
        having = self._conditions(ast.get("having"), scope, allow_agg=True)
        if having:
            if not (has_agg or group_sql):
                raise AstError("having 은 집계할 때만 씁니다")
            parts.append("HAVING " + having)
        order = self._order(ast.get("order_by"), scope, names)
        if order:
            parts.append("ORDER BY " + order)
        if ast.get("limit") is not None:
            limit = ast["limit"]
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
                raise AstError(f"limit 은 1~{MAX_LIMIT} 정수입니다")
            parts.append(d.limit(limit))
        self.level = outer_level
        self.output_families = families
        return " ".join(parts), names

    def _subquery(self, ast, alias: str, scope: Scope, depth: int) -> str:
        """하위 쿼리 하나 (from 이나 joins). 바깥에서는 하위 쿼리 select 의 이름을 컬럼으로 쓴다."""
        inner_sql, names = self._query(ast, depth + 1)
        scope.add(alias, dict.fromkeys(names, 0), self.output_families)
        return f"({inner_sql}) {self.dialect.q(alias)}"

    def _table(self, name, scope: Scope, alias=None) -> str:
        """테이블 하나. 별칭(as)이 없으면 테이블 이름이 곧 별칭이라 컬럼을 '테이블.컬럼' 으로 가리킨다."""
        if isinstance(name, str) and name in self.ctes:  # with 로 정의한 이름
            columns, families = self.ctes[name]
            alias = str(alias or "").strip() or name
            scope.add(alias, columns, families)
            return self.dialect.q(name) if alias == name else f"{self.dialect.q(name)} {self.dialect.q(alias)}"
        if not isinstance(name, str) or name not in self.tables:
            raise AstError(f"없는 테이블입니다 -> {name}")
        alias = str(alias or "").strip() or name
        scope.add(alias, self.tables[name], self.families[name])
        scope.real[alias] = name
        schema = self.schemas[name]
        # 다른 스키마의 테이블은 '스키마.테이블' 이름 그대로를 별칭으로 둔다 — 컬럼을 그 이름으로 가리킨다
        if schema or alias != name:
            return f"{self.dialect.table(name, schema)} {self.dialect.q(alias)}"
        return self.dialect.q(name)

    def _tree(self, spec, alias, scope: Scope) -> str:
        """계층 하나. 시작 행에서 key, parent 를 따라간 행 전부와 depth(시작 = 0).

        재귀 CTE 는 (key, parent, depth) 만 들고 다니고, 끝에 key 마다 가장 얕은 depth 로 원래 행을 붙인다 —
        같은 행에 여러 길로 닿아도 한 번만 나온다. MAX_TREE_DEPTH 에서 멈추므로 고리가 있어도 끝난다.
        """
        if not isinstance(spec, dict):
            raise AstError('tree 는 {"table", "key", "parent", "start", "direction"} 입니다')
        unknown = set(spec) - TREE_KEYS
        if unknown:
            raise AstError(f"tree 에 모르는 키입니다 -> {', '.join(sorted(unknown))} (쓸 수 있는 키: {', '.join(sorted(TREE_KEYS))})")
        name = spec.get("table")
        if not isinstance(name, str) or name not in self.tables:
            raise AstError(f"없는 테이블입니다 -> {name}")
        columns, families = self.tables[name], self.families[name]
        key, parent = spec.get("key"), spec.get("parent")
        for label, col in (("key", key), ("parent", parent)):
            if not isinstance(col, str) or col not in columns:
                raise AstError(f"tree 의 {label} 는 {name} 의 컬럼입니다 -> {col} (있는 것: {', '.join(columns)})")
        direction = str(spec.get("direction") or "down").lower()
        if direction not in ("down", "up"):
            raise AstError("tree 의 direction 은 down(자식 쪽) 이나 up(부모 쪽) 입니다")
        start = spec.get("start")
        if not isinstance(start, list) or not start:
            raise AstError("tree 의 start 는 시작 행을 고르는 조건 목록입니다 (예: 사용자의 그룹)")
        alias = str(alias or "").strip() or name
        depth_name = "depth" if "depth" not in columns else "tree_depth"

        d, q = self.dialect, self.dialect.q
        start_scope = Scope(d)
        start_scope.add(name, columns, families)
        start_scope.real[name] = name
        where = self._conditions(start, start_scope, allow_agg=False)

        self.trees += 1
        cte = q(f"tree{self.trees}")
        source = d.table(name, self.schemas[name])
        # 다음 단계: down 은 자식의 parent = 지난 key, up 은 부모의 key = 지난 parent
        if direction == "down":
            child, prev = f"c.{q(parent)}", f"t.{q('k')}"
        else:
            child, prev = f"c.{q(key)}", f"t.{q('p')}"
        if families[parent] != families[key]:
            child, prev = d.text(child), d.text(prev)
        listed = []
        for col in columns:
            listed.append(f"s.{q(col)}")
        sql = (
            f"({d.recursive()} {cte} ({q('k')}, {q('p')}, {q('depth')}) AS ("
            f"SELECT {q(key)}, {q(parent)}, 0 FROM {source} WHERE {where} "
            f"UNION ALL "
            f"SELECT c.{q(key)}, c.{q(parent)}, t.{q('depth')} + 1 FROM {source} c "
            f"JOIN {cte} t ON {child} = {prev} WHERE t.{q('depth')} < {MAX_TREE_DEPTH}) "
            f"SELECT {', '.join(listed)}, m.{q('depth')} AS {q(depth_name)} FROM {source} s "
            f"JOIN (SELECT {q('k')}, MIN({q('depth')}) AS {q('depth')} FROM {cte} GROUP BY {q('k')}) m "
            f"ON s.{q(key)} = m.{q('k')}) {q(alias)}"
        )
        out_columns = dict(columns)
        out_columns[depth_name] = 0
        out_families = dict(families)
        out_families[depth_name] = "number"
        scope.add(alias, out_columns, out_families)
        scope.real[alias] = name  # 계산 필요 컬럼 검사와 '테이블.컬럼' 참조에 쓴다
        return sql

    def _check_aggregate(self, fn: str, refs: list):
        """[계산 필요] 컬럼의 집계. 고른 지표가 그 컬럼에 쓴 집계 함수만 된다.
        지표가 쓰지 않은 컬럼은 MIN, MAX, COUNT (값을 보여 주거나 세기)만 된다 — 더하거나 평균내면 틀린다."""
        if self.allowed is None:
            return
        for table, column in refs:
            used = self.allowed.get(column)
            if used is None and fn in ("SUM", "AVG"):
                self._calc_error(table, column, fn)
            if used is not None and fn not in used:
                self._calc_error(table, column, fn, f"지표처럼 {', '.join(sorted(used))} 로 계산하십시오")

    def _check_arithmetic(self, fn: str, refs: list):
        """[계산 필요] 컬럼의 집계끼리 계산(예: MAX - MIN = 하루 사용량)은 그 컬럼을 쓰는 지표를 골랐을 때만 된다"""
        if self.allowed is None:
            return
        for table, column in refs:
            if column not in self.allowed:
                self._calc_error(table, column, fn)

    def _calc_error(self, table: str, column: str, fn: str, hint: str = "지표의 계산 식을 그대로 쓰십시오"):
        raise CalcColumnError(
            f"{table}.{column} 은 [계산 필요] 컬럼이라 지표에 없는 계산({fn})을 할 수 없습니다 — {hint}",
            table,
            column,
        )

    def _column(self, alias: str, col: str, scope: Scope) -> str:
        """컬럼 SQL. UTC 컬럼은 현지 시각으로 보정한다."""
        raw = scope.raw(alias, col)
        hours = scope.sources[alias][col]
        return self.dialect.add_interval(raw, hours, "hours") if hours else raw

    def _order(self, items, scope: Scope, names: list) -> str:
        out = []
        for item in items or []:
            if not isinstance(item, dict) or "expr" not in item:
                raise AstError('order_by 항목은 {"expr": 식, "dir": "asc|desc"} 입니다')
            target = item["expr"]
            if isinstance(target, dict) and set(target) == {"col"} and target["col"] in names:
                sql = self.dialect.q(target["col"])  # select 이름으로 정렬
            else:
                sql = self._expr(target, scope)[0]
            way = str(item.get("dir", "asc")).lower()
            if way not in ("asc", "desc"):
                raise AstError("order_by dir 은 asc 나 desc 입니다")
            out.append(f"{sql} {way.upper()}")
        return ", ".join(out)

    # ── 조건 ─────────────────────────────────────────────────────────────

    def _conditions(self, items, scope: Scope, allow_agg: bool) -> str:
        if items is None:
            return ""
        if not isinstance(items, list):
            raise AstError("where·having 은 조건 목록입니다")
        out = []
        for c in items:
            out.append(self._condition(c, scope, allow_agg))
        return " AND ".join(out)

    def _condition(self, c, scope: Scope, allow_agg: bool) -> str:
        if isinstance(c, dict) and ("or" in c or "and" in c):
            key = "or" if "or" in c else "and"
            group = c[key]
            if not isinstance(group, list) or not group:
                raise AstError(f"{key} 는 조건 목록입니다")
            out = []
            for x in group:
                out.append(self._condition(x, scope, allow_agg))
            joined = f" {key.upper()} ".join(out)
            return f"({joined})" if len(group) > 1 else joined
        if not isinstance(c, dict) or "expr" not in c or "op" not in c:
            raise AstError('조건은 {"expr": 식, "op": "=", "value": 값} 입니다')
        op = str(c["op"]).strip().lower()
        if op not in CONDITION_OPS:
            raise AstError(f"모르는 연산자입니다 -> {op} (쓸 수 있는 것: {' '.join(CONDITION_OPS)})")
        expr, value = c["expr"], c.get("value")
        if op in ("in", "not_in") and isinstance(value, dict) and "query" in value:
            return self._in_query(expr, op, value["query"], scope)

        utc = self._utc_column(expr, scope)
        if utc and op in (*COMPARE, "in", "not_in", "between"):
            shifted = self._utc_values(value, utc[1])
            if shifted is not None:
                return self._compare(utc[0], op, shifted)

        left, info = self._expr(expr, scope)
        if info["window"] or (info["agg"] and not allow_agg):
            raise AstError("where 에는 집계·윈도 함수를 쓸 수 없습니다 (집계 조건은 having, 순위 조건은 하위 쿼리 바깥에서)")
        if op in ("is_null", "is_not_null"):
            return f"{left} {'IS NULL' if op == 'is_null' else 'IS NOT NULL'}"
        if op in ("in", "not_in", "between"):
            if not isinstance(value, list) or not value or (op == "between" and len(value) != 2):
                raise AstError(f"{op} 의 value 는 값 목록입니다" + (" (두 개)" if op == "between" else ""))
            right = []
            for v in value:
                right.append(self._value(v, scope, allow_agg))
            return self._compare(left, op, right)
        if op in ("like", "not_like"):
            return f"{left} {'LIKE' if op == 'like' else 'NOT LIKE'} {self.dialect.literal(value)}"
        return self._compare(left, op, self._value(value, scope, allow_agg))

    def _in_query(self, expr, op: str, ast, scope: Scope) -> str:
        """컬럼 IN (하위 쿼리). 예: 사용자의 그룹에서 시작하는 계층. 타입 갈래가 다르면 둘 다 문자로 비교한다."""
        d = self.dialect
        left, info = self._expr(expr, scope)
        if info["agg"] or info["window"]:
            raise AstError("in 하위 쿼리의 왼쪽에는 집계·윈도 함수를 쓸 수 없습니다")
        inner_sql, names = self._query(ast, self.level + 1)
        if len(names) != 1:
            raise AstError(f"in 의 하위 쿼리는 select 가 하나여야 합니다 -> {', '.join(names)}")
        inner_family = self.output_families.get(names[0])
        left_family = self._family_of(expr, scope)
        if left_family and inner_family and left_family != inner_family:
            left = d.text(left)
            inner_sql = f"SELECT {d.text('x.' + d.q(names[0]))} FROM ({inner_sql}) x"
        return f"{left} {'IN' if op == 'in' else 'NOT IN'} ({inner_sql})"

    def _family_of(self, expr, scope: Scope) -> str | None:
        """컬럼 하나인 식의 타입 갈래. 그 밖의 식이면 None"""
        if isinstance(expr, dict) and set(expr) == {"col"} and isinstance(expr["col"], str) and expr["col"] != "*":
            alias, col, _ = scope.resolve(expr["col"])
            return scope.families.get(alias, {}).get(col)
        return None

    def _compare(self, left: str, op: str, right) -> str:
        if op in ("in", "not_in"):
            return f"{left} {'IN' if op == 'in' else 'NOT IN'} ({', '.join(right)})"
        if op == "between":
            return f"{left} BETWEEN {right[0]} AND {right[1]}"
        return f"{left} {COMPARE[op]} {right}"

    def _value(self, value, scope: Scope, allow_agg: bool) -> str:
        if isinstance(value, dict):
            sql, info = self._expr(value, scope)
            if info["window"] or (info["agg"] and not allow_agg):
                raise AstError("where 의 값에는 집계·윈도 함수를 쓸 수 없습니다")
            return sql
        if isinstance(value, list):
            raise AstError("값 목록은 in · not_in · between 에만 씁니다")
        return self.dialect.literal(value)

    def _utc_column(self, expr, scope: Scope):
        """보정 대상 컬럼 하나만 있는 식이면 (저장된 컬럼 SQL, 시차)"""
        if isinstance(expr, dict) and set(expr) == {"col"} and expr["col"] != "*":
            alias, col, hours = scope.resolve(expr["col"])
            if hours:
                return scope.raw(alias, col), hours
        return None

    def _utc_values(self, value, hours: int):
        """현지 시각 상수(CURRENT_DATE 는 오늘 날짜로)를 UTC 로. 그 밖의 식이면 None (그때는 컬럼을 보정해 비교한다)."""
        values = value if isinstance(value, list) else [value]
        if not values:
            return None
        out = []
        for v in values:
            if isinstance(v, dict) and set(v) == {"str"}:
                v = v["str"]  # 값을 {"str": "…"} 식으로 적어도 상수다
            if isinstance(v, str):
                moment = parse_datetime(v)
            else:
                moment = self._local_day(v, hours)
            if moment is None:
                return None
            out.append(self.dialect.add_interval(self.dialect.timestamp(moment), -hours, "hours"))
        return out if isinstance(value, list) else out[0]

    def _local_day(self, value, hours: int) -> datetime.datetime | None:
        """CURRENT_DATE · DATE(CURRENT_DATE) · ADD_DAYS(그것, n) 이면 그 컬럼의 현지 날짜 0시, 아니면 None.
        DB 의 CURRENT_DATE 는 DB 시간대 날짜라 현지 '오늘' 과 다를 수 있고, 컬럼을 감싸면 인덱스를 못 탄다."""
        if not isinstance(value, dict):
            return None
        fn = str(value.get("fn", "")).upper()
        args = value.get("args") or []
        if fn == "CURRENT_DATE" and not args:
            now = self.utc_now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            local = now + datetime.timedelta(hours=hours)
            return datetime.datetime(local.year, local.month, local.day)
        if fn == "DATE" and len(args) == 1:
            return self._local_day(args[0], hours)
        if fn == "ADD_DAYS" and len(args) == 2 and isinstance(args[1], dict):
            n = args[1].get("num")
            day = self._local_day(args[0], hours)
            if day is None or isinstance(n, bool) or not isinstance(n, int):
                return None
            return day + datetime.timedelta(days=n)
        return None

    # ── 식 → (SQL, {agg, window, cols}) ──────────────────────────────────

    def _expr(self, e, scope: Scope, in_agg: bool = False):
        d = self.dialect
        if not isinstance(e, dict):
            raise AstError(f'식은 {{"col"}}, {{"num"}}, {{"str"}}, {{"fn", "args"}} 중 하나입니다 -> {e!r}')
        if "col" in e:
            if e["col"] == "*":
                raise AstError('{"col": "*"} 는 select 항목으로만 씁니다 (건수는 {"fn": "COUNT", "args": []})')
            alias, col, _ = scope.resolve(e["col"])
            table = scope.real.get(alias)
            raw = [(table, col)] if table and col in self.calc_columns.get(table, ()) else []
            return self._column(alias, col, scope), _info(cols=True, raw=raw)
        if "num" in e:
            if isinstance(e["num"], bool) or not isinstance(e["num"], (int, float)):
                raise AstError(f"num 은 숫자입니다 -> {e['num']!r}")
            return repr(e["num"]), _info()
        if "str" in e:
            return d.literal(str(e["str"])), _info()
        fn = str(e.get("fn") or "").upper()
        args = e.get("args") or []
        if not fn:
            raise AstError(f'식은 {{"col"}}, {{"num"}}, {{"str"}}, {{"fn", "args"}} 중 하나입니다 -> {e!r}')
        if not isinstance(args, list):
            raise AstError(f"{fn} 의 args 는 목록입니다")

        def subs(items, inside=in_agg):
            out = []
            for a in items:
                out.append(self._expr(a, scope, inside))
            return out

        if fn in AGGREGATES:
            if in_agg:
                raise AstError(f"집계 안에 집계를 쓸 수 없습니다 -> {fn} (두 단계 집계는 from 에 하위 쿼리를 쓰십시오)")
            if fn == "COUNT" and not args:
                return "COUNT(*)", _info(agg=True)
            if len(args) != 1:
                raise AstError(f"{fn} 은 인자 하나입니다")
            ((sql, info),) = subs(args, inside=True)
            if info["window"]:
                raise AstError("집계 안에 윈도 함수를 쓸 수 없습니다")
            if info["raw"]:
                self._check_aggregate(fn, info["raw"])
            body = f"COUNT(DISTINCT {sql})" if fn == "COUNT_DISTINCT" else f"{fn}({sql})"
            return body, {**info, "agg": True, "raw": [], "calc": info["raw"]}
        if fn in ARITHMETIC:
            if len(args) < 2:
                raise AstError(f"{fn} 은 인자 둘 이상입니다")
            parts = subs(args)
            merged = _merge(parts)
            if merged["calc"]:
                self._check_arithmetic(fn, merged["calc"])
            return "(" + f" {ARITHMETIC[fn]} ".join(_sqls(parts)) + ")", merged
        if fn == "DIV":
            if len(args) != 2:
                raise AstError("DIV 는 인자 둘입니다")
            parts = subs(args)
            merged = _merge(parts)
            if merged["calc"]:
                self._check_arithmetic(fn, merged["calc"])
            return f"({parts[0][0]} * 1.0 / NULLIF({parts[1][0]}, 0))", merged
        if fn in SCALARS:
            lo, hi = SCALARS[fn]
            if not lo <= len(args) <= hi:
                raise AstError(f"{fn} 은 인자 {lo}~{hi}개입니다")
            parts = subs(args)
            return f"{fn}({', '.join(_sqls(parts))})", _merge(parts)
        if fn == "DATE":
            if len(args) != 1:
                raise AstError("DATE 는 인자 하나입니다")
            ((sql, info),) = subs(args)
            return d.date(sql), info
        if fn == "DATE_TRUNC":
            unit = str(e.get("unit", "")).lower()
            if unit not in TRUNC_UNITS or len(args) != 1:
                raise AstError(f'DATE_TRUNC 는 {{"fn": "DATE_TRUNC", "unit": "{"|".join(TRUNC_UNITS)}", "args": [식]}} 입니다')
            ((sql, info),) = subs(args)
            return d.trunc(unit, sql), info
        if fn in INTERVAL_FUNCTIONS:
            if len(args) != 2 or not isinstance(args[1], dict) or "num" not in args[1]:
                raise AstError(f'{fn} 은 [식, {{"num": 숫자}}] 입니다')
            n = args[1]["num"]
            if isinstance(n, bool) or not isinstance(n, (int, float)):
                raise AstError(f"{fn} 의 두 번째 값은 숫자입니다")
            utc = self._utc_column(args[0], scope)
            if fn == "ADD_HOURS" and utc and n == -utc[1]:
                return utc[0], _info(cols=True)  # 보정을 되돌리면 저장된 UTC 값 그대로
            ((sql, info),) = subs(args[:1])
            return d.add_interval(sql, n, INTERVAL_FUNCTIONS[fn]), info
        if fn == "CASE":
            return self._case(e, scope, in_agg)
        if fn == "NOW":
            return "CURRENT_TIMESTAMP", _info()
        if fn == "CURRENT_DATE":
            return d.current_date(), _info()
        if fn == "ROW_NUMBER":
            if in_agg:
                raise AstError("집계 안에 윈도 함수를 쓸 수 없습니다")
            part = subs(e.get("partition_by") or [])
            order = self._order(e.get("order_by"), scope, [])
            if not order:
                raise AstError("ROW_NUMBER 에는 order_by 가 필요합니다")
            over = (f"PARTITION BY {', '.join(_sqls(part))} " if part else "") + "ORDER BY " + order
            return f"ROW_NUMBER() OVER ({over})", {**_merge(part), "window": True}
        known = sorted(AGGREGATES | set(ARITHMETIC) | set(SCALARS) | OTHER_FUNCTIONS)
        raise AstError(f"모르는 함수입니다 -> {fn} (쓸 수 있는 것: {', '.join(known)})")


    def _case(self, e, scope: Scope, in_agg: bool):
        """{"fn": "CASE", "when": [{"if": [조건], "then": 식}], "else": 식} — 조건은 where 와 같은 모양"""
        whens = e.get("when")
        if not isinstance(whens, list) or not whens:
            raise AstError('CASE 는 {"fn": "CASE", "when": [{"if": [조건], "then": 식}], "else": 식} 입니다')
        sql, parts = [], []
        for w in whens:
            if not isinstance(w, dict) or "if" not in w or "then" not in w:
                raise AstError('CASE 의 when 항목은 {"if": [조건], "then": 식} 입니다')
            conds = w["if"] if isinstance(w["if"], list) else [w["if"]]
            if not conds:
                raise AstError("CASE 의 if 가 비었습니다")
            info = self._conditions_info(conds, scope)
            if in_agg and info["agg"]:
                raise AstError("집계 안에 집계를 쓸 수 없습니다 -> CASE 조건 (두 단계 집계는 from 에 하위 쿼리를 쓰십시오)")
            then = self._expr(w["then"], scope, in_agg)
            sql.append(f"WHEN {self._conditions(conds, scope, allow_agg=True)} THEN {then[0]}")
            parts += [("", info), then]
        if e.get("else") is not None:
            other = self._expr(e["else"], scope, in_agg)
            sql.append(f"ELSE {other[0]}")
            parts.append(other)
        return f"(CASE {' '.join(sql)} END)", _merge(parts)

    def _conditions_info(self, conds, scope: Scope) -> dict:
        """조건에 쓰인 식들의 {agg, window, cols}"""
        parts = []
        for c in conds:
            if isinstance(c, dict) and ("or" in c or "and" in c):
                group = c.get("or") if "or" in c else c.get("and")
                parts.append(("", self._conditions_info(group if isinstance(group, list) else [], scope)))
                continue
            if not isinstance(c, dict) or "expr" not in c:
                continue  # 모양 오류는 _condition 이 알린다
            parts.append(self._expr(c["expr"], scope))
            values = c.get("value") if isinstance(c.get("value"), list) else [c.get("value")]
            for v in values:
                if isinstance(v, dict):
                    parts.append(self._expr(v, scope))
        return _merge(parts) if parts else _info()


def ast_tables(ast: dict) -> list[str]:
    """AST 가 쓰는 실제 테이블 이름. 가장 바깥 from 의 테이블이 먼저 온다.

    with 이름은 테이블이 아니다 — 그 이름이 쓰인 자리에서 with 안의 테이블로 바꿔 센다.
    """
    out: list[str] = []
    ctes: dict = {}
    if isinstance(ast, dict) and isinstance(ast.get("with"), list):
        for item in ast["with"]:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                ctes[item["name"].strip()] = item.get("query")
    opened: set = set()

    def add(name):
        if name in ctes:
            if name not in opened:  # 같은 with 를 여러 번 써도 한 번만 연다
                opened.add(name)
                walk(ctes[name])
        elif isinstance(name, str):
            out.append(name)

    def walk(node):
        if not isinstance(node, dict):
            return
        source = node.get("from")
        if isinstance(source, str):
            add(source)
        elif isinstance(source, dict) and isinstance(source.get("table"), str):
            add(source["table"])
        elif isinstance(source, dict) and isinstance(source.get("tree"), dict):
            add(source["tree"].get("table"))
            conditions(source["tree"].get("start"))
        elif isinstance(source, dict):
            walk(source.get("query"))
        for j in node.get("joins") or []:
            if not isinstance(j, dict):
                continue
            if isinstance(j.get("table"), str):
                add(j["table"])
            elif isinstance(j.get("tree"), dict):
                add(j["tree"].get("table"))
                conditions(j["tree"].get("start"))
            elif isinstance(j.get("table"), dict):
                walk(j["table"].get("query"))
            elif "query" in j:
                walk(j.get("query"))
        conditions(node.get("where"))
        conditions(node.get("having"))

    def conditions(items):
        """조건의 in (하위 쿼리) 가 쓰는 테이블"""
        if isinstance(items, dict):
            items = [items]
        for c in items if isinstance(items, list) else []:
            if not isinstance(c, dict):
                continue
            conditions(c.get("or"))
            conditions(c.get("and"))
            if isinstance(c.get("value"), dict):
                walk(c["value"].get("query"))

    walk(ast)
    return list(dict.fromkeys(out))
