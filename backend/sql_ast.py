"""SQL → 조회 명세(AST). 지표를 SQL 로 등록할 때 쓴다.

AST 가 표현하지 못하는 문법은 무엇 때문인지 밝히고 실패한다 (UNION · OFFSET · 서브쿼리 조건 …).
바꾼 AST 를 다시 컴파일한 SQL 이 원본과 같은 결과를 내는지는 metrics.verify 가 대상 DB 에서 확인한다.

UTC 로 저장된 시각 컬럼(utc_offset)은 AST 에서 현지 시각이다 (compiler.py). 그래서 원본 SQL 의 뜻을 그대로 옮긴다.
  - 컬럼 + INTERVAL '시차 hours'  → 컬럼 (현지 시각)
  - 보정 없이 쓴 컬럼             → ADD_HOURS(컬럼, -시차) (원본처럼 UTC 값) — 경고를 남긴다
  - 컬럼 > '2026-10-01' (UTC 상수) → 컬럼 > '2026-10-01 09:00:00' (현지 상수)
"""

import datetime

import sqlglot
from sqlglot import exp

from compiler import parse_datetime, temporal_kind

DIALECTS = {"postgresql": "postgres", "mysql": "mysql", "oracle": "oracle"}
_AGGREGATES = {exp.Sum: "SUM", exp.Avg: "AVG", exp.Min: "MIN", exp.Max: "MAX"}
_ARITHMETIC = {exp.Add: "ADD", exp.Sub: "SUB", exp.Mul: "MUL", exp.Div: "DIV"}
_SCALARS = {exp.Coalesce: "COALESCE", exp.Greatest: "GREATEST", exp.Least: "LEAST"}
_COMPARE = {exp.EQ: "=", exp.NEQ: "!=", exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<="}
_FLIP = {"=": "=", "!=": "!=", ">": "<", ">=": "<=", "<": ">", "<=": ">="}
_INTERVAL_FN = {"minutes": "ADD_MINUTES", "hours": "ADD_HOURS", "days": "ADD_DAYS"}
_TRUNC = {"hour": "hour", "hours": "hour", "hh24": "hour", "day": "day", "dd": "day", "week": "week", "iw": "week",
          "month": "month", "mm": "month", "year": "year", "yyyy": "year"}


def parse_sql(sql: str, contract: dict) -> dict:
    """→ {"ast": AST, "warnings": [문장]}. 바꿀 수 없으면 ValueError."""
    text = str(sql or "").strip().rstrip(";").strip()
    if not text:
        raise ValueError("SQL 이 비어 있습니다")
    driver = contract.get("driver") or "postgresql"
    try:
        parsed = sqlglot.parse(text, read=DIALECTS.get(driver, "postgres"))
    except sqlglot.errors.ParseError as error:
        raise ValueError(f"SQL 을 읽을 수 없습니다 -> {str(error).splitlines()[0]}") from None
    trees = []
    for t in parsed:
        if t is not None:
            trees.append(t)
    if len(trees) != 1:
        raise ValueError(f"SQL 문이 {len(trees)}개입니다 — 하나만 넣으십시오")
    tree = trees[0]
    if not isinstance(tree, exp.Select):
        raise ValueError(f"SELECT 문만 받습니다 ({type(tree).__name__.upper()} 은 지원하지 않습니다)")
    parser = _Parser(contract)
    with_ = tree.args.get("with_") or tree.args.get("with")
    if with_ is not None:
        for cte in with_.expressions:
            parser.ctes[cte.alias] = cte.this
    ast = parser.select(tree)
    return {"ast": ast, "warnings": list(dict.fromkeys(parser.warnings))}


class _Scope:
    def __init__(self):
        self.sources: dict[str, dict] = {}  # SQL 별칭 → {"name": AST 이름, "columns": {컬럼: 시차}, "table": bool}

    def owner(self, column: exp.Column) -> tuple[str, dict]:
        table = column.table
        if table:
            for alias, src in self.sources.items():
                if alias.lower() == table.lower():
                    return alias, src
            raise ValueError(f"FROM 에 없는 테이블·별칭입니다 -> {table}")
        owners = []
        for alias, src in self.sources.items():
            if _find(src["columns"], column.name) is not None:
                owners.append((alias, src))
        if not owners:
            raise ValueError(f"없는 컬럼입니다 -> {column.name}")
        if len(owners) > 1:
            raise ValueError(f"여러 테이블에 있는 컬럼입니다 -> {column.name} (테이블을 붙여 적으십시오)")
        return owners[0]

    def knows(self, name: str) -> bool:
        """어느 FROM 소스에든 있는 컬럼인가"""
        for src in self.sources.values():
            if _find(src["columns"], name) is not None:
                return True
        return False


def _find(columns: dict, name: str):
    """컬럼 이름을 실제 이름으로 (대소문자 무시)"""
    if name in columns:
        return name
    for c in columns:
        if c.lower() == name.lower():
            return c
    return None


def _subquery_columns(inner: dict) -> dict:
    """하위 쿼리 결과 컬럼 → 시차 0 (UTC 정보는 하위 쿼리 밖으로 나오지 않는다)"""
    columns = {}
    for s in inner["select"]:
        columns[s["as"]] = 0
    return columns


class _Parser:
    def __init__(self, contract: dict):
        driver = contract.get("driver") or "postgresql"
        self.tables = {}
        for t in contract.get("tables", []):
            cols = {}
            for c in t.get("columns", []):
                hours = c.get("utc_offset") or 0
                if hours and temporal_kind(c.get("type", ""), driver) != "datetime":
                    hours = 0
                cols[c["name"]] = int(hours)
            self.tables[t["name"].lower()] = (t["name"], cols)
        self.ctes: dict = {}
        self.warnings: list[str] = []

    # ── SELECT 한 단계 ───────────────────────────────────────────────────

    def select(self, query) -> dict:
        if isinstance(query, exp.Subquery):
            query = query.this
        if not isinstance(query, exp.Select):
            raise ValueError(f"하위 쿼리는 SELECT 만 받습니다 ({type(query).__name__.upper()} 은 지원하지 않습니다)")
        for key, what in (("offset", "OFFSET"), ("qualify", "QUALIFY"), ("windows", "WINDOW 절")):
            if query.args.get(key):
                raise ValueError(f"{what} 은(는) 지원하지 않습니다")
        distinct = query.args.get("distinct")
        if distinct is not None and distinct.args.get("on") is not None:
            raise ValueError("DISTINCT ON 은 지원하지 않습니다 — ROW_NUMBER() OVER (...) 로 바꿔 적으십시오")

        scope = _Scope()
        ast: dict = {}
        source = query.args.get("from_") or query.args.get("from")
        if source is None:
            raise ValueError("FROM 이 없습니다")
        ast["from"] = self._source(source.this, scope, first=True)

        joins = []
        for join in query.args.get("joins") or []:
            joins.append(self._join(join, scope))
        if joins:
            ast["joins"] = joins

        select, aliases = [], {}
        for i, item in enumerate(query.expressions, start=1):
            body = item.unalias() if isinstance(item, exp.Alias) else item
            if isinstance(body, exp.Star) or (isinstance(body, exp.Column) and isinstance(body.this, exp.Star)):
                select += self._star(body, scope)
                continue
            name = item.alias if isinstance(item, exp.Alias) else (body.name if isinstance(body, exp.Column) else "")
            name = name or f"col{i}"
            node = self.node(body, scope)
            select.append({"expr": node, "as": name})
            aliases[name.lower()] = body
        names, seen = [], set()
        for s in select:
            if s["as"].lower() in seen:
                raise ValueError("SELECT 결과 이름이 겹칩니다 — 별칭(AS)을 붙여 주십시오")
            seen.add(s["as"].lower())
            names.append(s["as"])
        ast["select"] = select
        if distinct is not None:
            ast["distinct"] = True

        where = query.args.get("where")
        if where is not None:
            ast["where"] = self.conditions(where.this, scope)

        group = query.args.get("group")
        if group is not None:
            ast["group_by"] = []
            for g in group.expressions:
                ast["group_by"].append(self._group_item(g, query, scope, aliases))

        having = query.args.get("having")
        if having is not None:
            ast["having"] = self.conditions(having.this, scope, aliases)

        order = query.args.get("order")
        if order is not None:
            ast["order_by"] = []
            for o in order.expressions:
                ast["order_by"].append(self._order_item(o, query, scope, names))

        limit = query.args.get("limit")
        if limit is not None:
            value = limit.expression
            if not (isinstance(value, exp.Literal) and not value.is_string and str(value.this).isdigit()):
                raise ValueError(f"LIMIT 은 숫자만 받습니다 -> {limit.sql()}")
            ast["limit"] = int(value.this)
        return ast

    def _source(self, node, scope: _Scope, first=False):
        if isinstance(node, exp.Table):  # 스키마 이름(public.bms_log)은 떼고 테이블 이름으로 찾는다
            if node.name in self.ctes and not node.db:
                if not first:
                    raise ValueError(f"WITH 로 만든 {node.name} 은(는) FROM 에만 쓸 수 있습니다 (JOIN 은 지원하지 않습니다)")
                alias = node.alias or node.name
                inner = self.select(self.ctes[node.name])
                scope.sources[alias] = {"name": alias, "columns": _subquery_columns(inner), "table": False}
                return {"query": inner, "as": alias}
            found = self.tables.get(node.name.lower())
            if found is None:
                raise ValueError(f"메타데이터에 없는 테이블입니다 -> {node.name} (동기화했는지 확인하십시오)")
            real, columns = found
            alias = node.alias or real
            for s in scope.sources.values():
                if s["name"] == real:
                    raise ValueError(f"같은 테이블을 두 번 쓸 수 없습니다 (셀프 조인) -> {real}")
            scope.sources[alias] = {"name": real, "columns": columns, "table": True}
            return real
        if isinstance(node, exp.Subquery):
            if not first:
                raise ValueError("JOIN 에는 테이블만 쓸 수 있습니다 (하위 쿼리 JOIN 은 지원하지 않습니다)")
            alias = node.alias or "sub"
            inner = self.select(node.this)
            scope.sources[alias] = {"name": alias, "columns": _subquery_columns(inner), "table": False}
            return {"query": inner, "as": alias}
        raise ValueError(f"FROM 에 쓸 수 없는 것입니다 -> {node.sql()}")

    def _join(self, join: exp.Join, scope: _Scope) -> dict:
        side = (join.side or "").upper()
        kind = (join.kind or "").upper()
        if side == "LEFT" and kind in ("", "OUTER"):
            how = "left"
        elif not side and kind in ("", "INNER"):
            how = "inner"
        else:
            raise ValueError(f"{f'{side} {kind}'.strip()} JOIN 은 지원하지 않습니다 (INNER · LEFT 만)")
        if join.args.get("using"):
            raise ValueError("JOIN … USING 은 지원하지 않습니다 — ON a.x = b.x 로 적으십시오")
        table = self._source(join.this, scope)
        on = join.args.get("on")
        if on is None:
            raise ValueError(f"{table} JOIN 에 ON 이 없습니다")
        pairs = []
        for part in _split(on, exp.And):
            if not (isinstance(part, exp.EQ) and isinstance(part.this, exp.Column) and isinstance(part.expression, exp.Column)):
                raise ValueError(f"JOIN 조건은 컬럼 = 컬럼 만 받습니다 -> {part.sql()}")
            pairs.append([self._qualified(part.this, scope), self._qualified(part.expression, scope)])
        return {"table": table, "type": how, "on": pairs}

    def _qualified(self, column: exp.Column, scope: _Scope) -> str:
        _, src = scope.owner(column)
        name = _find(src["columns"], column.name)
        return f"{src['name']}.{name}"

    def _star(self, body, scope: _Scope) -> list:
        """SELECT * 를 컬럼마다 펼친다. UTC 컬럼은 원본처럼 저장된 값으로 (ADD_HOURS -시차)."""
        if isinstance(body, exp.Column) and body.table:
            alias, _ = scope.owner(exp.column("x", table=body.table))
            sources = {alias: scope.sources[alias]}
        else:
            sources = scope.sources
        out, seen = [], set()
        for src in sources.values():
            for col, hours in src["columns"].items():
                if col.lower() in seen:
                    raise ValueError(f"* 로 펼친 컬럼 이름이 겹칩니다 -> {col} (컬럼을 하나씩 적으십시오)")
                seen.add(col.lower())
                ref = self._ref(src, col, len(scope.sources) > 1)
                expr = {"col": ref}
                if hours:
                    expr = {"fn": "ADD_HOURS", "args": [expr, {"num": -hours}]}
                out.append({"expr": expr, "as": col})
        return out

    @staticmethod
    def _ref(src: dict, col: str, qualify: bool) -> str:
        return f"{src['name']}.{col}" if qualify else col

    def _group_item(self, g, query, scope: _Scope, aliases: dict):
        if isinstance(g, exp.Literal) and not g.is_string:
            return self.node(_unalias(query.expressions[int(g.this) - 1]), scope)
        if isinstance(g, exp.Column) and not g.table:
            # 입력 컬럼이 결과 별칭보다 먼저다 (PostgreSQL 규칙)
            if not scope.knows(g.name) and g.name.lower() in aliases:
                return self.node(aliases[g.name.lower()], scope)
        return self.node(g, scope)

    def _order_item(self, o: exp.Ordered, query, scope: _Scope, names: list) -> dict:
        target = o.this
        direction = "desc" if o.args.get("desc") else "asc"
        if isinstance(target, exp.Literal) and not target.is_string:
            return {"expr": {"col": names[int(target.this) - 1]}, "dir": direction}
        if isinstance(target, exp.Column) and not target.table:
            # 결과 별칭이 입력 컬럼보다 먼저다
            for n in names:
                if n.lower() == target.name.lower():
                    return {"expr": {"col": n}, "dir": direction}
        return {"expr": self.node(target, scope), "dir": direction}

    # ── 조건 ─────────────────────────────────────────────────────────────

    def conditions(self, node, scope: _Scope, aliases: dict | None = None) -> list:
        out = []
        for part in _split(node, exp.And):
            out += self._condition(part, scope, aliases)
        return out

    def _condition(self, node, scope: _Scope, aliases) -> list:
        """조건 하나 → 조건 목록 (BETWEEN 처럼 둘이 되기도 한다)"""
        if isinstance(node, exp.Paren):
            return self.conditions(node.this, scope, aliases)
        if isinstance(node, exp.Or):
            group = []
            for part in _split(node, exp.Or):
                inner = self.conditions(part, scope, aliases)
                group.append(inner[0] if len(inner) == 1 else {"and": inner})
            return [{"or": group}]
        negate = False
        if isinstance(node, exp.Not):
            negate, node = True, node.this
            if isinstance(node, exp.Paren):
                node = node.this
        if isinstance(node, exp.Is):
            if not isinstance(node.expression, exp.Null):
                raise ValueError(f"IS 는 NULL 비교만 받습니다 -> {node.sql()}")
            null = negate != bool(node.args.get("negate"))
            return [{"expr": self.node(node.this, scope, aliases), "op": "is_not_null" if null else "is_null"}]
        if isinstance(node, exp.In):
            if node.args.get("query") is not None:
                raise ValueError("IN (서브쿼리) 는 지원하지 않습니다")
            values = []
            for v in node.expressions:
                literal = self._literal(v)
                if literal is _NOT_LITERAL:
                    raise ValueError(f"IN 목록은 상수만 받습니다 -> {node.sql()}")
                values.append(literal)
            return [self._compare(node.this, "not_in" if negate else "in", values, scope, aliases)]
        if isinstance(node, exp.Like):
            pattern = self._literal(node.expression)
            if not isinstance(pattern, str):
                raise ValueError(f"LIKE 는 문자열 상수만 받습니다 -> {node.sql()}")
            return [{"expr": self.node(node.this, scope, aliases), "op": "not_like" if negate else "like", "value": pattern}]
        if negate:
            raise ValueError(f"NOT 은 IN · LIKE · IS NULL 에만 쓸 수 있습니다 -> NOT {node.sql()}")
        if isinstance(node, exp.Between):
            return [
                self._compare(node.this, ">=", node.args["low"], scope, aliases),
                self._compare(node.this, "<=", node.args["high"], scope, aliases),
            ]
        for cls, op in _COMPARE.items():
            if isinstance(node, cls):
                left, right = node.this, node.expression
                if self._literal(left) is not _NOT_LITERAL and self._literal(right) is _NOT_LITERAL:
                    left, right, op = right, left, _FLIP[op]
                return [self._compare(left, op, right, scope, aliases)]
        if isinstance(node, (exp.ILike, exp.Exists)):
            raise ValueError(f"{type(node).__name__.upper()} 은(는) 지원하지 않습니다")
        raise ValueError(f"조건으로 쓸 수 없는 식입니다 -> {node.sql()}")

    def _compare(self, left, op: str, right, scope: _Scope, aliases) -> dict:
        """UTC 컬럼과 날짜 상수의 비교는 상수를 현지 시각으로 옮긴다 (컬럼은 그대로 둔다)."""
        values = right if isinstance(right, list) else [right]
        literals, moments = [], []
        for v in values:
            literals.append(self._literal(v) if isinstance(v, exp.Expression) else v)
            moments.append(_moment(v))
        utc = self._utc_column(left, scope)
        if utc and None not in moments:
            ref, hours = utc
            shifted = []
            for m in moments:
                shifted.append((m + datetime.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S"))
            return {"expr": {"col": ref}, "op": op, "value": shifted if isinstance(right, list) else shifted[0]}
        expr = self.node(left, scope, aliases)
        if isinstance(right, list):
            return {"expr": expr, "op": op, "value": literals}
        literal = literals[0]
        value = self.node(right, scope, aliases) if literal is _NOT_LITERAL else literal
        return {"expr": expr, "op": op, "value": value}

    def _utc_column(self, node, scope: _Scope):
        """UTC 로 저장된 컬럼 하나면 (AST 참조, 시차)"""
        while isinstance(node, exp.Paren):
            node = node.this
        if not isinstance(node, exp.Column) or isinstance(node.this, exp.Star):
            return None
        _, src = scope.owner(node)
        name = _find(src["columns"], node.name)
        hours = src["columns"].get(name, 0)
        if not hours:
            return None
        return self._ref(src, name, len(scope.sources) > 1), hours

    def _literal(self, node):
        """상수면 그 값 (문자열·숫자). 아니면 _NOT_LITERAL"""
        while isinstance(node, exp.Paren):
            node = node.this
        if isinstance(node, exp.Cast) and isinstance(node.this, exp.Literal) and node.this.is_string:
            return node.this.this  # '2026-10-01'::timestamp · TIMESTAMP '…'
        if isinstance(node, exp.Literal):
            return node.this if node.is_string else _number(node.this)
        if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal) and not node.this.is_string:
            return -_number(node.this.this)
        return _NOT_LITERAL

    # ── 식 ───────────────────────────────────────────────────────────────

    def node(self, node, scope: _Scope, aliases: dict | None = None) -> dict:
        if isinstance(node, exp.Paren):
            return self.node(node.this, scope, aliases)
        if isinstance(node, exp.Column):
            if isinstance(node.this, exp.Star):
                raise ValueError("t.* 는 SELECT 목록에만 쓸 수 있습니다")
            if aliases and not node.table and node.name.lower() in aliases:
                if not scope.knows(node.name):
                    return self.node(aliases[node.name.lower()], scope, aliases)
            _, src = scope.owner(node)
            name = _find(src["columns"], node.name)
            ref = {"col": self._ref(src, name, len(scope.sources) > 1)}
            hours = src["columns"].get(name, 0)
            if hours:
                self.warnings.append(
                    f"{name} 은(는) UTC 로 저장된 컬럼인데 시차 보정 없이 썼습니다 (원본처럼 UTC 기준으로 옮겼습니다). "
                    f"현지 시각 기준이면 {name} + INTERVAL '{hours} hours' 로 적으십시오."
                )
                return {"fn": "ADD_HOURS", "args": [ref, {"num": -hours}]}
            return ref
        literal = self._literal(node)
        if literal is not _NOT_LITERAL:
            return {"str": literal} if isinstance(literal, str) else {"num": literal}
        if isinstance(node, exp.Neg):
            return {"fn": "SUB", "args": [{"num": 0}, self.node(node.this, scope, aliases)]}
        if isinstance(node, (exp.Add, exp.Sub)):
            shifted = self._interval_add(node, scope, aliases)
            if shifted is not None:
                return shifted
        for cls, name in _ARITHMETIC.items():
            if isinstance(node, cls):
                left = self.node(node.this, scope, aliases)
                right = self.node(node.expression, scope, aliases)
                # 같은 연산이 이어지면 하나로 (a - b - c → SUB(a, b, c)). DIV 는 두 값만.
                if name != "DIV" and left.get("fn") == name:
                    return {"fn": name, "args": left["args"] + [right]}
                if name == "DIV":
                    left, right = _strip_div_guards(left, right)
                return {"fn": name, "args": [left, right]}
        for cls, name in _AGGREGATES.items():
            if isinstance(node, cls):
                return {"fn": name, "args": [self.node(node.this, scope, aliases)]}
        if isinstance(node, exp.Count):
            arg = node.this
            if arg is None or isinstance(arg, exp.Star):
                return {"fn": "COUNT", "args": []}
            if isinstance(arg, exp.Distinct):
                if len(arg.expressions) != 1:
                    raise ValueError(f"COUNT(DISTINCT …) 는 컬럼 하나만 받습니다 -> {node.sql()}")
                return {"fn": "COUNT_DISTINCT", "args": [self.node(arg.expressions[0], scope, aliases)]}
            return {"fn": "COUNT", "args": [self.node(arg, scope, aliases)]}
        if isinstance(node, exp.Abs):
            return {"fn": "ABS", "args": [self.node(node.this, scope, aliases)]}
        if isinstance(node, exp.Round):
            args = [self.node(node.this, scope, aliases)]
            if node.args.get("decimals") is not None:
                args.append(self.node(node.args["decimals"], scope, aliases))
            return {"fn": "ROUND", "args": args}
        if isinstance(node, exp.Nullif):
            return {"fn": "NULLIF", "args": [self.node(node.this, scope, aliases), self.node(node.expression, scope, aliases)]}
        for cls, name in _SCALARS.items():
            if isinstance(node, cls):
                args = []
                for a in [node.this, *(node.expressions or [])]:
                    args.append(self.node(a, scope, aliases))
                return {"fn": name, "args": args}
        if isinstance(node, (exp.Date, exp.TsOrDsToDate)) or (
            isinstance(node, exp.Cast) and node.to.this == exp.DataType.Type.DATE
        ):
            return {"fn": "DATE", "args": [self.node(node.this, scope, aliases)]}
        if isinstance(node, (exp.TimestampTrunc, exp.DateTrunc)):
            unit = node.args.get("unit")
            word = (unit.name if unit is not None else "").lower()
            if word not in _TRUNC:
                raise ValueError(f"DATE_TRUNC 단위는 hour · day · week · month · year 만 받습니다 -> {node.sql()}")
            return {"fn": "DATE_TRUNC", "unit": _TRUNC[word], "args": [self.node(node.this, scope, aliases)]}
        if isinstance(node, exp.CurrentTimestamp):
            return {"fn": "NOW", "args": []}
        if isinstance(node, exp.CurrentDate):
            return {"fn": "CURRENT_DATE", "args": []}
        if isinstance(node, exp.Window):
            return self._window(node, scope, aliases)
        if isinstance(node, exp.Case):
            return self._case(node, scope, aliases)
        if isinstance(node, exp.Cast):
            raise ValueError(f"형 변환은 날짜(::date)만 받습니다 -> {node.sql()}")
        if isinstance(node, exp.Interval):
            raise ValueError(f"INTERVAL 은 날짜 ± INTERVAL 로만 씁니다 -> {node.sql()}")
        raise ValueError(f"지원하지 않는 식입니다 -> {node.sql()} ({type(node).__name__})")

    def _interval_add(self, node, scope: _Scope, aliases):
        """날짜 ± INTERVAL. UTC 컬럼 + 시차는 현지 시각 컬럼이 된다."""
        if not isinstance(node.expression, exp.Interval):
            return None
        n, unit = _interval(node.expression)
        if isinstance(node, exp.Sub):
            n = -n
        utc = self._utc_column(node.this, scope)
        if utc and unit == "hours":
            ref, hours = utc
            rest = n - hours  # 원본: UTC 값 + n 시간 = 현지 값 + (n - 시차) 시간
            return {"col": ref} if rest == 0 else {"fn": "ADD_HOURS", "args": [{"col": ref}, {"num": _whole(rest)}]}
        base = self.node(node.this, scope, aliases)
        return {"fn": _INTERVAL_FN[unit], "args": [base, {"num": _whole(n)}]}

    def _case(self, node: exp.Case, scope: _Scope, aliases) -> dict:
        """CASE WHEN 조건 THEN 값 … ELSE 값 END. CASE x WHEN 1 THEN … 은 x = 1 조건으로 옮긴다."""
        subject = node.this
        whens = []
        for branch in node.args.get("ifs") or []:
            if subject is not None:
                cond = [self._compare(subject, "=", branch.this, scope, aliases)]
            else:
                cond = self.conditions(branch.this, scope, aliases)
            whens.append({"if": cond, "then": self.node(branch.args["true"], scope, aliases)})
        out = {"fn": "CASE", "when": whens}
        if node.args.get("default") is not None:
            out["else"] = self.node(node.args["default"], scope, aliases)
        return out

    def _window(self, node: exp.Window, scope: _Scope, aliases) -> dict:
        if not isinstance(node.this, exp.RowNumber):
            raise ValueError(f"윈도 함수는 ROW_NUMBER() 만 받습니다 -> {node.this.sql()}")
        if node.args.get("spec") is not None:
            raise ValueError("윈도 범위(ROWS/RANGE)는 지원하지 않습니다")
        out = {"fn": "ROW_NUMBER", "args": []}
        part = node.args.get("partition_by") or []
        if part:
            out["partition_by"] = []
            for p in part:
                out["partition_by"].append(self.node(p, scope, aliases))
        order = node.args.get("order")
        if order is None:
            raise ValueError("ROW_NUMBER() 에는 ORDER BY 가 필요합니다")
        out["order_by"] = []
        for o in order.expressions:
            out["order_by"].append({"expr": self.node(o.this, scope, aliases), "dir": "desc" if o.args.get("desc") else "asc"})
        return out


class _NotLiteral:
    pass


_NOT_LITERAL = _NotLiteral()


def _number(text):
    value = float(text)
    return int(value) if value.is_integer() and "." not in str(text) else value


def _whole(n):
    return int(n) if float(n).is_integer() else n


def _interval(node: exp.Interval) -> tuple[float, str]:
    """INTERVAL '9 hours' · INTERVAL '9' HOUR → (9, 'hours')"""
    amount = str(node.this.this if isinstance(node.this, exp.Literal) else node.this).strip()
    unit = node.args.get("unit")
    word = (unit.name if unit is not None else "").lower()
    if not word and " " in amount:
        amount, word = amount.split(None, 1)
    word = word.lower().rstrip("s")
    try:
        n = float(amount)
    except ValueError:
        raise ValueError(f"INTERVAL 값을 읽을 수 없습니다 -> {node.sql()}") from None
    if word == "hour":
        return n, "hours"
    if word == "day":
        return n, "days"
    if word == "minute":
        return n, "minutes"
    raise ValueError(f"INTERVAL 은 분(minute)·시간(hour)·일(day) 단위만 받습니다 -> {node.sql()}")


def _moment(node) -> datetime.datetime | None:
    """날짜·시각 상수면 그 시각. '2026-10-01' · TIMESTAMP '…' · '…'::timestamp - INTERVAL '9 hours' 도 상수다."""
    if isinstance(node, str):
        return parse_datetime(node)
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, (exp.Add, exp.Sub)) and isinstance(node.expression, exp.Interval):
        base = _moment(node.this)
        if base is None:
            return None
        n, unit = _interval(node.expression)
        delta = datetime.timedelta(**{unit: n})
        return base + delta if isinstance(node, exp.Add) else base - delta
    if isinstance(node, exp.Cast):
        node = node.this
    if isinstance(node, exp.Literal) and node.is_string:
        return parse_datetime(node.this)
    return None


def _split(node, cls) -> list:
    """a AND b AND c → [a, b, c]"""
    if isinstance(node, cls):
        return _split(node.this, cls) + _split(node.expression, cls)
    return [node]


def _unalias(node):
    return node.unalias() if isinstance(node, exp.Alias) else node


def _strip_div_guards(left: dict, right: dict) -> tuple[dict, dict]:
    """DIV 는 스스로 `* 1.0` 과 `NULLIF(…, 0)` 를 붙이니, SQL 에 적힌 같은 장치는 뺀다 (뜻은 같다)."""
    if left.get("fn") == "MUL" and len(left["args"]) == 2 and left["args"][1] == {"num": 1}:
        left = left["args"][0]
    if right.get("fn") == "NULLIF" and right["args"][1] == {"num": 0}:
        right = right["args"][0]
    return left, right
