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
            # 지표의 고정 필터가 먼저, 모델이 낸 필터가 뒤에. 모델이 고정 필터를
            # 잊어도 여기서 항상 붙는다 — 이것이 지표 시스템의 요점이고,
            # 집계형이든 조회형이든 똑같이 적용된다.
            filters = list(metric.get("fixed_filters", [])) + list(ast.get("filters") or [])
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
            filters = ast.get("filters") or []

        # 테이블 검증
        resolved_table = self._match(table, self.allowed_tables)
        if resolved_table is None:
            raise ValueError(
                f"조회할 수 없는 테이블입니다 -> {table!r} "
                f"(사용 가능: {', '.join(self.allowed_tables)})"
            )
        allowed = self.allowed_tables[resolved_table]

        # SELECT 절 생성
        select = []
        for column in projection:
            resolved = self._match(column, allowed)
            if resolved is None:
                raise ValueError(f"조회할 수 없는 컬럼입니다 -> {resolved_table}.{column}")
            select.append(self._identifier(resolved))

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
                resolved = self._match(field, allowed)
                if resolved is None:
                    raise ValueError(f"존재하지 않는 컬럼입니다 -> {resolved_table}.{field}")
                target = self._identifier(resolved)
            alias = agg.get("alias") or f"{function.lower()}_{field}"
            select.append(f"{function}({target}) AS {self._alias(alias)}")

        # GROUP BY 절 생성
        group_cols = []
        for col in (ast.get("group_by") or []):
            resolved = self._match(col, allowed)
            if resolved is None:
                raise ValueError(f"그룹화할 수 없는 컬럼입니다 -> {resolved_table}.{col}")
            group_cols.append(self._identifier(resolved))

        select = group_cols + select

        # SQL 조립
        clauses = [
            f"SELECT {', '.join(select) or '*'}",
            f"FROM {self._identifier(resolved_table)}"
        ]
        where = [self._filter(f, resolved_table, allowed) for f in filters]
        if where:
            clauses.append("WHERE " + " AND ".join(where))
        if group_cols:
            clauses.append("GROUP BY " + ", ".join(group_cols))
        return " ".join(clauses) + ";"

    def _filter(self, spec: dict, table: str, allowed: list) -> str:
        """WHERE 조건 생성"""
        if not isinstance(spec, dict):
            raise ValueError(f"filters의 항목은 객체여야 합니다 -> {spec!r}")
        if "field" not in spec:
            raise ValueError(
                f"filters 항목에 field 키가 없습니다 -> 받은 키: {sorted(spec)} "
                "(모양: " + '{"field": "컬럼명", "operator": "equals", "value": "값"}' + ")"
            )
        resolved = self._match(spec.get("field"), allowed)
        if resolved is None:
            raise ValueError(f"필터링할 수 없는 컬럼입니다 -> {table}.{spec.get('field')}")
        column = self._identifier(resolved)
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
