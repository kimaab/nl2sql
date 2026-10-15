"""SQL 로 적은 계산식을 식 트리로 푼다 (계산식 등록 화면의 'SQL 로 입력').

식 하나 (ABS(MAX(uman_time) - MIN(uman_time))) 나 SELECT 전체를 받는다.
SELECT 는 (서브쿼리 · WITH 를 따라) 가장 안쪽 집계 질의의 SELECT 식을 계산식 후보로, 그 GROUP BY 를 디멘젼 후보로 낸다.
바깥 질의의 집계(날짜별 SUM 등)는 질문이 정할 몫이라 계산식이 아니라 참고로만 돌려준다.
다른 계산식은 [계산식 이름] 으로 적는다.
"""

import re

import sqlglot
from sqlglot import exp

from compiler import GRAINS, by_text, measure_has_aggregate, measure_text

DIALECTS = {"postgresql": "postgres", "mysql": "mysql", "oracle": "oracle"}
_REF = re.compile(r"\[([^\]]+)\]")
_AGGREGATES = {exp.Sum: "SUM", exp.Avg: "AVG", exp.Min: "MIN", exp.Max: "MAX"}
_ARITHMETIC = {exp.Add: "ADD", exp.Sub: "SUB", exp.Mul: "MUL", exp.Div: "DIV"}
_GRAIN_WORDS = {g: g for g in GRAINS} | {"hh24": "hour", "hh": "hour", "dd": "day", "mm": "month", "yyyy": "year", "q": "quarter", "iw": "week"}


def parse_formula_sql(sql: str, driver: str = "postgresql") -> dict:
    """→ {formulas: [{name, expr, text, level}], dimensions: [묶음], table, outer: [바깥 집계 설명]}"""
    text = str(sql or "").strip().rstrip(";")
    if not text:
        raise ValueError("SQL 이 비어 있습니다")
    refs: dict = {}

    def placeholder(m):
        key = f"__formula_{len(refs)}__"
        refs[key] = m.group(1).strip()
        return key

    text = _REF.sub(placeholder, text)
    dialect = DIALECTS.get(driver, "postgres")
    is_select = re.match(r"^\s*(select|with)\b", text, re.IGNORECASE) is not None
    try:
        trees = [t for t in sqlglot.parse(text if is_select else f"SELECT {text}", read=dialect) if t is not None]
    except sqlglot.errors.ParseError as error:
        raise ValueError(f"SQL 을 읽을 수 없습니다 -> {str(error).splitlines()[0]}") from None
    if len(trees) != 1:
        raise ValueError(f"SQL 문이 {len(trees)}개입니다 — 한 번에 하나만 넣으십시오 (; 로 나뉜 문)")
    tree = trees[0]
    if not isinstance(tree, exp.Select):
        raise ValueError("SELECT 문이나 식 하나만 받습니다 (UNION 은 풀지 않습니다)")
    parser = _Parser(refs, {cte.alias: cte.this for cte in tree.find_all(exp.CTE)})
    if not is_select:
        if len(tree.expressions) != 1:
            raise ValueError("식은 하나만 적으십시오 — 여러 개면 SELECT 문으로 적으십시오")
        expr = parser.node(tree.expressions[0].unalias())
        return {
            "formulas": [_formula(None, expr)],
            "dimensions": [],
            "table": None,
            "outer": [],
        }
    return parser.select(tree)


def _formula(name, expr) -> dict:
    return {
        "name": name,
        "expr": expr,
        "text": measure_text(expr),
        "level": "aggregate" if measure_has_aggregate(expr) else "row",
    }


class _Parser:
    def __init__(self, refs: dict, ctes: dict | None = None):
        self.refs = refs
        self.ctes = ctes or {}

    def select(self, query: exp.Select) -> dict:
        """가장 안쪽 집계 질의까지 내려간다. 바깥 질의의 식은 안쪽 별칭을 [계산식] 으로 가리키는 참고가 된다."""
        source = query.args.get("from_") or query.args.get("from")
        inner = source.this if source is not None else None
        if isinstance(inner, exp.Subquery):
            inner = inner.this
        elif isinstance(inner, exp.Table) and not inner.db and inner.name in self.ctes:
            inner = self.ctes[inner.name]
        if isinstance(inner, exp.Select):
            found = self.select(inner)
            names = {f["name"] for f in found["formulas"] if f["name"]}
            outer = []
            for item in query.expressions:
                alias = item.alias_or_name
                body = item.unalias()
                if not _has_agg(body) or self.dimension(body) is not None:
                    continue
                expr = self.node(body, aliases=names)
                outer.append(f"{alias}: {measure_text(expr)}")
            group = self.group_by(query)
            if group:
                outer.append("묶음: " + ", ".join(by_text(g) for g in group))
            found["outer"] = outer + found["outer"]
            return found

        table = inner.name if isinstance(inner, exp.Table) else None
        group = self.group_by(query)
        group_keys = {by_text(g) for g in group}
        formulas = []
        for item in query.expressions:
            body = item.unalias()
            if isinstance(body, exp.Star):
                continue
            dim = self.dimension(body)
            if dim is not None and by_text(dim) in group_keys:
                continue
            if isinstance(body, exp.Column):
                continue
            expr = self.node(body)
            name = item.alias if isinstance(item, exp.Alias) else None
            formulas.append(_formula(name, expr))
        if not formulas:
            raise ValueError("계산식으로 쓸 식이 없습니다 — SELECT 에 컬럼만 있습니다")
        return {"formulas": formulas, "dimensions": group, "table": table, "outer": []}

    def group_by(self, query: exp.Select) -> list:
        group = query.args.get("group")
        if group is None:
            return []
        out = []
        for g in group.expressions:
            target = g
            if isinstance(g, exp.Literal) and not g.is_string:
                target = query.expressions[int(g.this) - 1].unalias()
            elif isinstance(g, exp.Column) and not g.table:
                for item in query.expressions:
                    if isinstance(item, exp.Alias) and item.alias == g.name:
                        target = item.unalias()
            dim = self.dimension(target)
            if dim is None:
                raise ValueError(f"묶음을 디멘젼으로 바꿀 수 없습니다 -> {g.sql()}")
            if dim not in out:
                out.append(dim)
        return out

    def dimension(self, node):
        """컬럼 · DATE(컬럼) · CAST(컬럼 AS DATE) · DATE_TRUNC('month', 컬럼) → 디멘젼. 시차 보정(+ INTERVAL)은 뗀다."""
        if isinstance(node, exp.Paren):
            return self.dimension(node.this)
        if isinstance(node, exp.Column):
            return node.name
        grain = None
        if isinstance(node, (exp.Date, exp.TsOrDsToDate)) or (
            isinstance(node, exp.Cast) and node.to.this == exp.DataType.Type.DATE
        ):
            grain, target = "day", node.this
        elif isinstance(node, (exp.TimestampTrunc, exp.DateTrunc)):
            unit = node.args.get("unit")
            word = (unit.name if unit is not None else "").lower()
            grain, target = _GRAIN_WORDS.get(word), node.this
        elif isinstance(node, exp.Cast):
            return self.dimension(node.this)
        else:
            return None
        column = _column(target)
        if grain is None or column is None:
            return None
        return {"field": column, "grain": grain}

    def node(self, node, aliases=frozenset()) -> dict:
        if isinstance(node, exp.Paren):
            return self.node(node.this, aliases)
        if isinstance(node, exp.Column):
            if node.name in self.refs:
                return {"formula": self.refs[node.name]}
            if node.name in aliases:
                return {"formula": node.name}
            return {"col": f"{node.table}.{node.name}" if node.table else node.name}
        if isinstance(node, exp.Literal) and not node.is_string:
            value = float(node.this)
            return {"num": int(value) if value.is_integer() else value}
        if isinstance(node, exp.Neg):
            return {"fn": "SUB", "args": [{"num": 0}, self.node(node.this, aliases)]}
        for cls, name in _ARITHMETIC.items():
            if isinstance(node, cls):
                left = self.node(node.this, aliases)
                right = self.node(node.expression, aliases)
                # 같은 연산이 이어지면 하나로 (a - b - c → SUB(a, b, c)). DIV 는 두 값만.
                if name != "DIV" and left.get("fn") == name and not left.get("by"):
                    return {"fn": name, "args": left["args"] + [right]}
                if name in ("ADD", "MUL") and right.get("fn") == name and not right.get("by"):
                    return {"fn": name, "args": [left] + right["args"]}
                return {"fn": name, "args": [left, right]}
        for cls, name in _AGGREGATES.items():
            if isinstance(node, cls):
                return {"fn": name, "args": [self.node(node.this, aliases)]}
        if isinstance(node, exp.Count):
            arg = node.this
            if arg is None or isinstance(arg, exp.Star):
                return {"fn": "COUNT", "args": []}
            if isinstance(arg, exp.Distinct):
                return {"fn": "COUNT_DISTINCT", "args": [self.node(arg.expressions[0], aliases)]}
            return {"fn": "COUNT", "args": [self.node(arg, aliases)]}
        if isinstance(node, exp.Abs):
            return {"fn": "ABS", "args": [self.node(node.this, aliases)]}
        if isinstance(node, exp.Round):
            args = [self.node(node.this, aliases)]
            if node.args.get("decimals") is not None:
                args.append(self.node(node.args["decimals"], aliases))
            return {"fn": "ROUND", "args": args}
        for cls, name in ((exp.Coalesce, "COALESCE"), (exp.Greatest, "GREATEST"), (exp.Least, "LEAST")):
            if isinstance(node, cls):
                rest = node.expressions or []
                return {"fn": name, "args": [self.node(a, aliases) for a in [node.this, *rest]]}
        if isinstance(node, exp.Nullif) and isinstance(node.expression, exp.Literal) and node.expression.this == "0":
            return self.node(node.this, aliases)  # 0 나눗셈 막기는 컴파일러가 붙인다
        raise ValueError(f"계산식에서 지원하지 않는 식입니다 -> {node.sql()}")


def _column(node):
    """시차 보정을 뗀 컬럼 이름. collect_datetime + INTERVAL '9 hours' → collect_datetime"""
    while isinstance(node, (exp.Paren, exp.Add, exp.Sub, exp.Cast)):
        node = node.this
    return node.name if isinstance(node, exp.Column) else None


def _has_agg(node) -> bool:
    return any(isinstance(n, tuple(_AGGREGATES) + (exp.Count,)) for n in node.walk())

