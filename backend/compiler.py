"""조회 명세(AST) → SQL. 조합하고 검증만 한다 — 무엇을 어떻게 계산할지는 LLM 과 지표가 정한다.

예외는 하나: UTC 로 저장된 시각 컬럼(utc_offset)은 현지 시각으로 자동 보정한다. AST 의 시각은 모두 현지 시각이다.
  - 컬럼을 쓰면 (컬럼 + 시차) 로 바꾼다 — 날짜 묶음·표시가 현지 기준이 된다.
  - 컬럼을 상수와 비교하면 반대로 상수를 UTC 로 바꾼다 — 컬럼을 감싸지 않아야 인덱스를 탄다.

AST 모양
  {"from": "테이블" | {"query": AST, "as": "별칭"},
   "joins": [{"table": "테이블", "type": "inner|left", "on": [["a.컬럼", "b.컬럼"]]}],
   "select": [{"expr": 식, "as": "이름"}],        {"expr": {"col": "*"}} 는 모든 컬럼
   "distinct": true, "where": [조건], "group_by": [식], "having": [조건],
   "order_by": [{"expr": 식, "dir": "asc|desc"}], "limit": 숫자}
  식: {"col": "컬럼|테이블.컬럼"} | {"num": 1} | {"str": "문자"} | {"fn": "함수", "args": [식]}
  조건: {"expr": 식, "op": "=", "value": 값 또는 식} | {"or": [조건]} | {"and": [조건]}  — 목록은 AND
"""

import datetime

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
OTHER_FUNCTIONS = {"DIV", "DATE", "DATE_TRUNC", "ADD_HOURS", "ADD_DAYS", "ADD_MINUTES", "NOW", "CURRENT_DATE", "ROW_NUMBER", "CASE"}
INTERVAL_FUNCTIONS = {"ADD_MINUTES": "minutes", "ADD_HOURS": "hours", "ADD_DAYS": "days"}
TRUNC_UNITS = ("hour", "day", "week", "month", "year")
_ORACLE_TRUNC = {"hour": "HH24", "day": "DD", "week": "IW", "month": "MM", "year": "YYYY"}
COMPARE = {"=": "=", "!=": "<>", "<>": "<>", ">": ">", ">=": ">=", "<": "<", "<=": "<="}
CONDITION_OPS = tuple(COMPARE) + ("in", "not_in", "like", "not_like", "between", "is_null", "is_not_null")
QUERY_KEYS = {"from", "joins", "select", "distinct", "where", "group_by", "having", "order_by", "limit"}
MAX_LIMIT = 10000
MAX_DEPTH = 3


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
        """unit: minutes | hours | days"""
        sign, n = ("+", n) if n >= 0 else ("-", -n)
        amount = f"{n:g}"
        word = {"minutes": "MINUTE", "hours": "HOUR", "days": "DAY"}[unit]
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


class Scope:
    """FROM 절이 보이는 이름들. {별칭: {컬럼: UTC 시차(없으면 0)}}"""

    def __init__(self, dialect: Dialect):
        self.dialect = dialect
        self.sources: dict[str, dict[str, int]] = {}

    def add(self, alias: str, columns: dict):
        if alias in self.sources:
            raise AstError(f"FROM 의 테이블·별칭이 겹칩니다 -> {alias}")
        self.sources[alias] = dict(columns)

    def resolve(self, ref) -> tuple[str, str, int]:
        """→ (별칭, 컬럼, 시차)"""
        ref = str(ref).strip()
        if "." in ref:
            alias, col = ref.split(".", 1)
            if alias not in self.sources:
                raise AstError(f"FROM 에 없는 테이블입니다 -> {alias} (있는 것: {', '.join(self.sources)})")
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


def _info(agg=False, window=False, cols=False) -> dict:
    return {"agg": agg, "window": window, "cols": cols}


def _merge(parts) -> dict:
    merged = _info()
    for _, info in parts:
        for key in merged:
            if info[key]:
                merged[key] = True
    return merged


def _sqls(parts) -> list[str]:
    """(SQL, info) 목록에서 SQL 만"""
    out = []
    for sql, _ in parts:
        out.append(sql)
    return out


class Compiler:
    def __init__(self, contract: dict):
        self.dialect = Dialect(contract.get("driver") or "postgresql")
        self.tables: dict[str, dict[str, int]] = {}
        for t in contract.get("tables", []):
            cols = {}
            for c in t.get("columns", []):
                hours = c.get("utc_offset") or 0
                if hours and temporal_kind(c.get("type", ""), self.dialect.driver) != "datetime":
                    hours = 0
                cols[c["name"]] = int(hours)
            self.tables[t["name"]] = cols

    def compile(self, ast: dict) -> str:
        sql, _ = self._query(ast, 0)
        return sql

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

        source = ast.get("from")
        if isinstance(source, str):
            from_sql = self._table(source, scope)
        elif isinstance(source, dict) and "query" in source:
            alias = str(source.get("as") or "sub").strip()
            inner_sql, names = self._query(source["query"], depth + 1)
            scope.add(alias, dict.fromkeys(names, 0))
            from_sql = f"({inner_sql}) {d.q(alias)}"
        else:
            raise AstError('from 은 테이블 이름이나 {"query": AST, "as": "별칭"} 입니다')

        for join in ast.get("joins") or []:
            if not isinstance(join, dict):
                raise AstError('joins 항목은 {"table", "type", "on"} 입니다')
            kind = {"inner": "JOIN", "left": "LEFT JOIN"}.get(str(join.get("type", "inner")).lower())
            if kind is None:
                raise AstError("join type 은 inner 나 left 입니다")
            table_sql = self._table(join.get("table"), scope)
            pairs = join.get("on") or []
            if not pairs:
                raise AstError(f"{join.get('table')} 조인에 on 이 없습니다")
            on = []
            for pair in pairs:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    raise AstError('on 은 [["a.컬럼", "b.컬럼"]] 모양입니다')
                sides = []
                for ref in pair:
                    alias, col, _ = scope.resolve(ref)
                    sides.append(scope.raw(alias, col))
                on.append(f"{sides[0]} = {sides[1]}")
            from_sql += f" {kind} {table_sql} ON {' AND '.join(on)}"

        select = ast.get("select") or []
        if not isinstance(select, list) or not select:
            raise AstError("select 가 비었습니다")
        names, items, has_agg = [], [], False
        for item in select:
            if not isinstance(item, dict) or "expr" not in item:
                raise AstError('select 항목은 {"expr": 식, "as": "이름"} 입니다')
            if item["expr"] == {"col": "*"}:
                for alias, cols in scope.sources.items():
                    for col in cols:
                        if col in names:
                            raise AstError(f"* 로 펼친 컬럼 이름이 겹칩니다 -> {col} (컬럼을 하나씩 적으십시오)")
                        names.append(col)
                        items.append((self._column(alias, col, scope), _info(cols=True), col))
                continue
            name = str(item.get("as") or "").strip()
            if not name:
                raise AstError(f"select 항목에 as(결과 이름)가 없습니다 -> {item['expr']!r}")
            if name in names:
                raise AstError(f"select 이름이 겹칩니다 -> {name}")
            names.append(name)
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
        return " ".join(parts), names

    def _table(self, name, scope: Scope) -> str:
        if not isinstance(name, str) or name not in self.tables:
            raise AstError(f"없는 테이블입니다 -> {name}")
        scope.add(name, self.tables[name])
        return self.dialect.q(name)

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
        """현지 시각 상수를 UTC 로. 날짜·시각 상수가 아니면 None (그때는 컬럼을 보정해 비교한다)."""
        values = value if isinstance(value, list) else [value]
        if not values:
            return None
        out = []
        for v in values:
            if isinstance(v, dict) and set(v) == {"str"}:
                v = v["str"]  # 값을 {"str": "…"} 식으로 적어도 상수다
            moment = parse_datetime(v) if isinstance(v, str) else None
            if moment is None:
                return None
            out.append(self.dialect.add_interval(self.dialect.timestamp(moment), -hours, "hours"))
        return out if isinstance(value, list) else out[0]

    # ── 식 → (SQL, {agg, window, cols}) ──────────────────────────────────

    def _expr(self, e, scope: Scope, in_agg: bool = False):
        d = self.dialect
        if not isinstance(e, dict):
            raise AstError(f'식은 {{"col"}}, {{"num"}}, {{"str"}}, {{"fn", "args"}} 중 하나입니다 -> {e!r}')
        if "col" in e:
            if e["col"] == "*":
                raise AstError('{"col": "*"} 는 select 항목으로만 씁니다 (건수는 {"fn": "COUNT", "args": []})')
            alias, col, _ = scope.resolve(e["col"])
            return self._column(alias, col, scope), _info(cols=True)
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
            body = f"COUNT(DISTINCT {sql})" if fn == "COUNT_DISTINCT" else f"{fn}({sql})"
            return body, {**info, "agg": True}
        if fn in ARITHMETIC:
            if len(args) < 2:
                raise AstError(f"{fn} 은 인자 둘 이상입니다")
            parts = subs(args)
            return "(" + f" {ARITHMETIC[fn]} ".join(_sqls(parts)) + ")", _merge(parts)
        if fn == "DIV":
            if len(args) != 2:
                raise AstError("DIV 는 인자 둘입니다")
            parts = subs(args)
            return f"({parts[0][0]} * 1.0 / NULLIF({parts[1][0]}, 0))", _merge(parts)
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
    """AST 가 쓰는 실제 테이블 이름. 가장 바깥 from 의 테이블이 먼저 온다."""
    out: list[str] = []

    def walk(node):
        if not isinstance(node, dict):
            return
        source = node.get("from")
        if isinstance(source, str):
            out.append(source)
        elif isinstance(source, dict):
            walk(source.get("query"))
        for j in node.get("joins") or []:
            if isinstance(j, dict) and isinstance(j.get("table"), str):
                out.append(j["table"])

    walk(ast)
    return list(dict.fromkeys(out))
