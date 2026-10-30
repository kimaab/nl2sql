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

from compiler import parse_datetime, temporal_kind, type_family

DIALECTS = {"postgresql": "postgres", "mysql": "mysql", "oracle": "oracle"}
_AGGREGATES = {exp.Sum: "SUM", exp.Avg: "AVG", exp.Min: "MIN", exp.Max: "MAX"}
_ARITHMETIC = {exp.Add: "ADD", exp.Sub: "SUB", exp.Mul: "MUL", exp.Div: "DIV"}
_SCALARS = {exp.Coalesce: "COALESCE", exp.Greatest: "GREATEST", exp.Least: "LEAST"}
_COMPARE = {exp.EQ: "=", exp.NEQ: "!=", exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<="}
_FLIP = {"=": "=", "!=": "!=", ">": "<", ">=": "<=", "<": ">", "<=": ">="}
_INTERVAL_FN = {"seconds": "ADD_SECONDS", "minutes": "ADD_MINUTES", "hours": "ADD_HOURS", "days": "ADD_DAYS"}
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
            if with_.args.get("recursive") and isinstance(cte.this, exp.Union):
                parser.recursive[cte.alias] = cte  # 계층 — AST 의 tree 로 옮긴다
        # 두 번 이상 쓰는 WITH 는 AST 의 with 로 둔다 — 펼치면 같은 집계를 두 번 한다
        counts: dict = {}
        for table in tree.find_all(exp.Table):
            if not table.db and table.name in parser.ctes and table.name not in parser.recursive:
                counts[table.name] = counts.get(table.name, 0) + 1
        for name, n in counts.items():
            if n > 1:
                parser.shared.add(name)
    ast = parser.select(tree)
    if parser.withs:
        ast = {"with": parser.withs, **ast}
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
        self.zones: dict = {}  # 테이블 → {UTC 컬럼: 행마다 시차를 담은 컬럼} (compiler.py 와 같은 조건)
        for t in contract.get("tables", []):
            cols = {}
            types = {}
            for c in t.get("columns", []):
                types[c["name"]] = c.get("type", "")
            for c in t.get("columns", []):
                hours = c.get("utc_offset") or 0
                if hours and temporal_kind(c.get("type", ""), driver) != "datetime":
                    hours = 0
                cols[c["name"]] = int(hours)
                zone = c.get("utc_offset_column") or ""
                if hours and zone in types and type_family(types[zone]) == "number":
                    self.zones.setdefault(t["name"], {})[c["name"]] = zone
            self.tables[t["name"].lower()] = (t["name"], cols)
        self.ctes: dict = {}
        self.shared: set = set()  # AST 의 with 로 둘 WITH 이름 (JOIN 에 쓰거나 두 번 이상 쓴다)
        self.withs: list[dict] = []  # [{"name", "query"}] — 먼저 필요한 것이 앞에 온다
        self.with_columns: dict = {}
        self.opening: set = set()
        self.recursive: dict = {}  # 맨 바깥 WITH RECURSIVE 이름 → CTE 식
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
            if node.name in self.recursive and not node.db:
                return self._tree_source(self.recursive[node.name], node.alias or node.name, scope, own_columns=True)
            if node.name in self.ctes and not node.db:
                alias = node.alias or node.name
                if first and node.name not in self.shared:  # 한 번만 쓰면 그 자리의 하위 쿼리로 펼친다
                    inner = self.select(self.ctes[node.name])
                    scope.sources[alias] = {"name": alias, "columns": _subquery_columns(inner), "table": False}
                    return {"query": inner, "as": alias}
                scope.sources[alias] = {"name": alias, "columns": self._with(node.name), "table": False}
                return node.name if alias == node.name else {"table": node.name, "as": alias}
            real, columns = self._lookup(node)
            # 별칭이 없으면 SQL 처럼 스키마를 뗀 테이블 이름으로 가리킨다 (sales.orders → orders.amount)
            alias = node.alias or (node.name if "." in real else real)
            for s in scope.sources.values():
                if s["name"] == real:
                    raise ValueError(f"같은 테이블을 두 번 쓸 수 없습니다 (셀프 조인) -> {real}")
            scope.sources[alias] = {"name": real, "columns": columns, "table": True}
            return real
        if isinstance(node, exp.Subquery):
            if not first and not node.alias:
                raise ValueError("JOIN 하는 하위 쿼리에는 별칭이 있어야 합니다 (예: JOIN (…) top ON …)")
            alias = node.alias or "sub"
            with_ = node.this.args.get("with_") or node.this.args.get("with") if isinstance(node.this, exp.Select) else None
            if with_ is not None and with_.args.get("recursive"):
                return self._wrapped_tree(node, with_, alias, scope)
            inner = self.select(node.this)
            scope.sources[alias] = {"name": alias, "columns": _subquery_columns(inner), "table": False}
            return {"query": inner, "as": alias}
        raise ValueError(f"FROM 에 쓸 수 없는 것입니다 -> {node.sql()}")

    def _lookup(self, node: exp.Table) -> tuple[str, dict]:
        """SQL 의 테이블 → (메타데이터 이름, {컬럼: 시차})"""
        written = f"{node.db}.{node.name}" if node.db else node.name
        found = None
        if node.db:  # 기본 스키마가 아닌 테이블은 '스키마.테이블' 이름으로 등록돼 있다
            found = self.tables.get(written.lower())
        if found is None:
            found = self.tables.get(node.name.lower())
        if found is None:
            raise ValueError(f"메타데이터에 없는 테이블입니다 -> {written} (동기화했는지 확인하십시오)")
        return found

    # ── 계층 (재귀 WITH → tree) ──────────────────────────────────────────

    def _wrapped_tree(self, node: exp.Subquery, with_, alias: str, scope: _Scope) -> dict:
        """(WITH RECURSIVE t AS (…) SELECT 테이블 컬럼 … FROM 테이블 JOIN t …) 별칭 — 질문 처리가 만든 계층 SQL 모양"""
        found = []
        for cte in with_.expressions:
            if isinstance(cte.this, exp.Union):
                found.append(cte)
        if len(found) != 1:
            raise ValueError(f"하위 쿼리 {alias} 의 재귀 WITH 는 계층 하나만 받습니다")
        body_from = node.this.args.get("from_") or node.this.args.get("from")
        spec = self._tree_spec(found[0])
        if body_from is None or not isinstance(body_from.this, exp.Table) or self._lookup(body_from.this)[0] != spec["table"]:
            raise ValueError(
                f"하위 쿼리 {alias} 는 재귀 WITH 결과를 {spec['table']} 에 붙인 모양이어야 합니다 "
                f"(WITH RECURSIVE 를 맨 앞에 두고 그 이름을 FROM 에 쓰는 모양도 됩니다)"
            )
        return self._tree_source(found[0], alias, scope, own_columns=False, spec=spec)

    def _tree_source(self, cte, alias: str, scope: _Scope, own_columns: bool, spec: dict | None = None) -> dict:
        """계층을 FROM 소스로. 컬럼은 계층 테이블의 컬럼 + depth 다."""
        spec = spec or self._tree_spec(cte)
        columns = dict(self.tables[spec["table"].lower()][1])
        columns["depth" if "depth" not in columns else "tree_depth"] = 0
        if own_columns:  # WITH 이름을 그대로 쓴다 — 그 컬럼이 테이블 컬럼 이름이어야 tree 로 옮길 수 있다
            for name in _cte_names(cte):
                if _find(columns, name) is None:
                    raise ValueError(
                        f"재귀 WITH {cte.alias} 의 컬럼은 {spec['table']} 의 컬럼 이름 그대로 적어야 합니다 -> {name} "
                        f"(단계 수는 depth)"
                    )
        scope.sources[alias] = {"name": alias, "columns": columns, "table": False}
        return {"tree": spec, "as": alias}

    def _tree_spec(self, cte) -> dict:
        """재귀 WITH → {"table", "key", "parent", "start", "direction"}

        시작 SELECT: 테이블 하나에서 키 컬럼(첫 항목)을 WHERE 로 고른다.
        다음 단계 SELECT: 같은 테이블을 WITH 이름과 조인한다 — 테이블.parent = WITH.키 이면 아래로(down),
        테이블.키 = WITH.parent 이면 위로(up). 단계 수 제한(WHERE depth < n)은 컴파일러가 둔다.
        """
        name = cte.alias
        union = cte.this
        shape = f"재귀 WITH {name} 은 '시작 SELECT UNION ALL 다음 단계 SELECT' 모양이어야 합니다"
        if not isinstance(union, exp.Union) or not isinstance(union.this, exp.Select) or not isinstance(union.expression, exp.Select):
            raise ValueError(shape)
        anchor, step = union.this, union.expression
        anchor_from = anchor.args.get("from_") or anchor.args.get("from")
        if anchor_from is None or not isinstance(anchor_from.this, exp.Table) or anchor.args.get("joins"):
            raise ValueError(
                f"재귀 WITH {name} 의 시작 SELECT 는 테이블 하나에서 WHERE 로 고릅니다 "
                f"(다른 테이블의 값은 WHERE 키 IN (SELECT …) 로 적으십시오)"
            )
        real, columns = self._lookup(anchor_from.this)
        anchor_cols = []
        for e in anchor.expressions:
            col = _strip_text_cast(e.this if isinstance(e, exp.Alias) else e)
            anchor_cols.append(_find(columns, col.name) if isinstance(col, exp.Column) else None)
        key = anchor_cols[0] if anchor_cols else None
        if key is None:
            raise ValueError(f"재귀 WITH {name} 의 시작 SELECT 첫 항목은 {real} 의 키 컬럼이어야 합니다")

        step_from = step.args.get("from_") or step.args.get("from")
        joins = step.args.get("joins") or []
        if step_from is None or not isinstance(step_from.this, exp.Table) or self._lookup(step_from.this)[0] != real or len(joins) != 1:
            raise ValueError(f"재귀 WITH {name} 의 다음 단계 SELECT 는 {real} 를 {name} 과 조인한 모양이어야 합니다")
        join = joins[0]
        if not isinstance(join.this, exp.Table) or join.this.name != name:
            raise ValueError(f"재귀 WITH {name} 의 다음 단계 SELECT 는 {real} 를 {name} 과 조인한 모양이어야 합니다")
        cte_alias = join.this.alias or name
        on = join.args.get("on")
        if not isinstance(on, exp.EQ):
            raise ValueError(f"재귀 WITH {name} 의 조인 조건은 컬럼 = 컬럼 하나입니다")
        own = prev = None
        for side in (_strip_text_cast(on.this), _strip_text_cast(on.expression)):
            if not isinstance(side, exp.Column):
                raise ValueError(f"재귀 WITH {name} 의 조인 조건은 컬럼 = 컬럼 하나입니다")
            if side.table in (cte_alias, name):
                prev = side
            else:
                own = side
        if own is None or prev is None:
            raise ValueError(f"재귀 WITH {name} 의 조인 조건은 {real} 컬럼 = {name} 컬럼 이어야 합니다")
        own_col = _find(columns, own.name)
        names = _cte_names(cte)
        prev_col = None
        for i, n in enumerate(names):
            if n.lower() == prev.name.lower() and i < len(anchor_cols):
                prev_col = anchor_cols[i]
        if own_col is None or prev_col is None:
            raise ValueError(f"재귀 WITH {name} 의 조인 컬럼을 {real} 의 컬럼으로 읽지 못했습니다 -> {on.sql()}")
        if prev_col == key and own_col != key:
            direction, parent = "down", own_col
        elif own_col == key and prev_col != key:
            direction, parent = "up", prev_col
        else:
            raise ValueError(f"재귀 WITH {name} 의 조인은 부모 = 키 모양이어야 합니다 -> {on.sql()}")

        where = anchor.args.get("where")
        if where is None:
            raise ValueError(f"재귀 WITH {name} 의 시작 SELECT 에 WHERE(시작 행 조건)가 없습니다")
        start_scope = _Scope()
        start_alias = anchor_from.this.alias or (anchor_from.this.name if "." in real else real)
        start_scope.sources[start_alias] = {"name": real, "columns": columns, "table": True}
        start = self.conditions(where.this, start_scope)
        return {"table": real, "key": key, "parent": parent, "start": start, "direction": direction}

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
        alias = None
        if isinstance(table, dict) and "table" in table:  # 별칭을 붙인 WITH 이름
            table, alias = table["table"], table["as"]
        on = join.args.get("on")
        if on is None:
            raise ValueError(f"{table} JOIN 에 ON 이 없습니다")
        pairs = []
        for part in _split(on, exp.And):
            left = _strip_text_cast(part.this) if isinstance(part, exp.EQ) else None
            right = _strip_text_cast(part.expression) if isinstance(part, exp.EQ) else None
            if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                raise ValueError(f"JOIN 조건은 컬럼 = 컬럼 만 받습니다 -> {part.sql()}")
            # 타입이 다른 컬럼끼리의 CAST(… AS VARCHAR) 는 벗긴다 — 컴파일러가 다시 맞춘다
            pairs.append([self._qualified(left, scope), self._qualified(right, scope)])
        if isinstance(table, dict) and "tree" in table:  # 계층은 {"tree", "as"} 로 둔다
            return {"tree": table["tree"], "as": table["as"], "type": how, "on": pairs}
        out = {"table": table, "type": how, "on": pairs}
        if alias:
            out["as"] = alias
        return out

    def _with(self, name: str) -> dict:
        """WITH 하나를 AST 의 with 로 옮기고 그 결과 컬럼을 돌려준다 (한 번만 옮긴다)"""
        if name not in self.with_columns:
            if name in self.opening:
                raise ValueError(f"재귀 WITH 는 지원하지 않습니다 -> {name} (계층은 질문으로 물으십시오)")
            self.opening.add(name)
            inner = self.select(self.ctes[name])
            self.opening.discard(name)
            self.withs.append({"name": name, "query": inner})
            self.with_columns[name] = _subquery_columns(inner)
        return self.with_columns[name]

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
                inner = self.select(_unwrap_text_select(node.args["query"]))
                if len(inner["select"]) != 1:
                    raise ValueError(f"IN 의 하위 쿼리는 SELECT 항목이 하나여야 합니다 -> {node.sql()}")
                left = self.node(_strip_text_cast(node.this), scope, aliases)
                return [{"expr": left, "op": "not_in" if negate else "in", "value": {"query": inner}}]
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
        # 저장된 UTC 값과 지금(UTC)의 비교: 컬럼 >= (NOW() AT TIME ZONE 'UTC') - INTERVAL …
        # 또는 (컬럼 AT TIME ZONE 'UTC') >= NOW() - INTERVAL …. AST 에서는 컬럼 >= NOW 기준 값이고, 컴파일러가 UTC 로 맞춘다
        core, wrapped = _at_utc(left)
        utc = self._utc_column(core, scope)
        now = _now_offset(right) if utc and isinstance(right, exp.Expression) else None
        if now is not None and now[0] != wrapped:
            return {"expr": {"col": utc[0]}, "op": op, "value": now[1]}
        if wrapped:
            raise ValueError(f"AT TIME ZONE 'UTC' 는 UTC 컬럼과 NOW() 의 비교에만 씁니다 -> {left.sql()}")
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
        if isinstance(node, exp.Add):
            local = self._zone_shift(node, scope)
            if local is not None:
                return local
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

    def _zone_shift(self, node, scope: _Scope):
        """UTC 컬럼 + (COALESCE(시차 컬럼, h) || ' hours')::INTERVAL → 컬럼 (행마다 보정한 현지 시각).
        시차 컬럼과 h 가 메타데이터의 보정(utc_offset_column, utc_offset)과 같을 때만이다."""
        zone = _zone_hours(node.expression)
        column = _unparen(node.this)
        if zone is None or not isinstance(column, exp.Column):
            return None
        utc = self._utc_column(column, scope)
        if utc is None:
            return None
        alias, src = scope.owner(column)
        name = _find(src["columns"], column.name)
        expected = self.zones.get(src["name"], {}).get(name) if src.get("table") else None
        zone_column, fallback = zone
        zone_alias, _ = scope.owner(zone_column)
        if expected is None or zone_column.name.lower() != expected.lower() or zone_alias != alias:
            raise ValueError(
                f"{name} 의 행마다 시차는 같은 행의 {expected or '시차 컬럼'} 으로만 보정합니다 -> {node.sql()}"
            )
        if fallback != utc[1]:
            raise ValueError(f"{name} 의 시차 컬럼이 비었을 때 값은 {utc[1]} 시간입니다 -> {node.sql()}")
        return {"col": utc[0]}

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
    if word == "second":
        return n, "seconds"
    raise ValueError(f"INTERVAL 은 초(second)·분(minute)·시간(hour)·일(day) 단위만 받습니다 -> {node.sql()}")


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


_TEXT_TYPES = (
    exp.DataType.Type.VARCHAR,
    exp.DataType.Type.CHAR,
    exp.DataType.Type.TEXT,
    exp.DataType.Type.NVARCHAR,
    exp.DataType.Type.NCHAR,
)


def _unparen(node):
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _is_utc_zone(node) -> bool:
    """AT TIME ZONE 'UTC' 인가"""
    node = _unparen(node)
    if not isinstance(node, exp.AtTimeZone):
        return False
    zone = node.args.get("zone")
    return isinstance(zone, exp.Literal) and zone.is_string and zone.this.upper() == "UTC"


def _at_utc(node) -> tuple:
    """(x AT TIME ZONE 'UTC') → (x, True). 아니면 (node, False)"""
    if _is_utc_zone(node):
        return _unparen(node).this, True
    return node, False


def _now_offset(node):
    """NOW() ± INTERVAL … → (UTC 로 맞췄는가, AST 식). NOW() AT TIME ZONE 'UTC' 면 True. 아니면 None"""
    node = _unparen(node)
    if isinstance(node, (exp.Add, exp.Sub)) and isinstance(node.expression, exp.Interval):
        inner = _now_offset(node.this)
        if inner is None:
            return None
        n, unit = _interval(node.expression)
        if isinstance(node, exp.Sub):
            n = -n
        return inner[0], {"fn": _INTERVAL_FN[unit], "args": [inner[1], {"num": _whole(n)}]}
    if isinstance(node, exp.CurrentTimestamp):
        return False, {"fn": "NOW", "args": []}
    if _is_utc_zone(node) and isinstance(_unparen(node.this), exp.CurrentTimestamp):
        return True, {"fn": "NOW", "args": []}
    return None


def _zone_hours(node):
    """(COALESCE(시차 컬럼, h) || ' hours')::INTERVAL → (시차 컬럼, h). 아니면 None"""
    node = _unparen(node)
    if not isinstance(node, exp.Cast) or node.to.this != exp.DataType.Type.INTERVAL:
        return None
    text = _unparen(node.this)
    if not isinstance(text, exp.DPipe):
        return None
    unit = _unparen(text.expression)
    if not isinstance(unit, exp.Literal) or not unit.is_string or unit.this.strip().lower() not in ("hours", "hour"):
        return None
    zone = _unparen(text.this)
    if not isinstance(zone, exp.Coalesce) or len(zone.expressions) != 1:
        return None
    column = _unparen(zone.this)
    fallback = _unparen(zone.expressions[0])
    if not isinstance(column, exp.Column) or not isinstance(fallback, exp.Literal) or fallback.is_string:
        return None
    return column, _number(fallback.this)


def _strip_text_cast(node):
    """문자로 바꾸는 형 변환(CAST … AS VARCHAR, ::text, TO_CHAR)을 벗긴다.
    타입이 다른 컬럼끼리의 비교에 붙는 것이라 AST 에는 두지 않는다 — 컴파일러가 다시 맞춘다."""
    while True:
        if isinstance(node, exp.Paren):
            node = node.this
        elif isinstance(node, exp.Cast) and node.to.this in _TEXT_TYPES:
            node = node.this
        elif type(node).__name__ == "ToChar" and not node.args.get("format"):
            node = node.this
        else:
            return node


def _unwrap_text_select(query):
    """IN 하위 쿼리의 SELECT 항목에 붙은 문자 형 변환을 벗긴다 (컴파일러가 비교 타입을 다시 맞춘다).
    컴파일러가 만든 SELECT CAST(x.c AS VARCHAR) FROM (안쪽 SELECT) x 는 안쪽 SELECT 로 되돌린다."""
    select = query.this if isinstance(query, exp.Subquery) else query
    if not isinstance(select, exp.Select) or len(select.expressions) != 1:
        return select
    expr = select.expressions[0]
    inner = expr.this if isinstance(expr, exp.Alias) else expr
    col = _strip_text_cast(inner)
    if col is expr or not isinstance(col, exp.Column):
        return select
    source = select.args.get("from_") or select.args.get("from")
    wrapped = (
        source is not None
        and isinstance(source.this, exp.Subquery)
        and not (select.args.get("joins") or select.args.get("where") or select.args.get("group"))
        and source.this.alias == col.table
    )
    if wrapped:
        return source.this.this
    if col is inner:
        return select
    # 예: u.group_id IN (SELECT id::text FROM grp)
    stripped = select.copy()
    stripped.set("expressions", [col.copy()])
    return stripped


def _cte_names(cte) -> list[str]:
    """WITH 의 결과 컬럼 이름 (컬럼 목록이 있으면 그것, 없으면 첫 SELECT 의 이름)"""
    names = []
    alias = cte.args.get("alias")
    columns = alias.columns if alias is not None else []
    if columns:
        for c in columns:
            names.append(c.name)
        return names
    first = cte.this.this if isinstance(cte.this, exp.Union) else cte.this
    for e in first.expressions:
        names.append(e.alias_or_name)
    return names


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
