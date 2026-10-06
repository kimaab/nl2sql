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
JOIN_TYPES = {"inner": "INNER JOIN", "left": "LEFT JOIN"}


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
            raise ValueError(f"조인에 없는 테이블입니다 -> {ref_table!r}")
        table = resolved
    else:
        table = base
    cols = [c for c in scope[table] if c.lower() == ref_col.lower()]
    if not cols:
        raise ValueError(f"{label} -> {table}.{ref_col}")
    return table, cols[0]


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
            table = metric["table"]
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
            projection = []
            aggregations = ast.get("aggregations") or []

        # 테이블 검증 — 조인은 지표 정의에서만 온다(모델은 joins 키를 쓸 수 없다).
        joins = []
        if metric_name and metric.get("joins"):
            scope, joins = build_scope(table, metric["joins"], self.allowed_tables)
            resolved_table = next(iter(scope))
        else:
            resolved_table = self._match(table, self.allowed_tables)
            if resolved_table is None:
                raise ValueError(
                    f"조회할 수 없는 테이블입니다 -> {table!r} "
                    f"(사용 가능: {', '.join(self.allowed_tables)})"
                )
            scope = {resolved_table: self.allowed_tables[resolved_table]}
        # 단일 테이블 SQL 은 예전 모양 그대로 두고, 조인이 있을 때만 컬럼에 테이블을 붙인다.
        qualify = bool(joins)

        if metric_name:
            model_filters = list(ast.get("filters") or [])
            # 지표의 필터가 먼저, 모델이 낸 필터가 뒤에.
            filters = self._metric_filters(metric, model_filters, scope, resolved_table) + model_filters
        else:
            filters = ast.get("filters") or []

        # SELECT 절 생성
        select = []
        for column in projection:
            owner, resolved = resolve_ref(column, scope, resolved_table)
            select.append(self._column_sql(owner, resolved, qualify))

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
            if field == "*":
                target = "*"
            else:
                owner, resolved = resolve_ref(field, scope, resolved_table, "존재하지 않는 컬럼입니다")
                target = self._column_sql(owner, resolved, qualify)
            alias = agg.get("alias") or f"{function.lower()}_{field}"
            select.append(f"{function}({target}) AS {self._alias(alias)}")

        # GROUP BY 절 생성
        group_cols = []
        for col in (ast.get("group_by") or []):
            owner, resolved = resolve_ref(col, scope, resolved_table, "그룹화할 수 없는 컬럼입니다")
            group_cols.append(self._column_sql(owner, resolved, qualify))

        select = group_cols + select

        # SQL 조립
        source = f"FROM {self._identifier(resolved_table)}"
        for kind, joined, pairs in joins:
            on = " AND ".join(
                f"{self._column_sql(lt, lc, True)} = {self._column_sql(rt, rc, True)}"
                for (lt, lc), (rt, rc) in pairs
            )
            source += f" {JOIN_TYPES[kind]} {self._identifier(joined)} ON {on}"
        clauses = [
            f"SELECT {', '.join(select) or '*'}",
            source
        ]
        where = [self._filter(f, scope, resolved_table, qualify) for f in filters]
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
        # 'MEMBER_ID' 와 'TB_ORDER.MEMBER_ID' 는 같은 컬럼이다 — 해석한 (테이블, 컬럼)으로 비교한다.
        touched = set()
        for f in model_filters:
            if isinstance(f, dict):
                try:
                    touched.add(resolve_ref(str(f.get("field", "")), scope, base))
                except ValueError:
                    pass  # 잘못된 컬럼은 _filter 가 구체적인 오류로 돌려준다
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
            key = resolve_ref(field, scope, base, "지표의 고정 필터 컬럼을 찾을 수 없습니다")
            if key in touched:
                continue
            if source == "question":
                raise ValueError(
                    f"지표 {metric['name']!r} 는 {field} 조건을 질문에서 받습니다 -> "
                    f"filters 에 {field} 를 넣어 값이나 기간을 지정하십시오"
                )
            where = f"{key[0]}.{key[1]}"
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
        table, resolved = resolve_ref(str(spec.get("field")), scope, base, "필터링할 수 없는 컬럼입니다")
        column = self._column_sql(table, resolved, qualify)
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

    def _match(self, name, options):
        """대소문자 무시하고 이름 찾기"""
        if not isinstance(name, str):
            return None
        for candidate in options:
            if candidate.lower() == name.lower():
                return candidate
        return None

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
