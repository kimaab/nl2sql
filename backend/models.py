from enum import Enum
from uuid import UUID
from typing import Literal
from pydantic import BaseModel, field_validator, model_validator
from datetime import datetime


class Driver(str, Enum):
    MYSQL = "mysql"
    POSTGRESQL = "postgresql"
    ORACLE = "oracle"


DEFAULT_PORTS = {
    Driver.MYSQL: 3306,
    Driver.POSTGRESQL: 5432,
    Driver.ORACLE: 1521,
}


class DatasourceInput(BaseModel):
    """등록 화면이 보내는 것"""
    name: str
    description: str = ""
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


class Datasource(BaseModel):
    """저장된 데이터소스"""
    id: UUID
    name: str
    description: str
    driver: Driver
    host: str
    port: int
    db_name: str
    db_schema: str
    username: str
    synced_at: str | None
    table_count: int
    metric_count: int


class MetricKind(str, Enum):
    AGGREGATE = "aggregate"
    PROJECTION = "projection"
    DERIVED = "derived"


AGG_FUNCTIONS = ("SUM", "COUNT", "AVG", "MIN", "MAX", "COUNT_DISTINCT")


class JoinOn(BaseModel):
    """ON 조건 한 쌍. 양쪽 모두 '테이블.컬럼'."""
    left: str
    right: str


class JoinSpec(BaseModel):
    """지표의 기본 테이블에 붙는 조인 하나"""
    table: str
    type: Literal["inner", "left"] = "inner"
    on: list[JoinOn]


class MetricExample(BaseModel):
    """지표의 예시 질문. ast 가 있으면 프롬프트의 모범 답안(few-shot)이 된다."""
    question: str
    ast: dict | None = None

    @field_validator("question")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("예시 질문이 비어 있습니다")
        return cleaned


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
    fixed_filters: list[dict] = []
    examples: list[MetricExample] = []

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
        if self.kind is MetricKind.AGGREGATE:
            if not self.agg_field or not self.agg_function:
                raise ValueError("집계형 지표는 agg_field와 agg_function이 필요합니다")
            if str(self.agg_function).upper() not in AGG_FUNCTIONS:
                raise ValueError(f"agg_function은 {', '.join(AGG_FUNCTIONS)} 중 하나여야 합니다")
            self.agg_function = str(self.agg_function).upper()
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
        else:
            if not (self.expression or "").strip():
                raise ValueError("파생 지표는 expression(예: [매출] - [환불])이 필요합니다")
            self.expression = self.expression.strip()
            self.agg_field = None
            self.agg_function = None
            self.select_columns = []
        return self


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
    fixed_filters: list[dict]
    examples: list[MetricExample]
    version: int
    updated_at: str | None


class MetricHistoryEntry(BaseModel):
    """지표 정의의 한 시점"""
    version: int
    action: Literal["create", "update", "delete"]
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


class CodeValue(BaseModel):
    code: str
    label: str

    @field_validator("code", "label", mode="before")
    @classmethod
    def _not_blank(cls, value) -> str:
        cleaned = str(value if value is not None else "").strip()
        if not cleaned:
            raise ValueError("코드와 이름은 비어 있을 수 없습니다")
        return cleaned


class AnnotationInput(BaseModel):
    """컬럼 동의어와 코드값 사전"""
    synonyms: list[str] = []
    codes: list[CodeValue] = []

    @model_validator(mode="after")
    def _clean(self):
        seen, synonyms = set(), []
        for s in self.synonyms:
            text = s.strip()
            if text and text.lower() not in seen:
                seen.add(text.lower())
                synonyms.append(text)
        self.synonyms = synonyms
        for key in ("code", "label"):
            values = [getattr(c, key).lower() for c in self.codes]
            if len(values) != len(set(values)):
                raise ValueError(f"코드 사전에 같은 {'코드' if key == 'code' else '이름'}가 두 번 있습니다")
        return self


class Annotation(AnnotationInput):
    table_name: str
    column_name: str


class AskRequest(BaseModel):
    """질문 요청"""
    datasource_id: UUID
    question: str


class AskResponse(BaseModel):
    """질문 응답. clarification 이 있으면 모델이 되물은 것이다 — sql 은 없다."""
    id: UUID | None = None
    sql: str | None
    error: str | None
    attempts: int
    clarification: str | None = None


class CompileRequest(BaseModel):
    ast: dict


class CompileResponse(BaseModel):
    sql: str | None
    error: str | None


class HistoryEntry(BaseModel):
    id: UUID
    datasource_id: UUID
    datasource_name: str
    question: str
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
    """동기화 결과"""
    table_count: int
    column_count: int
    synced_at: str
    relation_count: int | None = None
    broken_metrics: list[dict] = []


class SyncLog(BaseModel):
    id: int
    datasource_id: UUID
    datasource_name: str
    trigger: Literal["manual", "auto"]
    status: Literal["ok", "error"]
    table_count: int | None
    column_count: int | None
    relation_count: int | None
    broken_metrics: list[dict]
    error: str | None
    started_at: str
    finished_at: str


class ApiException(Exception):
    """API 예외"""
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
