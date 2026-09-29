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


JOIN_TYPES = {"inner": "INNER JOIN", "left": "LEFT JOIN"}


def _find(name, options):
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
        # 조건 없는 조인은 곱집합이다. 행 수가 곱으로 불어나 집계가 조용히 틀린다.
        if not pairs:
            raise ValueError(f"{table} 조인에 ON 조건이 없습니다")
        earlier = dict(scope)
        scope[table] = tables[table]
        rendered = []
        for pair in pairs:
            left = resolve_ref(pair.get("left"), scope, resolved_base)
            right = resolve_ref(pair.get("right"), scope, resolved_base)
            sides = {left[0] == table, right[0] == table}
            if sides != {True, False} or not all(t == table or t in earlier for t, _ in (left, right)):
                raise ValueError(
                    f"{table} 조인의 ON 은 {table} 의 컬럼과 앞서 나온 테이블"
                    f"({', '.join(earlier)})의 컬럼을 이어야 합니다 -> "
                    f"{pair.get('left')} = {pair.get('right')}"
                )
            rendered.append((left, right))
        out.append((kind, table, rendered))
    return scope, out


def resolve_ref(ref, scope: dict, base: str) -> tuple[str, str]:
    """'테이블.컬럼' 또는 '컬럼' 을 (테이블, 컬럼) 으로.

    접두어가 없으면 기본 테이블을 먼저 보고, 없으면 조인된 테이블 중
    유일하게 가진 곳을 쓴다. 둘 이상이면 모호하다고 거부한다.
    """
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError(f"컬럼 이름이 비어 있습니다 -> {ref!r}")
    text = ref.strip()
    if "." in text:
        prefix, column = text.split(".", 1)
        table = _find(prefix, scope)
        if table is None:
            raise ValueError(f"이 조회에 없는 테이블입니다 -> {text} (사용 가능: {', '.join(scope)})")
        resolved = _find(column, scope[table])
        if resolved is None:
            raise ValueError(f"존재하지 않는 컬럼입니다 -> {table}.{column}")
        return table, resolved
    resolved = _find(text, scope[base])
    if resolved is not None:
        return base, resolved
    owners = [(t, c) for t, cols in scope.items() if t != base
              for c in [_find(text, cols)] if c is not None]
    if len(owners) == 1:
        return owners[0]
    if owners:
        raise ValueError(
            f"여러 테이블에 있는 컬럼입니다 -> {text} "
            f"('{owners[0][0]}.{text}' 처럼 테이블을 붙여 적으십시오)"
        )
    raise ValueError(f"존재하지 않는 컬럼입니다 -> {base}.{text}")


class Compiler:
    """AST 검증 및 SQL 조립"""

    KEYS = {"target_table", "metric", "aggregations", "filters", "group_by"}
    UNSUPPORTED = {
        "having": "집계 결과 필터(HAVING)",
        "join": "테이블 조인(JOIN)",
        "joins": "테이블 조인(JOIN)",
        "distinct": "중복 제거(DISTINCT)",
        "order_by": "정렬(ORDER BY)",
        "limit": "행수 제한(LIMIT)",
    }
    _FUNCTIONS = ("SUM", "COUNT", "AVG", "MIN", "MAX")
    _OPERATORS = {
        "equals": "=",
        "not_equals": "!=",
        "greater_than": ">",
        "greater_or_equal": ">=",
        "less_than": "<",
        "less_or_equal": "<=",
    }
    _LIST_OPERATORS = {"in": "IN", "not_in": "NOT IN"}
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
        self.metrics = {m["name"]: m for m in contract.get("metrics", [])}
        self.driver = contract["driver"]
        self.quote = self._QUOTE.get(contract["driver"], '"')

    def compile(self, ast: dict) -> str:
        """AST를 SQL로 컴파일"""
        # 모르는 키 검사
        unknown = set(ast) - self.KEYS
        if unknown:
            named = [
                self.UNSUPPORTED[k] for k in sorted(unknown)
                if k in self.UNSUPPORTED
            ]
            other = sorted(k for k in unknown if k not in self.UNSUPPORTED)
            parts = []
            if named:
                parts.append("이 도구는 " + ", ".join(dict.fromkeys(named)) + "을(를) 만들지 않습니다")
            if other:
                parts.append(f"모르는 항목입니다 -> {', '.join(other)}")
            if unknown & {"join", "joins"}:
                parts.append("여러 테이블이 필요하면 조인이 정의된 지표를 metric 으로 쓰십시오")
            raise ValueError(
                "; ".join(parts) + ". 쓸 수 있는 키: " + ", ".join(sorted(self.KEYS))
            )

        # 지표 또는 테이블 선택
        metric_name = ast.get("metric")
        if metric_name:
            metric = self.metrics.get(metric_name)
            if metric is None:
                raise ValueError(
                    f"등록되지 않은 지표입니다 -> {metric_name!r} "
                    f"(사용 가능: {', '.join(self.metrics) or '없음'})"
                )
            # 조인은 사람이 정의한 지표에서만 온다. 모델은 조인을 만들 수 없다.
            scope, joins = build_scope(metric["table"], metric.get("joins"), self.allowed_tables)
            base = next(iter(scope))
            model_filters = list(ast.get("filters") or [])
            # 지표의 필터가 먼저, 모델이 낸 필터가 뒤에.
            filters = self._metric_filters(metric, model_filters, scope, base) + model_filters
            if metric.get("kind") == "projection":
                # 조회형 지표는 컬럼 목록 자체가 정의다. 여기에 group_by를 겹치면
                # 정의에 없던 집계가 생기므로 받지 않는다.
                if ast.get("group_by"):
                    raise ValueError(
                        f"조회형 지표에는 group_by를 쓸 수 없습니다 -> {metric_name!r} "
                        f"(이 지표는 {', '.join(metric['columns'])} 조회로 이미 정의돼 있습니다)"
                    )
                projection = metric["columns"]
                aggregations = []
            else:
                projection = []
                aggregations = [{**metric["aggregation"], "alias": metric_name}]
        else:
            table = ast.get("target_table")
            if _find(table, self.allowed_tables) is None:
                joinable = [m for m, d in self.metrics.items() if d.get("joins")]
                hint = f" 여러 테이블이 필요하면 조인 지표를 쓰십시오: {', '.join(joinable)}" if joinable else ""
                raise ValueError(
                    f"조회할 수 없는 테이블입니다 -> {table!r} "
                    f"(사용 가능: {', '.join(self.allowed_tables)}).{hint}"
                )
            scope, joins = build_scope(table, [], self.allowed_tables)
            base = next(iter(scope))
            projection = []
            aggregations = ast.get("aggregations") or []
            filters = ast.get("filters") or []

        # 조인이 있을 때만 컬럼에 테이블을 붙인다. 단일 테이블 SQL 은 예전 모양 그대로.
        qualify = bool(joins)

        def column_sql(ref) -> str:
            return self._column(resolve_ref(ref, scope, base), qualify)

        # SELECT 절 생성
        select = [column_sql(column) for column in projection]

        for agg in aggregations:
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
            if function not in self._FUNCTIONS:
                raise ValueError(
                    f"허용되지 않은 집계 함수입니다 -> {function!r} "
                    f"(사용 가능: {', '.join(self._FUNCTIONS)}). "
                    "함수와 컬럼은 따로 적습니다 -> " + '{"field": "컬럼명 또는 *", "function": "COUNT"}'
                )
            target = "*" if field == "*" else column_sql(field)
            alias = agg.get("alias") or f"{function.lower()}_{str(field).replace('.', '_')}"
            select.append(f"{function}({target}) AS {self._alias(alias)}")

        # GROUP BY 절 생성
        group_cols = [column_sql(col) for col in (ast.get("group_by") or [])]

        select = group_cols + select

        # SQL 조립
        clauses = [
            f"SELECT {', '.join(select) or '*'}",
            f"FROM {self._identifier(base)}"
        ]
        for kind, table, pairs in joins:
            on = " AND ".join(
                f"{self._column(left, True)} = {self._column(right, True)}"
                for left, right in pairs
            )
            clauses.append(f"{JOIN_TYPES[kind]} {self._identifier(table)} ON {on}")
        where = [self._filter(f, scope, base, qualify) for f in filters]
        if where:
            clauses.append("WHERE " + " AND ".join(where))
        if group_cols:
            clauses.append("GROUP BY " + ", ".join(group_cols))
        return " ".join(clauses) + ";"

    def _metric_filters(self, metric: dict, model_filters: list, scope: dict, base: str) -> list:
        """지표의 필터를 출처에 따라 펼친다.

        literal 은 언제나 붙는다 — 모델이 잊어도 여기서 강제되는 불변 조건이다.
        relative·question 은 값이 질문에서 오는 것이 요점이라, 질문이 같은
        컬럼을 건드리면 물러난다. 그러지 않으면 지표의 기간과 질문의 기간이
        AND 로 묶여 교집합이 빈 SQL 이 조용히 나간다.
        """
        # 'ORDERS.ORDER_DT' 와 'ORDER_DT' 가 같은 컬럼이면 같은 것으로 본다.
        touched = set()
        for f in model_filters:
            if isinstance(f, dict):
                try:
                    touched.add(resolve_ref(f.get("field"), scope, base))
                except ValueError:
                    pass  # 잘못된 필드는 뒤의 _filter 가 이유와 함께 거부한다
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
                out.append(spec)
                continue
            if resolve_ref(field, scope, base) in touched:
                continue
            if source == "question":
                raise ValueError(
                    f"지표 {metric['name']!r} 는 {field} 조건을 질문에서 받습니다 -> "
                    f"filters 에 {field} 를 넣어 값이나 기간을 지정하십시오"
                )
            where = ".".join(resolve_ref(field, scope, base))
            out.append({**spec, "source": "literal",
                        "value": resolve_relative(spec.get("value"), where)})
        return out

    def _filter(self, spec: dict, scope: dict, base: str, qualify: bool) -> str:
        """WHERE 조건 생성"""
        if not isinstance(spec, dict):
            raise ValueError(f"filters의 항목은 객체여야 합니다 -> {spec!r}")
        if "field" not in spec:
            raise ValueError(
                f"filters 항목에 field 키가 없습니다 -> 받은 키: {sorted(spec)} "
                "(모양: " + '{"field": "컬럼명", "operator": "equals", "value": "값"}' + ")"
            )
        table, resolved = resolve_ref(spec.get("field"), scope, base)
        column = self._column((table, resolved), qualify)
        operator = spec.get("operator")
        temporal = temporal_kind(self.column_types.get((table, resolved), ""), self.driver)
        where = f"{table}.{resolved}"

        if operator in self._LIST_OPERATORS:
            values = spec.get("value")
            if not isinstance(values, (list, tuple)) or not values:
                raise ValueError(
                    f"{operator!r}의 value는 비어 있지 않은 목록이어야 합니다 -> {values!r}"
                )
            if temporal:
                rendered = ", ".join(self._temporal_value(temporal, v, where) for v in values)
            else:
                rendered = ", ".join(self._literal(v) for v in values)
            return f"{column} {self._LIST_OPERATORS[operator]} ({rendered})"

        if operator not in self._OPERATORS:
            raise ValueError(
                f"허용되지 않는 비교 연산자입니다 -> {operator!r} (사용 가능: "
                f"{', '.join(list(self._OPERATORS) + list(self._LIST_OPERATORS))})"
            )

        if temporal:
            operator, rendered = self._temporal_compare(
                temporal, operator, spec.get("value"), where)
            return f"{column} {self._OPERATORS[operator]} {rendered}"
        return f"{column} {self._OPERATORS[operator]} {self._literal(spec.get('value'))}"

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

    def _column(self, ref: tuple[str, str], qualify: bool) -> str:
        table, column = ref
        if qualify:
            return f"{self._identifier(table)}.{self._identifier(column)}"
        return self._identifier(column)

    def _identifier(self, name: str) -> str:
        """식별자를 드라이버의 인용 문자로 감싸기"""
        text = str(name)
        if not text:
            raise ValueError("빈 식별자는 쓸 수 없습니다")
        if self.quote in text or "\x00" in text:
            raise ValueError(f"식별자에 {self.quote}나 NUL을 쓸 수 없습니다 -> {name!r}")
        return f"{self.quote}{text}{self.quote}"

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
