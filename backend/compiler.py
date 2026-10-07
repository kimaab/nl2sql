import calendar
import datetime
import re

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?$")


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


_NUMERIC_TYPES = (
    "int", "integer", "bigint", "smallint", "tinyint", "mediumint", "numeric", "decimal",
    "number", "float", "double", "real", "serial", "bigserial", "smallserial", "binary_float",
    "binary_double",
)


def is_numeric_type(data_type: str) -> bool:
    """숫자 컬럼인지 판정한다. 'int(11) unsigned', 'NUMBER(10,2)' 같은 표기도 받는다."""
    text = (data_type or "").strip().lower()
    head = re.split(r"[\s(]", text, maxsplit=1)[0] if text else ""
    return head in _NUMERIC_TYPES or text.startswith("double precision")


def parse_temporal(value, where: str) -> tuple[str, str]:
    """날짜 값을 (종류, 정규화된 문자열) 로. 형식과 달력 유효성을 함께 본다."""
    text = str(value).strip()
    if _DATE_RE.match(text):
        kind, iso = "date", text
    elif _DATETIME_RE.match(text):
        kind, iso = "datetime", text.replace("T", " ")
    else:
        raise ValueError(
            f"{where} 는 날짜 컬럼입니다. 값은 'YYYY-MM-DD' 또는 "
            f"'YYYY-MM-DD HH:MM:SS' 형식이어야 합니다 -> {value!r}"
        )
    try:
        datetime.date.fromisoformat(iso[:10])
    except ValueError:
        raise ValueError(f"{where} 에 없는 날짜입니다 -> {value!r}") from None
    return kind, iso


def next_day(iso_date: str) -> str:
    d = datetime.date.fromisoformat(iso_date) + datetime.timedelta(days=1)
    return d.isoformat()


FILTER_SOURCES = ("literal", "relative", "question")
_RELATIVE_RE = re.compile(r"^([+-]?\d+)([dmy])$")
JOIN_TYPES = {"inner": "INNER JOIN", "left": "LEFT JOIN"}
GRAINS = ("day", "week", "month", "quarter", "year")
# 지표 정의에서만 쓰는 시계열 집계. 행 순서(series)가 있어야 뜻이 있어서 모델 AST 로는 받지 않는다.
SERIES_FUNCTIONS = {
    "DELTA_SUM": "누적값 증가분 합 — 직전 행보다 늘어난 만큼만 더한다(리셋으로 줄면 0)",
    "CHANGE_COUNT": "상태 발생 횟수 — 기준값(정상)에서 다른 값으로 바뀐 순간만 센다",
}
# 지표 정의에서 온 필터 표시. 모델이 이 키를 보내도 normalize 를 거친 filters 와 섞이지 않게
# 사람이 쓰지 않을 이름으로 둔다.
METRIC_FILTER = "__metric_filter__"
MAX_LIMIT = 1000


def _shift_months(d: datetime.date, n: int) -> datetime.date:
    total = d.year * 12 + (d.month - 1) + n
    y, m = divmod(total, 12)
    m += 1
    # 말일 보정 — 3월 31일의 한 달 전은 2월 28(29)일이다.
    return datetime.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def resolve_relative(value, where: str, today: datetime.date | None = None) -> str:
    """'-7d' 같은 상대 기간을 오늘 기준 날짜로 편다.

    DB 함수(SYSDATE-7 등)가 아니라 계산된 날짜를 쓴다. 드라이버마다 다른
    날짜 산술 문법을 늘리지 않아도 되고, SQL 을 읽는 사람이 어느 구간인지
    바로 알 수 있다.
    """
    text = str(value or "").strip().lower()
    m = _RELATIVE_RE.match(text)
    if not m:
        raise ValueError(
            f"{where} 의 상대 기간 형식이 잘못됐습니다 -> {value!r} "
            "(예: -7d 이레 전, -1m 한 달 전, -1y 일 년 전, 0d 오늘)"
        )
    n, unit = int(m.group(1)), m.group(2)
    base = today or datetime.date.today()
    if unit == "d":
        return (base + datetime.timedelta(days=n)).isoformat()
    return _shift_months(base, n if unit == "m" else n * 12).isoformat()


def _find(name, options):
    """대소문자 무시하고 이름 찾기"""
    if not isinstance(name, str):
        return None
    for candidate in options:
        if candidate.lower() == name.lower():
            return candidate
    return None


def build_scope(base: str, joins: list, tables: dict) -> tuple[dict, list]:
    """지표의 기본 테이블과 조인을 검증해 (scope, 조인 목록) 을 돌려준다.

    scope 는 {테이블: [컬럼...]} 로 기본 테이블이 맨 앞이다. 조인 목록은
    (조인 종류, 테이블, [((왼쪽 테이블, 컬럼), (오른쪽 테이블, 컬럼)), ...]).
    ON 은 한 쪽이 이번에 붙는 테이블, 다른 쪽이 앞서 나온 테이블이어야 한다 —
    그래야 조인 순서대로 읽었을 때 모든 조건이 이미 나온 테이블만 가리킨다.
    """
    resolved_base = _find(base, tables)
    if resolved_base is None:
        raise ValueError(f"조회할 수 없는 테이블입니다 -> {base!r} (사용 가능: {', '.join(tables)})")
    scope = {resolved_base: tables[resolved_base]}
    out = []
    for join in joins or []:
        if not isinstance(join, dict):
            raise ValueError(f"조인 항목은 객체여야 합니다 -> {join!r}")
        table = _find(join.get("table"), tables)
        if table is None:
            raise ValueError(f"조인할 수 없는 테이블입니다 -> {join.get('table')!r}")
        if table in scope:
            raise ValueError(f"같은 테이블을 두 번 조인할 수 없습니다 -> {table}")
        kind = str(join.get("type") or "inner").lower()
        if kind not in JOIN_TYPES:
            raise ValueError(f"조인 종류는 {', '.join(JOIN_TYPES)} 중 하나여야 합니다 -> {kind!r}")
        pairs = join.get("on") or []
        if not pairs:
            raise ValueError(f"{table} 조인에 ON 조건이 없습니다")
        earlier = dict(scope)
        scope[table] = tables[table]
        rendered = []
        for pair in pairs:
            if not isinstance(pair, dict):
                raise ValueError(f"ON 항목은 객체여야 합니다 -> {pair!r}")
            left = pair.get("left", "")
            right = pair.get("right", "")
            if not left or not right:
                raise ValueError(f"ON 항목에 left와 right가 필요합니다 -> {pair!r}")
            left_table, left_col = _parse_ref(left)
            right_table, right_col = _parse_ref(right)
            left_resolved = _find(left_table, earlier) if left_table else next(iter(earlier))
            right_resolved = _find(right_table, scope) if right_table else table
            if left_resolved is None:
                raise ValueError(f"ON 조건의 왼쪽 테이블을 찾을 수 없습니다 -> {left!r}")
            if right_resolved is None:
                raise ValueError(f"ON 조건의 오른쪽 테이블을 찾을 수 없습니다 -> {right!r}")
            left_cols = [c for c in tables[left_resolved] if c.lower() == left_col.lower()]
            right_cols = [c for c in tables[right_resolved] if c.lower() == right_col.lower()]
            if not left_cols:
                raise ValueError(f"ON 조건의 왼쪽 컬럼을 찾을 수 없습니다 -> {left_resolved}.{left_col}")
            if not right_cols:
                raise ValueError(f"ON 조건의 오른쪽 컬럼을 찾을 수 없습니다 -> {right_resolved}.{right_col}")
            rendered.append(((left_resolved, left_cols[0]), (right_resolved, right_cols[0])))
        out.append((kind, table, rendered))
    return scope, out


def _parse_ref(ref: str) -> tuple[str, str]:
    """'테이블.컬럼' 형식을 파싱"""
    parts = str(ref).split(".")
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    if len(parts) == 1:
        return "", parts[0].strip()
    raise ValueError(f"컬럼 참조 형식이 잘못됐습니다 -> {ref!r}")


def resolve_ref(ref: str, scope: dict, base: str, label: str = "조회할 수 없는 컬럼입니다") -> tuple[str, str]:
    """컬럼 참조를 (테이블, 컬럼)으로 해석"""
    ref_table, ref_col = _parse_ref(ref)
    if ref_table:
        resolved = _find(ref_table, scope)
        if resolved is None:
            raise ValueError(f"조인에 없는 테이블입니다 -> {ref_table!r} (이 조회의 테이블: {', '.join(scope)})")
        table = resolved
    else:
        table = base
    cols = [c for c in scope[table] if c.lower() == ref_col.lower()]
    if not cols:
        raise ValueError(f"{label} -> {table}.{ref_col}")
    return table, cols[0]


# ---------------------------------------------------------------- 별칭 정규화

_TOP_KEY_ALIASES = {
    "table": "target_table", "from": "target_table", "metric_name": "metric",
    "where": "filters", "filter": "filters", "conditions": "filters",
    "group": "group_by", "groupby": "group_by", "group_by_columns": "group_by",
    "order": "order_by", "orderby": "order_by", "sort": "order_by", "sort_by": "order_by",
    "top": "limit", "max_rows": "limit", "join": "joins", "metrics": "metric",
    "select": "columns", "fields": "columns",
    "aggregation": "aggregations", "aggs": "aggregations", "aggregates": "aggregations",
}
_ITEM_KEY_ALIASES = {
    "column": "field", "col": "field", "name": "field",
    "op": "operator", "comparison": "operator", "comparator": "operator",
    "values": "value",
    "func": "function", "agg": "function", "fn": "function", "aggregate": "function",
    "as": "alias",
    "dir": "direction", "order": "direction", "sort": "direction",
    "or": "any_of", "and": "all_of",
}
_OPERATOR_ALIASES = {
    "=": "equals", "==": "equals", "eq": "equals", "equal": "equals", "is": "equals",
    "!=": "not_equals", "<>": "not_equals", "ne": "not_equals", "neq": "not_equals",
    "not_equal": "not_equals",
    ">": "greater_than", "gt": "greater_than",
    ">=": "greater_or_equal", "gte": "greater_or_equal", "ge": "greater_or_equal",
    "greater_than_or_equal": "greater_or_equal", "greater_than_or_equals": "greater_or_equal",
    "greater_or_equals": "greater_or_equal", "greater_equal": "greater_or_equal",
    "<": "less_than", "lt": "less_than",
    "<=": "less_or_equal", "lte": "less_or_equal", "le": "less_or_equal",
    "less_than_or_equal": "less_or_equal", "less_than_or_equals": "less_or_equal",
    "less_or_equals": "less_or_equal", "less_equal": "less_or_equal",
    "not_in_list": "not_in", "notin": "not_in", "in_list": "in",
    "isnull": "is_null", "null": "is_null", "is_not_null": "is_not_null", "notnull": "is_not_null",
    "not_null": "is_not_null", "includes": "contains", "contain": "contains",
    "startswith": "starts_with", "start_with": "starts_with", "begins_with": "starts_with",
    "endswith": "ends_with", "end_with": "ends_with",
}
_FUNCTION_ALIASES = {
    "AVERAGE": "AVG", "MEAN": "AVG", "TOTAL": "SUM",
    "COUNTDISTINCT": "COUNT_DISTINCT", "COUNT DISTINCT": "COUNT_DISTINCT",
    "DISTINCT_COUNT": "COUNT_DISTINCT", "DISTINCTCOUNT": "COUNT_DISTINCT", "UNIQUE_COUNT": "COUNT_DISTINCT",
}
_DIRECTION_ALIASES = {"ascending": "asc", "descending": "desc"}


def _rename(item: dict, aliases: dict) -> dict:
    out = {}
    for key, value in item.items():
        target = aliases.get(str(key).lower(), key) if isinstance(key, str) else key
        # 정식 이름이 이미 있으면 별칭으로 덮어쓰지 않는다
        if target != key and target in item:
            target = key
        out[target] = value
    return out


def _normalize_operator(op):
    if not isinstance(op, str):
        return op
    text = re.sub(r"\s+", "_", op.strip().lower())
    return _OPERATOR_ALIASES.get(text, _OPERATOR_ALIASES.get(op.strip().lower(), text))


def _normalize_filter(spec):
    if not isinstance(spec, dict):
        return spec
    spec = _rename(spec, _ITEM_KEY_ALIASES)
    if "operator" in spec:
        spec["operator"] = _normalize_operator(spec["operator"])
    for group in ("any_of", "all_of"):
        if isinstance(spec.get(group), list):
            spec[group] = [_normalize_filter(s) for s in spec[group]]
    return spec


def _normalize_agg(spec):
    if not isinstance(spec, dict):
        return spec
    spec = _rename(spec, _ITEM_KEY_ALIASES)
    fn = spec.get("function")
    if isinstance(fn, str):
        upper = fn.strip().upper()
        spec["function"] = _FUNCTION_ALIASES.get(upper, upper.replace(" ", "_"))
    if "operator" in spec:
        spec["operator"] = _normalize_operator(spec["operator"])
    return spec


def _as_list(value):
    if value is None:
        return value
    return value if isinstance(value, list) else [value]


def normalize_ast(ast: dict) -> dict:
    """모델이 흔히 틀리는 키·연산자 이름을 표준 이름으로 맞춘다.

    뜻이 분명한 오타(greater_than_or_equal, column, >=)로 재시도 기회를 쓰지 않게 한다.
    모르는 키를 지우지는 않는다 — 그것은 compile 이 목록과 함께 거부한다.
    """
    out = _rename(ast, _TOP_KEY_ALIASES)
    if "filters" in out:
        out["filters"] = [_normalize_filter(f) for f in _as_list(out["filters"]) or []]
    if "having" in out:
        out["having"] = [_normalize_agg(h) for h in _as_list(out["having"]) or []]
    if "aggregations" in out:
        out["aggregations"] = [_normalize_agg(a) for a in _as_list(out["aggregations"]) or []]
    if "group_by" in out:
        out["group_by"] = [
            _rename(g, _ITEM_KEY_ALIASES) if isinstance(g, dict) else g
            for g in _as_list(out["group_by"]) or []
        ]
    if "columns" in out:
        out["columns"] = _as_list(out["columns"])
    if "joins" in out:
        out["joins"] = _as_list(out["joins"])
    if "order_by" in out:
        items = []
        for o in _as_list(out["order_by"]) or []:
            if isinstance(o, str):
                parts = o.strip().split()
                o = {"field": parts[0], "direction": parts[1]} if len(parts) == 2 else {"field": o.strip()}
            if isinstance(o, dict):
                o = _rename(o, _ITEM_KEY_ALIASES)
                d = o.get("direction")
                if isinstance(d, str):
                    o["direction"] = _DIRECTION_ALIASES.get(d.strip().lower(), d.strip().lower())
            items.append(o)
        out["order_by"] = items
    return out


# ---------------------------------------------------------------- 파생 지표 수식

_FORMULA_TOKEN = re.compile(r"\s*(?:\[([^\]]+)\]|(\d+(?:\.\d+)?)|([-+*/()]))")


def _tokenize_formula(expression: str) -> list[tuple[str, str]]:
    text = str(expression or "")
    tokens, pos = [], 0
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        m = _FORMULA_TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise ValueError(
                f"수식을 읽을 수 없습니다 -> {text[pos:pos + 20]!r} "
                "(지표는 [지표명], 숫자, + - * / ( ) 만 쓸 수 있습니다)"
            )
        if m.group(1) is not None:
            tokens.append(("ref", m.group(1).strip()))
        elif m.group(2) is not None:
            tokens.append(("num", m.group(2)))
        else:
            tokens.append(("op", m.group(3)))
        pos = m.end()
    if not tokens:
        raise ValueError("수식이 비어 있습니다 (예: [매출] - [환불])")
    return tokens


def compile_formula(expression: str, render_ref) -> tuple[str, list[str]]:
    """파생 지표 수식을 SQL 식으로 바꾼다. 참조한 지표 이름 목록을 함께 돌려준다.

    나눗셈은 0으로 나누지 않도록 NULLIF 로 감싸고, 정수 나눗셈으로 비율이 0이
    되지 않도록 1.0 을 곱한다.
    """
    tokens = _tokenize_formula(expression)
    refs: list[str] = []
    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else (None, None)

    def take():
        nonlocal pos
        tok = peek()
        pos += 1
        return tok

    def factor():
        kind, value = take()
        if kind == "num":
            return value
        if kind == "ref":
            refs.append(value)
            return render_ref(value)
        if kind == "op" and value == "(":
            inner = expr()
            if take() != ("op", ")"):
                raise ValueError(f"수식의 괄호가 닫히지 않았습니다 -> {expression!r}")
            return f"({inner})"
        if kind == "op" and value == "-":
            return f"-{factor()}"
        raise ValueError(f"수식이 올바르지 않습니다 -> {expression!r}")

    def term():
        left = factor()
        while peek() in (("op", "*"), ("op", "/")):
            _, op = take()
            right = factor()
            left = f"{left} * {right}" if op == "*" else f"{left} * 1.0 / NULLIF({right}, 0)"
        return left

    def expr():
        left = term()
        while peek() in (("op", "+"), ("op", "-")):
            _, op = take()
            left = f"{left} {op} {term()}"
        return left

    sql = expr()
    if pos != len(tokens):
        raise ValueError(f"수식이 올바르지 않습니다 -> {expression!r}")
    if not refs:
        raise ValueError(f"수식에 지표가 없습니다 -> {expression!r} (예: [매출] - [환불])")
    return sql, refs


class Compiler:
    """AST 검증 및 SQL 조립"""

    KEYS = {
        "target_table", "metric", "columns", "joins", "aggregations", "filters", "group_by",
        "having", "order_by", "limit", "distinct", "compare",
    }
    UNSUPPORTED = {
        "offset": "건너뛰기(OFFSET)",
        "union": "결과 합치기(UNION)",
        "subquery": "하위 질의",
        "sql": "SQL 문자열",
        "raw_sql": "SQL 문자열",
        "window": "윈도 함수",
    }
    _FUNCTIONS = ("SUM", "COUNT", "AVG", "MIN", "MAX", "COUNT_DISTINCT")
    _OPERATORS = {
        "equals": "=",
        "not_equals": "!=",
        "greater_than": ">",
        "greater_or_equal": ">=",
        "less_than": "<",
        "less_or_equal": "<=",
    }
    _LIST_OPERATORS = {"in": "IN", "not_in": "NOT IN"}
    _NULL_OPERATORS = {"is_null": "IS NULL", "is_not_null": "IS NOT NULL"}
    _LIKE_OPERATORS = ("contains", "starts_with", "ends_with")
    _QUOTE = {"mysql": "`", "postgresql": '"', "oracle": '"'}

    def __init__(self, contract: dict):
        self.allowed_tables = {
            t["name"]: [c["name"] for c in t["columns"]]
            for t in contract["tables"]
        }
        # 날짜 컬럼을 알아보려면 타입이 필요하다. 이름만 들고 있으면
        # '2026-01-01' 이 Oracle 에서 깨지는 것을 막을 수 없다.
        self.column_types = {
            (t["name"], c["name"]): c.get("type", "")
            for t in contract["tables"] for c in t["columns"]
        }
        self.column_codes = {
            (t["name"], c["name"]): c["codes"]
            for t in contract["tables"] for c in t["columns"] if c.get("codes")
        }
        self.relations = contract.get("relations", [])
        self.metrics = {m["name"]: m for m in contract.get("metrics", [])}
        self.driver = contract["driver"]
        self.quote = self._QUOTE.get(contract["driver"], '"')

    # ------------------------------------------------------------ 진입점

    def compile(self, ast: dict) -> str:
        """AST를 SQL로 컴파일"""
        ast = normalize_ast(ast)
        self._reject_unknown(ast)

        metrics = self._metrics(ast)
        # 지표 하나면 그 지표의 고정 필터가 WHERE 로, 여럿이면 지표마다 CASE WHEN 안으로 간다.
        metric = metrics[0] if len(metrics) == 1 else None
        if metrics:
            for key in ("columns", "aggregations", "joins"):
                if ast.get(key):
                    raise ValueError(
                        f"지표를 쓸 때는 {key} 를 적지 않습니다 — 지표 {', '.join(m['name'] for m in metrics)} 의 정의가 "
                        "그대로 쓰입니다. 조건은 filters, 묶음은 group_by 로만 더하십시오"
                    )
            table = metrics[0]["table"]
            joins_spec = metrics[0].get("joins") or []
        else:
            table = ast.get("target_table")
            joins_spec = []

        resolved_base = _find(table, self.allowed_tables)
        if resolved_base is None:
            raise ValueError(
                f"조회할 수 없는 테이블입니다 -> {table!r} "
                f"(사용 가능: {', '.join(self.allowed_tables)})"
            )
        if not metrics and ast.get("joins"):
            joins_spec = self._relation_joins(resolved_base, ast["joins"])
        if joins_spec:
            scope, joins = build_scope(resolved_base, joins_spec, self.allowed_tables)
        else:
            scope, joins = {resolved_base: self.allowed_tables[resolved_base]}, []
        base = resolved_base
        # 단일 테이블 SQL 은 예전 모양 그대로 두고, 조인이 있을 때만 컬럼에 테이블을 붙인다.
        qualify = bool(joins)
        # lags: 시계열 집계가 쓰는 LAG 컬럼 {(컬럼, 구분, 순서): 별칭}
        ctx = {"scope": scope, "base": base, "qualify": qualify, "lags": {}}

        model_filters = list(ast.get("filters") or [])
        if metric is not None:
            # 지표의 필터가 먼저, 모델이 낸 필터가 뒤에.
            filters = self._metric_filters(metric, model_filters, scope, base) + model_filters
        else:
            filters = model_filters

        # 무엇을 SELECT 하는가
        if len(metrics) > 1:
            row_columns, aggregates = None, [self._conditional_metric(m, model_filters, ctx) for m in metrics]
        else:
            row_columns, aggregates = self._selection(ast, metric, model_filters, ctx)
        row_mode = row_columns is not None

        group_cols, group_items = [], []
        for col in ast.get("group_by") or []:
            if row_mode:
                raise ValueError(
                    "목록 조회에는 group_by를 쓸 수 없습니다"
                    + (f" (지표 {metric['name']!r} 는 {', '.join(metric['columns'])} 조회로 이미 정의돼 있습니다)"
                       if metric is not None else " — 집계하려면 columns 대신 aggregations 를 쓰십시오")
                )
            group_items.append(self._group_item(col, ctx))
        group_cols = [g["select"] for g in group_items]

        if ast.get("distinct"):
            if not row_mode:
                raise ValueError(
                    "distinct 는 목록 조회(columns 또는 조회형 지표)에만 씁니다. "
                    "집계에서 중복을 빼려면 function 을 COUNT_DISTINCT 로 적으십시오"
                )
            if ast["distinct"] is not True:
                raise ValueError(f"distinct 는 true 만 받습니다 -> {ast['distinct']!r}")

        # 기간 비교는 집계 하나를 기간마다 펼친다
        compare = ast.get("compare")
        if compare:
            if row_mode or not aggregates:
                raise ValueError("compare 는 집계(aggregations 또는 집계형·파생 지표)에만 씁니다")
            if ast.get("having"):
                raise ValueError("compare 와 having 은 함께 쓸 수 없습니다")
            aggregates, period_filters = self._compare(compare, aggregates, ctx)
            filters = filters + period_filters

        select = list(group_cols)
        if row_mode:
            select += [c["sql"] for c in row_columns]
        for agg in aggregates:
            select.append(f"{agg['sql']} AS {self._alias(agg['alias'])}")

        where = [self._condition(f, ctx) for f in filters]
        if ctx["lags"]:
            # 시계열 집계: 기본 테이블을 LAG 를 붙인 서브쿼리로 감싼다. 별칭을 테이블 이름 그대로 둬서
            # 바깥의 컬럼 참조는 바뀌지 않는다. 기본 테이블만 보는 조건(기간 등)은 안으로 넣어 읽는 범위를
            # 줄인다 — 누적값의 증가분은 행을 걸러도 합이 맞는다(중간 값이 빠져도 끝값−시작값은 같다).
            inner = [w for f, w in zip(filters, where) if self._only_base(f, ctx)]
            where = [w for f, w in zip(filters, where) if not self._only_base(f, ctx)]
            # 같은 시각에 찍힌 행이 있어도 결과가 매번 같도록 값 자체로 한 번 더 정렬한다.
            lag_cols = ", ".join(
                f"LAG({self._identifier(col)}) OVER (PARTITION BY {self._identifier(part)} "
                f"ORDER BY {self._identifier(order)}, {self._identifier(col)}) AS {self._identifier(alias)}"
                for (col, part, order), alias in ctx["lags"].items()
            )
            sub = f"SELECT {self._identifier(base)}.*, {lag_cols} FROM {self._identifier(base)}"
            if inner:
                sub += " WHERE " + " AND ".join(inner)
            source = f"FROM ({sub}) {self._identifier(base)}"
        else:
            source = f"FROM {self._identifier(base)}"
        for kind, joined, pairs in joins:
            on = " AND ".join(
                f"{self._column_sql(lt, lc, True)} = {self._column_sql(rt, rc, True)}"
                for (lt, lc), (rt, rc) in pairs
            )
            source += f" {JOIN_TYPES[kind]} {self._identifier(joined)} ON {on}"

        head = "SELECT DISTINCT" if ast.get("distinct") else "SELECT"
        clauses = [f"{head} {', '.join(select) or '*'}", source]
        if where:
            clauses.append("WHERE " + " AND ".join(where))
        if group_items:
            clauses.append("GROUP BY " + ", ".join(g["expr"] for g in group_items))
        having = self._having(ast.get("having"), aggregates, row_mode, ctx)
        if having:
            clauses.append("HAVING " + " AND ".join(having))
        order = self._order_by(ast.get("order_by"), row_columns, aggregates, group_items, ctx, bool(ast.get("distinct")))
        if order:
            clauses.append("ORDER BY " + ", ".join(order))
        limit = self._limit(ast.get("limit"))
        if limit is not None:
            clauses.append(f"FETCH FIRST {limit} ROWS ONLY" if self.driver == "oracle" else f"LIMIT {limit}")
        return " ".join(clauses) + ";"

    # ------------------------------------------------------------ 검증 단계

    def _reject_unknown(self, ast: dict) -> None:
        unknown = set(ast) - self.KEYS
        if not unknown:
            return
        named = [self.UNSUPPORTED[k] for k in sorted(unknown) if k in self.UNSUPPORTED]
        other = sorted(k for k in unknown if k not in self.UNSUPPORTED)
        parts = []
        if named:
            parts.append("이 도구는 " + ", ".join(dict.fromkeys(named)) + "을(를) 만들지 않습니다")
        if other:
            parts.append(f"모르는 항목입니다 -> {', '.join(other)}")
        raise ValueError("; ".join(parts) + ". 쓸 수 있는 키: " + ", ".join(sorted(self.KEYS)))

    def _metrics(self, ast: dict) -> list[dict]:
        """metric 은 지표 이름 하나나 이름 목록이다. 여럿이면 한 SELECT 에 나란히 조회한다."""
        names = ast.get("metric")
        if not names:
            return []
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ValueError(f"metric 은 지표 이름이나 이름 목록입니다 -> {names!r}")
        out = []
        for name in names:
            metric = self.metrics.get(name) or self.metrics.get(_find(name, self.metrics) or "")
            if metric is None:
                raise ValueError(
                    f"등록되지 않은 지표입니다 -> {name!r} "
                    f"(사용 가능: {', '.join(self.metrics) or '없음'})"
                )
            if metric not in out:
                out.append(metric)
        if len(out) > 1:
            first = out[0]
            for m in out:
                if m.get("kind") == "projection":
                    raise ValueError(f"조회형 지표 {m['name']!r} 는 다른 지표와 함께 쓸 수 없습니다 — 따로 물으십시오")
                if m["table"].lower() != first["table"].lower() or (m.get("joins") or []) != (first.get("joins") or []):
                    raise ValueError(
                        f"지표 {first['name']!r} 와 {m['name']!r} 는 테이블(조인)이 달라 한 번에 조회할 수 없습니다 "
                        "— 지표마다 따로 물으십시오"
                    )
        return out

    def _conditional_metric(self, metric: dict, model_filters: list, ctx: dict) -> dict:
        """여러 지표를 함께 조회할 때: 지표마다 자기 고정 필터를 CASE WHEN 으로 품어 서로 섞이지 않게 한다."""
        specs = self._metric_filters(metric, model_filters, ctx["scope"], ctx["base"])
        own = " AND ".join(self._condition(s, ctx) for s in specs) or None
        if metric.get("kind") == "derived":
            inner = self._derived_aggregate(metric, model_filters, ctx)
        else:
            agg = metric["aggregation"]
            inner = self._aggregate({"field": agg["field"], "function": agg["function"], "alias": metric["name"]},
                                    ctx, series=metric.get("series"))

        def render(extra=None):
            return inner["render"](" AND ".join(c for c in (own, extra) if c) or None)

        return {"alias": metric["name"], "sql": render(), "render": render}

    def _relation_joins(self, base: str, items: list) -> list:
        """모델이 테이블 이름만 적은 조인을 등록된 관계로 펼친다.

        ON 조건은 모델이 아니라 관계 정의에서 온다 — 모델이 엉뚱한 컬럼끼리
        잇는 조인을 만들 수 없다.
        """
        scope = [base]
        out = []
        for item in items:
            if isinstance(item, str):
                item = {"table": item}
            if not isinstance(item, dict) or not item.get("table"):
                raise ValueError(f"joins 항목은 테이블 이름이나 {{\"table\": ..., \"type\": \"left\"}} 입니다 -> {item!r}")
            extra = set(item) - {"table", "type"}
            if extra:
                raise ValueError(
                    f"joins 항목에는 table 과 type 만 씁니다 -> {sorted(extra)} "
                    "(ON 조건은 등록된 관계에서 자동으로 정해집니다)"
                )
            table = _find(item["table"], self.allowed_tables)
            if table is None:
                raise ValueError(
                    f"조인할 수 없는 테이블입니다 -> {item['table']!r} (사용 가능: {', '.join(self.allowed_tables)})"
                )
            groups: dict = {}
            for r in self.relations:
                lt, rt = r["left_table"], r["right_table"]
                if lt in scope and rt == table:
                    pair = (f"{lt}.{r['left_column']}", f"{rt}.{r['right_column']}")
                elif rt in scope and lt == table:
                    pair = (f"{rt}.{r['right_column']}", f"{lt}.{r['left_column']}")
                else:
                    continue
                key = (r.get("constraint") or f"{lt}->{rt}", pair[0].split(".")[0])
                groups.setdefault(key, []).append(pair)
            if not groups:
                related = sorted({
                    r["right_table"] if r["left_table"] in scope else r["left_table"]
                    for r in self.relations
                    if (r["left_table"] in scope) != (r["right_table"] in scope)
                })
                raise ValueError(
                    f"{', '.join(scope)} 와(과) {table} 사이에 등록된 관계가 없습니다 "
                    f"(관계가 있는 테이블: {', '.join(related) or '없음'})"
                )
            if len(groups) > 1:
                raise ValueError(
                    f"{table} 로 잇는 관계가 여러 개라 고를 수 없습니다 -> "
                    f"{', '.join(k[0] for k in groups)}. 이 조인은 지표로 정의해 쓰십시오"
                )
            pairs = next(iter(groups.values()))
            out.append({
                "table": table,
                "type": str(item.get("type") or "inner").lower(),
                "on": [{"left": left, "right": right} for left, right in pairs],
            })
            scope.append(table)
        return out

    def _selection(self, ast: dict, metric: dict | None, model_filters: list, ctx: dict):
        """(목록 컬럼 또는 None, 집계 목록) 을 정한다."""
        if metric is not None:
            kind = metric.get("kind")
            if kind == "projection":
                return self._row_columns(metric["columns"], ctx), []
            if kind == "derived":
                return None, [self._derived_aggregate(metric, model_filters, ctx)]
            agg = metric["aggregation"]
            return None, [self._aggregate(
                {"field": agg["field"], "function": agg["function"], "alias": metric["name"]}, ctx,
                series=metric.get("series"))]

        columns = ast.get("columns")
        aggregations = ast.get("aggregations") or []
        if columns:
            if aggregations:
                raise ValueError("columns(목록 조회)와 aggregations(집계)는 함께 쓸 수 없습니다")
            return self._row_columns(columns, ctx), []
        return None, [self._aggregate(agg, ctx) for agg in aggregations]

    def _row_columns(self, columns: list, ctx: dict) -> list:
        out = []
        for column in columns:
            owner, resolved = self._ref(str(column), ctx["scope"], ctx["base"])
            out.append({"table": owner, "column": resolved,
                        "sql": self._column_sql(owner, resolved, ctx["qualify"])})
        return out

    def _aggregate(self, agg, ctx: dict, series: dict | None = None) -> dict:
        if not isinstance(agg, dict):
            raise ValueError(f"aggregations의 항목은 객체여야 합니다 -> {agg!r}")
        # 받은 키를 그대로 실어 보낸다. "컬럼이 None이다"라고만 하면 모델은
        # 자기가 field 대신 column이라고 썼다는 것을 알 수 없어서, 키 순서만
        # 바꾼 같은 AST를 시도 횟수만큼 반복한다.
        if "field" not in agg:
            raise ValueError(
                f"aggregations 항목에 field 키가 없습니다 -> 받은 키: {sorted(agg)} "
                "(모양: " + '{"field": "컬럼명 또는 *", "function": "COUNT"}' + ")"
            )
        field = agg.get("field")
        function = str(agg.get("function", "")).upper()
        if function in SERIES_FUNCTIONS and series is None:
            raise ValueError(
                f"{function} 는 지표 정의에서만 쓸 수 있습니다 (차량·시간 순서 같은 행 순서가 필요합니다). "
                "누적값의 증가분이나 발생 횟수는 그렇게 정의된 지표를 metric 으로 쓰십시오"
            )
        if function not in self._FUNCTIONS and function not in SERIES_FUNCTIONS:
            raise ValueError(
                f"허용되지 않은 집계 함수입니다 -> {function!r} "
                f"(사용 가능: {', '.join(self._FUNCTIONS)}). "
                "함수와 컬럼은 따로 적습니다 -> " + '{"field": "컬럼명 또는 *", "function": "COUNT"}'
            )
        if function in SERIES_FUNCTIONS:
            target = self._series_target(function, field, series, ctx)
            alias = agg.get("alias") or f"{function.lower()}_{field}"

            def render_series(extra=None, target=target):
                return self._agg_sql("SUM", target, extra)

            return {"alias": str(alias), "sql": render_series(), "render": render_series}
        if field == "*":
            if function not in ("COUNT",):
                raise ValueError(f"* 는 COUNT 에만 쓸 수 있습니다 -> {function}(*)")
            target = "*"
        else:
            owner, resolved = self._ref(str(field), ctx["scope"], ctx["base"], "존재하지 않는 컬럼입니다")
            target = self._column_sql(owner, resolved, ctx["qualify"])
        alias = agg.get("alias") or f"{function.lower()}_{field}"

        def render(extra=None, function=function, target=target):
            return self._agg_sql(function, target, extra)

        return {"alias": str(alias), "sql": render(), "render": render}

    def _derived_aggregate(self, metric: dict, model_filters: list, ctx: dict) -> dict:
        """파생 지표: 구성 지표마다 자기 고정 필터를 CASE WHEN 으로 품은 집계를 수식으로 잇는다."""
        parts = {}

        def component(name):
            comp = self.metrics.get(name) or self.metrics.get(_find(name, self.metrics) or "")
            if comp is None or comp.get("kind") not in (None, "aggregate"):
                raise ValueError(f"파생 지표 {metric['name']!r} 의 구성 지표 {name!r} 를 쓸 수 없습니다")
            if _find(comp["table"], ctx["scope"]) is None:
                raise ValueError(f"구성 지표 {name!r} 의 테이블 {comp['table']} 이(가) 이 조회에 없습니다")
            specs = self._metric_filters(comp, model_filters, ctx["scope"], ctx["base"])
            cond = " AND ".join(self._condition(s, ctx) for s in specs) or None
            agg = comp["aggregation"]
            field = agg["field"]
            function = str(agg["function"]).upper()
            if function in SERIES_FUNCTIONS:
                target = self._series_target(function, field, comp.get("series"), ctx)
                function = "SUM"
            elif field == "*":
                target = "*"
            else:
                owner, resolved = self._ref(str(field), ctx["scope"], ctx["base"], "존재하지 않는 컬럼입니다")
                target = self._column_sql(owner, resolved, ctx["qualify"])
            parts[name] = (function, target, cond)
            return name

        # 먼저 한 번 돌려 구성 지표를 검증·수집한다
        compile_formula(metric["expression"], component)

        def render(extra=None):
            def ref(name):
                function, target, cond = parts[name]
                both = " AND ".join(c for c in (cond, extra) if c) or None
                return self._agg_sql(function, target, both)
            return compile_formula(metric["expression"], ref)[0]

        return {"alias": metric["name"], "sql": render(), "render": render}

    def _series_target(self, function: str, field, series, ctx: dict) -> str:
        """시계열 집계의 행 단위 식. 직전 행 값은 서브쿼리의 LAG 컬럼에서 온다."""
        if not isinstance(series, dict) or not series.get("partition_by") or not series.get("order_by"):
            raise ValueError(f"{function} 지표에는 구분 컬럼(partition_by)과 순서 컬럼(order_by)이 필요합니다")
        base, scope = ctx["base"], ctx["scope"]
        base_only = {base: scope[base]}
        owner, column = self._ref(str(field), scope, base, "존재하지 않는 컬럼입니다")
        if owner != base:
            raise ValueError(f"{function} 는 기본 테이블 {base} 의 컬럼만 쓸 수 있습니다 -> {owner}.{column}")
        _, part = resolve_ref(str(series["partition_by"]), base_only, base, "구분 컬럼을 찾을 수 없습니다")
        _, order = resolve_ref(str(series["order_by"]), base_only, base, "순서 컬럼을 찾을 수 없습니다")
        lags = ctx["lags"]
        key = (column, part, order)
        if key not in lags:
            lags[key] = f"lag_{len(lags) + 1}_{column}"
        value = self._column_sql(base, column, ctx["qualify"])
        previous = self._column_sql(base, lags[key], ctx["qualify"])
        if function == "DELTA_SUM":
            max_step = series.get("max_step")
            if max_step is None:
                return f"GREATEST({value} - {previous}, 0)"
            # 리셋 뒤 원래 값으로 돌아오는 순간처럼 한 행에서 크게 튄 증가는 사용량이 아니다.
            limit = self._number(max_step, "max_step")
            return f"CASE WHEN {value} - {previous} BETWEEN 0 AND {limit} THEN {value} - {previous} ELSE 0 END"
        baseline = series.get("baseline")
        if baseline is None or str(baseline).strip() == "":
            raise ValueError("CHANGE_COUNT 지표에는 기준값(baseline, 예: 정상 = 0)이 필요합니다")
        data_type = self.column_types.get((base, column), "")
        where = f"{base}.{column}"
        normal = self._number(baseline, where) if is_numeric_type(data_type) else self._value(
            base, column, data_type, baseline, where, codes=False)
        return f"CASE WHEN {value} <> {normal} AND COALESCE({previous}, {normal}) = {normal} THEN 1 ELSE 0 END"

    def _only_base(self, spec, ctx: dict) -> bool:
        """이 조건이 기본 테이블 컬럼만 보는가 (시계열 서브쿼리 안으로 넣어도 되는가)."""
        fields = _filter_fields([spec]) if isinstance(spec, dict) else []
        if not fields:
            return False
        for field in fields:
            try:
                owner, _ = resolve_ref(str(field), ctx["scope"], ctx["base"])
            except ValueError:
                return False
            if owner != ctx["base"]:
                return False
        return True

    def _agg_sql(self, function: str, target: str, cond: str | None) -> str:
        if cond is None:
            if function == "COUNT_DISTINCT":
                return f"COUNT(DISTINCT {target})"
            return f"{function}({target})"
        inner = "1" if target == "*" else target
        case = f"CASE WHEN {cond} THEN {inner} END"
        if function == "COUNT_DISTINCT":
            return f"COUNT(DISTINCT {case})"
        return f"{function}({case})"

    def _group_item(self, col, ctx: dict) -> dict:
        if isinstance(col, dict):
            extra = set(col) - {"field", "grain"}
            if extra or "field" not in col:
                raise ValueError(
                    f"group_by 항목은 컬럼 이름이나 {{\"field\": \"날짜컬럼\", \"grain\": \"month\"}} 입니다 -> {col!r}"
                )
            field, grain = col["field"], col.get("grain")
        else:
            field, grain = col, None
        owner, resolved = self._ref(str(field), ctx["scope"], ctx["base"], "그룹화할 수 없는 컬럼입니다")
        column = self._column_sql(owner, resolved, ctx["qualify"])
        if grain is None:
            return {"expr": column, "select": column, "alias": None, "table": owner, "column": resolved}
        grain = str(grain).lower()
        if grain not in GRAINS:
            raise ValueError(f"grain 은 {', '.join(GRAINS)} 중 하나입니다 -> {grain!r}")
        if not temporal_kind(self.column_types.get((owner, resolved), ""), self.driver):
            raise ValueError(f"{owner}.{resolved} 는 날짜 컬럼이 아니라 grain 으로 묶을 수 없습니다")
        expr = self._truncate(column, grain)
        alias = f"{resolved}_{grain}"
        return {"expr": expr, "select": f"{expr} AS {self._alias(alias)}", "alias": alias,
                "table": owner, "column": resolved}

    def _truncate(self, column: str, grain: str) -> str:
        """날짜를 기간 단위의 첫날로 내린다. 드라이버마다 문법이 다르다."""
        if self.driver == "oracle":
            unit = {"day": "DD", "week": "IW", "month": "MM", "quarter": "Q", "year": "YYYY"}[grain]
            return f"TRUNC({column}, '{unit}')"
        if self.driver == "mysql":
            return {
                "day": f"DATE({column})",
                "week": f"DATE_SUB(DATE({column}), INTERVAL WEEKDAY({column}) DAY)",
                "month": f"DATE_FORMAT({column}, '%Y-%m-01')",
                "quarter": f"MAKEDATE(YEAR({column}), 1) + INTERVAL (QUARTER({column}) - 1) QUARTER",
                "year": f"DATE_FORMAT({column}, '%Y-01-01')",
            }[grain]
        return f"DATE_TRUNC('{grain}', {column})"

    def _compare(self, compare, aggregates: list, ctx: dict):
        """기간 비교: 집계마다 기간별 값과 (두 기간이면) 증감률을 만든다."""
        if not isinstance(compare, dict) or set(compare) - {"field", "periods"}:
            raise ValueError(
                "compare 모양: {\"field\": \"날짜컬럼\", \"periods\": "
                "[{\"label\": \"이번달\", \"from\": \"YYYY-MM-DD\", \"to\": \"YYYY-MM-DD\"}, {...}]}"
            )
        owner, resolved = self._ref(str(compare.get("field")), ctx["scope"], ctx["base"], "비교할 수 없는 컬럼입니다")
        if not temporal_kind(self.column_types.get((owner, resolved), ""), self.driver):
            raise ValueError(f"{owner}.{resolved} 는 날짜 컬럼이 아니라 기간 비교를 할 수 없습니다")
        periods = compare.get("periods")
        if not isinstance(periods, list) or not 2 <= len(periods) <= 4:
            raise ValueError("compare.periods 는 기간 2~4개의 목록입니다")
        ref = f"{owner}.{resolved}"
        labels, conds, starts, ends = [], [], [], []
        for p in periods:
            if not isinstance(p, dict) or not p.get("label") or "from" not in p or "to" not in p:
                raise ValueError(f"기간은 label, from, to 를 가져야 합니다 -> {p!r}")
            label = str(p["label"]).strip()
            if label in labels:
                raise ValueError(f"기간 이름이 겹칩니다 -> {label!r}")
            _, start = parse_temporal(p["from"], ref)
            _, end = parse_temporal(p["to"], ref)
            if start > end:
                raise ValueError(f"기간의 시작이 끝보다 늦습니다 -> {label}: {start} ~ {end}")
            labels.append(label)
            starts.append(start)
            ends.append(end)
            conds.append(" AND ".join(self._condition(s, ctx) for s in (
                {"field": ref, "operator": "greater_or_equal", "value": start},
                {"field": ref, "operator": "less_or_equal", "value": end},
            )))
        out = []
        for agg in aggregates:
            exprs = []
            for label, cond in zip(labels, conds):
                sql = agg["render"](cond)
                exprs.append(sql)
                out.append({"alias": f"{agg['alias']}_{label}", "sql": sql, "render": None})
            if len(exprs) == 2:
                out.append({
                    "alias": f"{agg['alias']}_증감률",
                    "sql": f"({exprs[0]} - {exprs[1]}) * 1.0 / NULLIF({exprs[1]}, 0)",
                    "render": None,
                })
        span = [
            {"field": ref, "operator": "greater_or_equal", "value": min(starts)},
            {"field": ref, "operator": "less_or_equal", "value": max(ends)},
        ]
        return out, span

    def _having(self, items, aggregates: list, row_mode: bool, ctx: dict) -> list:
        if not items:
            return []
        if row_mode or not aggregates:
            raise ValueError("having 은 집계 결과를 거르는 조건이라 aggregations(또는 집계형 지표)와 함께만 씁니다")
        by_alias = {a["alias"].lower(): a for a in aggregates}
        out = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError(f"having 항목은 객체여야 합니다 -> {item!r}")
            if item.get("alias") is not None and "function" not in item:
                agg = by_alias.get(str(item["alias"]).lower())
                if agg is None:
                    raise ValueError(
                        f"having 이 가리키는 집계가 없습니다 -> {item['alias']!r} (사용 가능: {', '.join(a['alias'] for a in aggregates)})"
                    )
                expr = agg["sql"]
            else:
                expr = self._aggregate({k: v for k, v in item.items() if k in ("field", "function")}, ctx)["sql"]
            operator = item.get("operator")
            if operator not in self._OPERATORS:
                raise ValueError(
                    f"having 의 비교 연산자가 허용되지 않습니다 -> {operator!r} (사용 가능: {', '.join(self._OPERATORS)})"
                )
            out.append(f"{expr} {self._OPERATORS[operator]} {self._number(item.get('value'), 'having')}")
        return out

    def _order_by(self, items, row_columns, aggregates, group_items, ctx, distinct: bool) -> list:
        if not items:
            return []
        out = []
        aliases = {a["alias"].lower(): a["alias"] for a in aggregates}
        aliases.update({g["alias"].lower(): g["alias"] for g in group_items if g["alias"]})
        for item in items:
            if not isinstance(item, dict) or "field" not in item:
                raise ValueError(f"order_by 항목 모양: {{\"field\": \"컬럼 또는 집계 별칭\", \"direction\": \"desc\"}} -> {item!r}")
            extra = set(item) - {"field", "direction"}
            if extra:
                raise ValueError(f"order_by 항목에는 field 와 direction 만 씁니다 -> {sorted(extra)}")
            direction = str(item.get("direction") or "asc").lower()
            if direction not in ("asc", "desc"):
                raise ValueError(f"direction 은 asc 나 desc 입니다 -> {direction!r}")
            field = str(item["field"])
            if field.lower() in aliases:
                out.append(self._order_term(self._alias(aliases[field.lower()]), direction))
                continue
            try:
                owner, resolved = self._ref(field, ctx["scope"], ctx["base"], "정렬할 수 없는 컬럼입니다")
            except ValueError as error:
                options = list(aliases.values())
                raise ValueError(f"{error}" + (f" (집계 별칭: {', '.join(options)})" if options else "")) from None
            column = self._column_sql(owner, resolved, ctx["qualify"])
            if row_columns is None and (aggregates or group_items):
                plain = [g for g in group_items if g["alias"] is None and (g["table"], g["column"]) == (owner, resolved)]
                if not plain:
                    allowed = [g["column"] for g in group_items if g["alias"] is None] + list(aliases.values())
                    raise ValueError(
                        f"집계 질의는 group_by 컬럼이나 집계 별칭으로만 정렬할 수 있습니다 -> {field} "
                        f"(사용 가능: {', '.join(allowed) or '없음'})"
                    )
            elif distinct and row_columns is not None and not any(
                (c["table"], c["column"]) == (owner, resolved) for c in row_columns
            ):
                raise ValueError(f"distinct 목록은 조회한 컬럼으로만 정렬할 수 있습니다 -> {field}")
            out.append(self._order_term(column, direction))
        return out

    def _order_term(self, expr: str, direction: str) -> str:
        """NULL 은 언제나 뒤로. 0으로 나눠 NULL 이 된 비율이 "상위 5개" 의 맨 앞에 오지 않게 한다.

        PostgreSQL·Oracle 은 DESC 에서, MySQL 은 ASC 에서 NULL 이 앞에 온다.
        """
        if self.driver == "mysql":
            return f"{expr} IS NULL, {expr} ASC" if direction == "asc" else f"{expr} DESC"
        return f"{expr} DESC NULLS LAST" if direction == "desc" else f"{expr} ASC"

    def _limit(self, value):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, str) and value.strip().isdigit():
                value = int(value.strip())
            else:
                raise ValueError(f"limit 은 정수입니다 -> {value!r}")
        if not 1 <= value <= MAX_LIMIT:
            raise ValueError(f"limit 은 1 이상 {MAX_LIMIT} 이하입니다 -> {value}")
        return value

    # ------------------------------------------------------------ 필터

    def _metric_filters(self, metric: dict, model_filters: list, scope: dict, base: str) -> list:
        """지표의 필터를 출처에 따라 펼친다.

        literal 은 언제나 붙는다 — 모델이 잊어도 여기서 강제되는 불변 조건이다.
        relative·question 은 값이 질문에서 오는 것이 요점이라, 질문이 같은
        컬럼을 건드리면 물러난다. 그러지 않으면 지표의 기간과 질문의 기간이
        AND 로 묶여 교집합이 빈 SQL 이 조용히 나간다.
        """
        # 'MEMBER_ID' 와 'TB_ORDER.MEMBER_ID' 는 같은 컬럼이다 — 해석한 (테이블, 컬럼)으로 비교한다.
        touched = set()
        for field in _filter_fields(model_filters):
            try:
                touched.add(self._ref(str(field), scope, base))
            except ValueError:
                pass  # 잘못된 컬럼은 _condition 이 구체적인 오류로 돌려준다
        out = []
        for spec in metric.get("fixed_filters", []):
            source = str(spec.get("source") or "literal").lower()
            field = str(spec.get("field", ""))
            if source not in FILTER_SOURCES:
                raise ValueError(
                    f"알 수 없는 필터 출처입니다 -> {source!r} "
                    f"(사용 가능: {', '.join(FILTER_SOURCES)})"
                )
            if source == "literal":
                out.append({**spec, METRIC_FILTER: True})
                continue
            key = self._ref(field, scope, base, "지표의 고정 필터 컬럼을 찾을 수 없습니다")
            if key in touched:
                continue
            if source == "question":
                raise ValueError(
                    f"지표 {metric['name']!r} 는 {field} 조건을 질문에서 받습니다 -> "
                    f"filters 에 {field} 를 넣어 값이나 기간을 지정하십시오"
                )
            where = f"{key[0]}.{key[1]}"
            out.append({**spec, "source": "literal", METRIC_FILTER: True,
                        "value": resolve_relative(spec.get("value"), where)})
        return out

    def _condition(self, spec, ctx: dict, parent: str = " AND ") -> str:
        """WHERE 조건 하나. any_of / all_of 로 묶인 조건은 의미가 달라질 때만 괄호로 감싼다.

        parent 는 이 조건을 잇는 바깥 연결어. 같은 연결어끼리(AND 안의 all_of, OR 안의 any_of)나
        조건이 하나뿐인 묶음은 괄호가 뜻을 바꾸지 않으므로 풀어 쓴다. 모델이 filters 전체를
        all_of 하나로 감싸 보내는 일이 흔하다 — filters 는 원래 AND 다.
        """
        if isinstance(spec, dict) and ("any_of" in spec or "all_of" in spec):
            if len(spec) != 1:
                raise ValueError(f"any_of / all_of 는 다른 키와 함께 쓸 수 없습니다 -> 받은 키: {sorted(spec)}")
            group = "any_of" if "any_of" in spec else "all_of"
            items = spec[group]
            if not isinstance(items, list) or len(items) < 1:
                raise ValueError(f"{group} 는 조건 목록입니다 -> {items!r}")
            joiner = " OR " if group == "any_of" else " AND "
            inner = joiner.join(self._condition(s, ctx, joiner) for s in items)
            return inner if len(items) == 1 or joiner == parent else f"({inner})"
        return self._filter(spec, ctx["scope"], ctx["base"], ctx["qualify"])

    def _filter(self, spec: dict, scope: dict, base: str, qualify: bool) -> str:
        """WHERE 조건 생성"""
        if not isinstance(spec, dict):
            raise ValueError(f"filters의 항목은 객체여야 합니다 -> {spec!r}")
        if "field" not in spec:
            raise ValueError(
                f"filters 항목에 field 키가 없습니다 -> 받은 키: {sorted(spec)} "
                "(모양: " + '{"field": "컬럼명", "operator": "equals", "value": "값"}' + ", 또는 "
                + '{"any_of": [조건, 조건]}' + ")"
            )
        table, resolved = self._ref(str(spec.get("field")), scope, base, "필터링할 수 없는 컬럼입니다")
        column = self._column_sql(table, resolved, qualify)
        operator = spec.get("operator")
        data_type = self.column_types.get((table, resolved), "")
        temporal = temporal_kind(data_type, self.driver)
        where = f"{table}.{resolved}"

        if operator in self._NULL_OPERATORS:
            if spec.get("value") not in (None, ""):
                raise ValueError(f"{operator!r} 는 값을 받지 않습니다 -> {spec.get('value')!r}")
            return f"{column} {self._NULL_OPERATORS[operator]}"

        if operator in self._LIKE_OPERATORS:
            if temporal or is_numeric_type(data_type):
                raise ValueError(f"{where} 는 문자 컬럼이 아니라 {operator!r} 를 쓸 수 없습니다")
            value = spec.get("value")
            if not isinstance(value, str) or not value:
                raise ValueError(f"{operator!r} 의 값은 비어 있지 않은 문자열입니다 -> {value!r}")
            escaped = value.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            pattern = {"contains": f"%{escaped}%", "starts_with": f"{escaped}%", "ends_with": f"%{escaped}"}[operator]
            return f"{column} LIKE {self._literal(pattern)} ESCAPE '!'"

        if operator in self._LIST_OPERATORS:
            values = spec.get("value")
            if not isinstance(values, (list, tuple)) or not values:
                raise ValueError(
                    f"{operator!r}의 value는 비어 있지 않은 목록이어야 합니다 -> {values!r}"
                )
            if temporal:
                rendered = ", ".join(self._temporal_value(temporal, v, where) for v in values)
            else:
                rendered = ", ".join(
                    self._value(table, resolved, data_type, v, where, codes=not spec.get(METRIC_FILTER)) for v in values)
            return f"{column} {self._LIST_OPERATORS[operator]} ({rendered})"

        if operator not in self._OPERATORS:
            raise ValueError(
                f"허용되지 않는 비교 연산자입니다 -> {operator!r} (사용 가능: "
                f"{', '.join(list(self._OPERATORS) + list(self._LIST_OPERATORS) + list(self._NULL_OPERATORS) + list(self._LIKE_OPERATORS))})"
            )

        if temporal:
            operator, rendered = self._temporal_compare(
                temporal, operator, spec.get("value"), where)
            return f"{column} {self._OPERATORS[operator]} {rendered}"
        # 코드 사전은 모델이 낸 값에만 적용한다. 지표 정의의 값은 관리자가 실제 DB 값으로
        # 적은 것이라, 사전이 일부만 채워져 있어도 그대로 믿는다.
        codes = operator in ("equals", "not_equals") and not spec.get(METRIC_FILTER)
        rendered = self._value(table, resolved, data_type, spec.get("value"), where, codes=codes)
        return f"{column} {self._OPERATORS[operator]} {rendered}"

    def _value(self, table: str, column: str, data_type: str, value, where: str, codes: bool = True) -> str:
        """비교 값을 리터럴로. 코드 사전이 있으면 업무 용어를 코드로 바꾸고,
        숫자 컬럼에 숫자가 아닌 값이 오면 거부한다."""
        known = self.column_codes.get((table, column)) if codes else None
        if known and value is not None and not isinstance(value, (list, tuple, dict)):
            text = str(value).strip().lower()
            for entry in known:
                if text == str(entry.get("code", "")).strip().lower():
                    value = entry["code"]
                    break
                if text == str(entry.get("label", "")).strip().lower():
                    value = entry["code"]
                    break
            else:
                listed = ", ".join(f"{e.get('code')}={e.get('label')}" for e in known)
                raise ValueError(f"{where} 는 코드 컬럼입니다. 사전에 없는 값입니다 -> {value!r} (사용 가능: {listed})")
        if is_numeric_type(data_type) and value is not None and not isinstance(value, bool):
            if not isinstance(value, (int, float)):
                try:
                    float(str(value).strip())
                except ValueError:
                    raise ValueError(
                        f"{where} 는 숫자 컬럼({data_type})인데 값이 숫자가 아닙니다 -> {value!r}. "
                        "이름 같은 값이면 그 값을 담은 다른 컬럼(예: 이름 컬럼)으로 필터하십시오"
                    ) from None
        return self._literal(value)

    def _number(self, value, where: str) -> str:
        if isinstance(value, bool):
            raise ValueError(f"{where} 의 값은 숫자입니다 -> {value!r}")
        if isinstance(value, (int, float)):
            return str(value)
        try:
            number = float(str(value).strip())
        except ValueError:
            raise ValueError(f"{where} 의 값은 숫자입니다 -> {value!r}") from None
        return str(int(number)) if number.is_integer() else str(number)

    def _temporal_value(self, column_kind: str, value, where: str) -> str:
        """날짜 값을 타입이 붙은 리터럴로.

        Oracle 은 문자열→DATE 암묵 변환이 NLS_DATE_FORMAT 에 좌우돼서
        '2026-01-01' 이 ORA-01861 로 깨진다. DATE / TIMESTAMP 리터럴은
        세 드라이버가 모두 같은 뜻으로 받는다.
        """
        value_kind, iso = parse_temporal(value, where)
        if column_kind == "datetime" and value_kind == "date":
            raise ValueError(
                f"{where} 는 시각을 포함하는 컬럼이라 하루를 값 하나로 고를 수 없습니다 "
                f"(자정인 행만 걸립니다) -> {value!r}. "
                "greater_or_equal 과 less_or_equal 로 기간을 지정하십시오"
            )
        return f"{'TIMESTAMP' if value_kind == 'datetime' else 'DATE'} '{iso}'"

    def _temporal_compare(self, column_kind: str, operator: str, value, where: str):
        """시각을 포함한 컬럼에 날짜만 주면, 그 날 하루가 온전히 들어오도록 맞춘다.

        `<= 2026-01-31` 을 그대로 두면 31일 00:00 까지만 걸려서 그날 낮에
        들어온 행이 조용히 빠진다. 이 앱은 SQL 을 실행하지 않고 보여주므로
        바뀐 결과가 화면에 그대로 드러난다.
        """
        value_kind, iso = parse_temporal(value, where)
        if column_kind == "datetime" and value_kind == "date":
            if operator in ("equals", "not_equals"):
                raise ValueError(
                    f"{where} 는 시각을 포함하는 컬럼이라 {operator!r} 로는 하루를 고를 수 없습니다 "
                    "(자정인 행만 걸립니다). "
                    "greater_or_equal 과 less_or_equal 로 기간을 지정하십시오"
                )
            if operator == "less_or_equal":
                operator, iso = "less_than", next_day(iso)
            elif operator == "greater_than":
                operator, iso = "greater_or_equal", next_day(iso)
        prefix = "TIMESTAMP" if value_kind == "datetime" else "DATE"
        return operator, f"{prefix} '{iso}'"

    # ------------------------------------------------------------ 식별자·리터럴

    def _ref(self, ref: str, scope: dict, base: str, label: str = "조회할 수 없는 컬럼입니다") -> tuple[str, str]:
        """resolve_ref 에 '어떻게 고치면 되는지' 를 더한다 — 조인하지 않은 테이블을 가리킨 경우."""
        try:
            return resolve_ref(ref, scope, base, label)
        except ValueError as error:
            parts = str(ref).split(".")
            table = _find(parts[0].strip(), self.allowed_tables) if len(parts) == 2 else None
            if table is None or table in scope:
                raise
            linked = any(
                (r["left_table"] == table and r["right_table"] in scope)
                or (r["right_table"] == table and r["left_table"] in scope)
                for r in self.relations
            )
            hint = (
                f"target_table 조회에 \"joins\": [\"{table}\"] 를 더하면 등록된 관계로 이어집니다 "
                "(지표에는 joins 를 더할 수 없으니 그때는 지표 대신 target_table 로 조회하십시오)"
                if linked else f"{table} 는 이 조회의 테이블과 등록된 관계가 없어 함께 조회할 수 없습니다"
            )
            raise ValueError(f"{error}. {hint}") from None

    def _identifier(self, name: str) -> str:
        """식별자를 드라이버의 인용 문자로 감싸기"""
        text = str(name)
        if not text:
            raise ValueError("빈 식별자는 쓸 수 없습니다")
        if self.quote in text or "\x00" in text:
            raise ValueError(f"식별자에 {self.quote}나 NUL을 쓸 수 없습니다 -> {name!r}")
        return f"{self.quote}{text}{self.quote}"

    def _column_sql(self, table: str, column: str, qualify: bool) -> str:
        """컬럼 식별자. 조인이 있으면 같은 이름의 컬럼이 겹치지 않도록 테이블을 붙인다."""
        if qualify:
            return f"{self._identifier(table)}.{self._identifier(column)}"
        return self._identifier(column)

    def _alias(self, alias: str) -> str:
        """SELECT 별칭 검증 및 인용"""
        text = str(alias)
        if not text or text != text.strip():
            raise ValueError(f"별칭은 비어 있거나 공백으로 끝날 수 없습니다 -> {alias!r}")
        if len(text) > 64:
            raise ValueError(f"별칭은 64자를 넘을 수 없습니다 -> {text[:20]}...")
        if self.quote in text or "\x00" in text:
            raise ValueError(f"별칭에 {self.quote}나 NUL을 쓸 수 없습니다 -> {alias!r}")
        return f"{self.quote}{text}{self.quote}"

    def _literal(self, value) -> str:
        """SQL 리터럴로 변환"""
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float)):
            return str(value)
        if value is None:
            return "NULL"
        if isinstance(value, (list, tuple, dict)):
            raise ValueError(f"값 하나를 받는 자리에 목록이 왔습니다 -> {value!r}")

        text = str(value)
        if "\x00" in text:
            raise ValueError("값에 NUL 문자를 넣을 수 없습니다")
        if self.quote == "`" and "\\" in text:
            raise ValueError(
                "값에 백슬래시를 넣을 수 없습니다 "
                "(MySQL에서 백슬래시는 이스케이프 문자입니다)"
            )
        return "'" + text.replace("'", "''") + "'"


def _filter_fields(filters) -> list:
    """filters 안의 모든 field (any_of / all_of 안쪽 포함)."""
    out = []
    for f in filters or []:
        if not isinstance(f, dict):
            continue
        for group in ("any_of", "all_of"):
            if isinstance(f.get(group), list):
                out.extend(_filter_fields(f[group]))
        if "field" in f:
            out.append(f.get("field", ""))
    return out
