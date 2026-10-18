import re
from enum import Enum
from uuid import UUID
from typing import Literal
from pydantic import BaseModel, field_validator, model_validator



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


def _clean_names(values: list[str]) -> list[str]:
    seen, out = set(), []
    for v in values:
        text = (v or "").strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


class MetricInput(BaseModel):
    """업무 지표 = 이름 · 설명 · 같은 말 + SQL. SQL 은 AST 로 옮겨 저장한다."""

    name: str
    description: str = ""
    synonyms: list[str] = []
    sql: str

    @field_validator("synonyms")
    @classmethod
    def _clean_lists(cls, values: list[str]) -> list[str]:
        return _clean_names(values)

    @field_validator("name", "sql")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned


class Metric(BaseModel):
    """업무 지표. compiled_sql 은 저장된 AST 를 지금 스키마로 컴파일한 것 (깨졌으면 None)."""

    id: UUID
    name: str
    description: str
    synonyms: list[str]
    sql: str
    ast: dict
    compiled_sql: str | None
    tables: list[str]
    status: Literal["active", "broken"]
    broken_reason: str | None
    version: int
    updated_at: str | None


class MetricCheckRequest(BaseModel):
    sql: str


class MetricCheck(BaseModel):
    """등록 전 확인 결과. stage: 실패한 단계 (parse = AST 변환, compile = 컴파일, verify = 결과 비교)"""

    ast: dict | None = None
    sql: str | None = None
    tables: list[str] = []
    warnings: list[str] = []
    rows: int | None = None
    stage: Literal["parse", "compile", "verify"] | None = None
    error: str | None = None


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
    """질문 응답. clarification 이 있으면 모델이 되물은 것이다 — sql 은 없다.
    needs_metric 이면 지표가 필요한 질문인데 맞는 지표가 없다는 뜻이다 — error 에 이유가 있다."""

    id: UUID | None = None
    sql: str | None
    error: str | None
    attempts: int
    clarification: str | None = None
    selected_tables: list[str] = []
    selected_metrics: list[str] = []
    needs_metric: bool = False
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


class TimeColumn(BaseModel):
    """시각을 포함한 날짜 컬럼과 UTC 보정값 (None = 보정 없음)"""

    name: str
    data_type: str
    utc_offset_hours: int | None = None


class ColumnInfo(BaseModel):
    """컬럼과 '계산 필요' 표시. 계산 필요 컬럼을 계산해야 답할 수 있는 질문은 지표가 필요하다."""

    name: str
    data_type: str
    comment: str
    needs_calc: bool = False


class TableInfo(BaseModel):
    """테이블 목록 화면용"""

    id: UUID
    name: str
    comment: str
    purpose: str
    column_count: int
    metric_count: int
    time_columns: list[TimeColumn] = []
    columns: list[ColumnInfo] = []


class UtcOffsetInput(BaseModel):
    """시각 컬럼의 UTC 보정값. DB 가 UTC 로 저장할 때 현지 시차(예: KST = 9)를 적는다."""

    column: str
    utc_offset_hours: int | None = None

    @field_validator("utc_offset_hours")
    @classmethod
    def _range(cls, value):
        if value == 0:
            return None
        if value is not None and not -12 <= value <= 14:
            raise ValueError("UTC 보정값은 -12 ~ +14 시간입니다")
        return value


class NeedsCalcInput(BaseModel):
    """컬럼의 '계산 필요' 표시 (예: 누적값 — 그대로 집계하면 틀린다)"""

    column: str
    needs_calc: bool


class TablePurposeInput(BaseModel):
    """사람이 적는 테이블 용도. 테이블 추론에 쓰인다."""

    purpose: str


class ApiException(Exception):
    """API 예외"""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
