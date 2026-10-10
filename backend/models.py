import re
from enum import Enum
from uuid import UUID
from typing import Literal
from pydantic import BaseModel, field_validator, model_validator

from compiler import MEASURE_ROW_SERIES, SERIES_FUNCTIONS, measure_functions


class Driver(str, Enum):
    MYSQL = "mysql"
    POSTGRESQL = "postgresql"
    ORACLE = "oracle"


DEFAULT_PORTS = {
    Driver.MYSQL: 3306,
    Driver.POSTGRESQL: 5432,
    Driver.ORACLE: 1521,
}


class SystemInput(BaseModel):
    """시스템 등록 화면이 보내는 것. 수정할 때 password 를 비우면 기존 비밀번호를 둔다."""
    code: str
    name: str
    domain_desc: str = ""
    driver: Driver
    host: str
    port: int = 0
    db_name: str
    db_schema: str = ""
    username: str = ""
    password: str = ""

    @field_validator("name")
    @classmethod
    def _name_usable(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned

    @field_validator("code")
    @classmethod
    def _code_usable(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", cleaned):
            raise ValueError("코드는 영문·숫자·_·- 로 40자 이내여야 합니다")
        return cleaned

    @field_validator("host", "db_name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def _port_defaulted(self):
        if self.port == 0:
            self.port = DEFAULT_PORTS[self.driver]
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        return self


class System(BaseModel):
    """저장된 시스템 (비밀번호는 내보내지 않는다)"""
    id: UUID
    code: str
    name: str
    domain_desc: str
    driver: Driver
    host: str
    port: int
    db_name: str
    db_schema: str
    username: str
    synced_at: str | None
    table_count: int
    metric_count: int
    active_metric_count: int


class MetricKind(str, Enum):
    AGGREGATE = "aggregate"
    PROJECTION = "projection"
    DERIVED = "derived"


AGG_FUNCTIONS = ("SUM", "COUNT", "AVG", "MIN", "MAX", "COUNT_DISTINCT")
# 지표 정의에서만 쓰는 시계열 집계 (compiler.SERIES_FUNCTIONS). 행 순서(series)가 필요하다.
SERIES_AGG_FUNCTIONS = ("DELTA_SUM", "CHANGE_COUNT")


class JoinOn(BaseModel):
    """ON 조건 한 쌍. 양쪽 모두 '테이블.컬럼'."""
    left: str
    right: str


class JoinSpec(BaseModel):
    """지표의 기본 테이블에 붙는 조인 하나"""
    table: str
    type: Literal["inner", "left"] = "inner"
    on: list[JoinOn]


class SeriesSpec(BaseModel):
    """직전 행 비교의 행 순서. 누적 카운터·직전 거래는 이 순서로 직전 행과 비교한다.

    구분·순서는 컬럼 하나(예전 지표)나 목록이다 — 아파트 거래처럼 '지역+단지+면적' 이 한 묶음인 경우.
    """
    partition_by: str | list[str]  # 행을 나누는 컬럼 (예: 차량 ID)
    order_by: str | list[str]      # 시간 순서 컬럼 (예: 수집 일시). 뒤 컬럼은 같은 시각의 순서를 정한다
    baseline: str | None = None  # CHANGE_COUNT 의 정상값 (예: 0)
    # DELTA_SUM 의 한 행 최대 증가폭. 넘으면 리셋 복귀·장비 교체로 튄 값으로 보고 0으로 친다.
    max_step: float | None = None

    @field_validator("partition_by", "order_by")
    @classmethod
    def _not_blank(cls, value):
        if isinstance(value, list):
            cleaned = [str(v or "").strip() for v in value if str(v or "").strip()]
            if not cleaned:
                raise ValueError("구분 컬럼과 순서 컬럼이 필요합니다")
            return cleaned[0] if len(cleaned) == 1 else cleaned
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("구분 컬럼과 순서 컬럼이 필요합니다")
        return cleaned

    @field_validator("max_step")
    @classmethod
    def _positive(cls, value):
        if value is not None and value <= 0:
            raise ValueError("최대 증가폭은 0보다 커야 합니다")
        return value

    @field_validator("baseline", mode="before")
    @classmethod
    def _baseline_text(cls, value):
        if value is None:
            return None
        text = str(value).strip()
        return text or None


class Measure(BaseModel):
    """지표의 출력 컬럼 하나. expr 은 함수를 겹친 식 트리다 (compiler 의 측정값 식).

    {"col": "컬럼"} · {"num": 0} · {"fn": "ROUND", "args": [{"fn": "AVG", "args": [{"col": "금액"}]}, {"num": 0}]}
    """
    name: str = ""
    expr: dict

    @field_validator("name")
    @classmethod
    def _trim(cls, value: str) -> str:
        return (value or "").strip()


class MetricInput(BaseModel):
    """업무 지표 입력.

    집계형(aggregate)은 숫자 하나를, 조회형(projection)은 컬럼 몇 개를, 파생형(derived)은
    집계형 지표를 잇는 수식 하나를 낸다. 어느 쪽이든 fixed_filters는 컴파일러가 강제로 붙인다.
    """
    name: str
    description: str = ""
    kind: MetricKind = MetricKind.AGGREGATE
    table_name: str
    joins: list[JoinSpec] = []
    agg_field: str | None = None
    agg_function: str | None = None
    select_columns: list[str] = []
    expression: str | None = None
    series: SeriesSpec | None = None
    # 집계형·조회형의 출력 컬럼 목록. 있으면 agg_field·agg_function·select_columns 대신 이것을 쓴다.
    measures: list[Measure] = []
    fixed_filters: list[dict] = []
    synonyms: list[str] = []

    @field_validator("synonyms")
    @classmethod
    def _clean_synonyms(cls, values: list[str]) -> list[str]:
        seen, out = set(), []
        for v in values:
            text = (v or "").strip()
            if text and text.lower() not in seen:
                seen.add(text.lower())
                out.append(text)
        return out

    @field_validator("name", "table_name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned

    @model_validator(mode="after")
    def _shape_matches_kind(self):
        # 종류에 맞지 않는 필드를 남겨두면 "집계로 저장했는데 컬럼 목록이 딸려 있는"
        # 행이 생기고, 나중에 어느 쪽이 진짜인지 알 수 없게 된다.
        if self.measures and self.kind is not MetricKind.DERIVED:
            self._shape_measures()
            return self
        self.measures = []
        if self.kind is MetricKind.AGGREGATE:
            if not self.agg_field or not self.agg_function:
                raise ValueError("집계형 지표는 agg_field와 agg_function이 필요합니다")
            function = str(self.agg_function).upper()
            if function not in AGG_FUNCTIONS + SERIES_AGG_FUNCTIONS:
                raise ValueError(f"agg_function은 {', '.join(AGG_FUNCTIONS + SERIES_AGG_FUNCTIONS)} 중 하나여야 합니다")
            self.agg_function = function
            if function in SERIES_AGG_FUNCTIONS:
                if self.series is None:
                    raise ValueError(f"{function} 는 구분 컬럼과 순서 컬럼(series)이 필요합니다")
                if function == "CHANGE_COUNT" and self.series.baseline is None:
                    raise ValueError("CHANGE_COUNT 는 정상값(series.baseline, 예: 0)이 필요합니다")
                if function == "DELTA_SUM":
                    self.series.baseline = None
                else:
                    self.series.max_step = None
            else:
                self.series = None
            self.select_columns = []
            self.expression = None
        elif self.kind is MetricKind.PROJECTION:
            if not self.select_columns:
                raise ValueError("조회형 지표는 select_columns에 컬럼이 하나 이상 필요합니다")
            seen, unique = set(), []
            for column in self.select_columns:
                key = column.lower()
                if key not in seen:
                    seen.add(key)
                    unique.append(column)
            self.select_columns = unique
            self.agg_field = None
            self.agg_function = None
            self.expression = None
            self.series = None
        else:
            if not (self.expression or "").strip():
                raise ValueError("파생 지표는 expression(예: [매출] - [환불])이 필요합니다")
            self.expression = self.expression.strip()
            self.agg_field = None
            self.agg_function = None
            self.select_columns = []
            self.series = None
        return self

    def _shape_measures(self) -> None:
        """측정값 정의: 이름을 채우고, 직전 행 비교를 쓰지 않으면 series 를 지운다."""
        names = set()
        for ms in self.measures:
            if not ms.name:
                if "col" in ms.expr:
                    ms.name = str(ms.expr["col"]).split(".")[-1]
                elif len(self.measures) == 1:
                    ms.name = self.name
                else:
                    raise ValueError("측정값이 여럿이면 각각 이름이 필요합니다")
            if ms.name.lower() in names:
                raise ValueError(f"측정값 이름이 겹칩니다: {ms.name}")
            names.add(ms.name.lower())
        used = set().union(*(measure_functions(ms.expr) for ms in self.measures))
        if used & (set(MEASURE_ROW_SERIES) | set(SERIES_FUNCTIONS)):
            if self.series is None:
                raise ValueError("직전 행 비교(PREV·DELTA·DELTA_SUM·CHANGE_COUNT)에는 구분 컬럼과 순서 컬럼(series)이 필요합니다")
            if "CHANGE_COUNT" in used and self.series.baseline is None:
                raise ValueError("CHANGE_COUNT 는 정상값(series.baseline, 예: 0)이 필요합니다")
            if "CHANGE_COUNT" not in used:
                self.series.baseline = None
            if "DELTA_SUM" not in used:
                self.series.max_step = None
        else:
            self.series = None
        self.agg_field = None
        self.agg_function = None
        self.select_columns = []
        self.expression = None


class Metric(BaseModel):
    """업무 지표"""
    id: UUID
    name: str
    description: str
    kind: MetricKind
    table_name: str
    joins: list[JoinSpec]
    agg_field: str | None
    agg_function: str | None
    select_columns: list[str]
    expression: str | None
    series: SeriesSpec | None
    measures: list[Measure] = []
    fixed_filters: list[dict]
    synonyms: list[str]
    status: Literal["draft", "active", "broken", "retired"]
    broken_reason: str | None
    version: int
    updated_at: str | None


class MetricHistoryEntry(BaseModel):
    """지표 정의의 한 시점"""
    version: int
    action: Literal["create", "update", "delete", "retire"]
    snapshot: dict
    created_at: str


class RelationInput(BaseModel):
    """테이블 관계. left 가 참조하는 쪽(FK), right 가 참조되는 쪽이다."""
    left_table: str
    left_column: str
    right_table: str
    right_column: str
    constraint_name: str = ""

    @field_validator("left_table", "left_column", "right_table", "right_column")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned


class Relation(RelationInput):
    id: UUID
    source: Literal["fk", "manual"]


class AskRequest(BaseModel):
    """질문 요청"""
    system_id: UUID
    question: str


class AskStep(BaseModel):
    """질문 처리의 한 단계 (ask_trace 한 행). 화면이 '왜 이 테이블·지표인가' 를 보여준다."""
    stage: Literal["table", "metric", "sql"]
    candidates: list[str]
    selected: list[str]
    reason: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_ms: int = 0


class AskResponse(BaseModel):
    """질문 응답. clarification 이 있으면 모델이 되물은 것이다 — sql 은 없다."""
    id: UUID | None = None
    sql: str | None
    error: str | None
    attempts: int
    clarification: str | None = None
    selected_tables: list[str] = []
    selected_metrics: list[str] = []
    steps: list[AskStep] = []


class CompileRequest(BaseModel):
    ast: dict


class CompileResponse(BaseModel):
    sql: str | None
    error: str | None


class HistoryEntry(BaseModel):
    id: UUID
    system_id: UUID
    system_name: str
    question: str
    selected_tables: list[str]
    selected_metrics: list[str]
    total_tokens: int
    sql: str | None
    ast: dict | None
    error: str | None
    clarification: str | None
    attempts: int
    elapsed_ms: int
    favorite: bool
    feedback: Literal["up", "down"] | None
    feedback_note: str
    created_at: str


class HistoryPatch(BaseModel):
    """바꿀 것만 보낸다. feedback 을 지우려면 빈 문자열을 보낸다."""
    favorite: bool | None = None
    feedback: Literal["up", "down", ""] | None = None
    feedback_note: str | None = None


class SyncResult(BaseModel):
    """동기화 결과. 병합이라 추가·변경·삭제 건수로 말한다."""
    table_count: int
    column_count: int
    tables_added: int
    tables_changed: int
    tables_removed: int
    synced_at: str
    relation_count: int | None = None
    broken_metrics: list[dict] = []


class SyncLog(BaseModel):
    id: int
    system_id: UUID
    system_name: str
    trigger: Literal["manual", "auto"]
    status: Literal["ok", "error"]
    tables_added: int | None
    tables_changed: int | None
    tables_removed: int | None
    broken_metrics: list[dict]
    error: str | None
    started_at: str
    finished_at: str


class TableInfo(BaseModel):
    """테이블 목록 화면용"""
    id: UUID
    name: str
    comment: str
    purpose: str
    column_count: int
    metric_count: int


class TablePurposeInput(BaseModel):
    """사람이 적는 테이블 용도. 테이블 추론에 쓰인다."""
    purpose: str


class ApiException(Exception):
    """API 예외"""
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
