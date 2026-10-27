import re
from enum import Enum
from uuid import UUID
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator



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
    db_schemas: list[str] = []  # 첫 스키마가 기본 스키마. 비우면 PostgreSQL 은 public, Oracle 은 계정
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

    @field_validator("db_schemas")
    @classmethod
    def _schemas_usable(cls, values: list[str]) -> list[str]:
        cleaned = _clean_names(values)
        for name in cleaned:
            if re.search(r'[.,"`\x00]', name):
                raise ValueError(f"스키마 이름에 . , \" ` 를 쓸 수 없습니다 -> {name}")
        return cleaned

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
    db_schemas: list[str]
    username: str
    synced_at: str | None
    table_count: int
    metric_count: int
    active_metric_count: int
    superset_database_id: int | None = None  # Superset 에 등록한 DB 연결 id (없으면 미연결)


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
    """등록 전 확인 결과. stage: 실패한 단계 (parse = AST 변환, compile = 컴파일, guard = 실행 전 확인, verify = 결과 비교)"""

    ast: dict | None = None
    sql: str | None = None
    tables: list[str] = []
    warnings: list[str] = []
    rows: int | None = None
    stage: Literal["parse", "compile", "guard", "verify"] | None = None
    error: str | None = None


class MissingMetric(BaseModel):
    """지표가 없는 계산 하나. 테이블 선택 단계가 적는다 — 지표 초안의 출발점이다."""

    name: str
    table: str
    column: str = ""


class MetricDraftRequest(BaseModel):
    question: str
    missing: MissingMetric


class ValueSample(BaseModel):
    """대상 컬럼의 실제 값 샘플. 인덱스를 타는 조회로만 뽑는다 — 쓸 인덱스가 없으면 rows 가 비고 note 에 이유가 있다.

    queries: 실제로 실행한 조회, latest: 데이터의 가장 최근 시각 (초안의 예시 기간 기준)
    """

    index: str | None = None
    queries: list[str] = []
    columns: list[str] = []
    rows: list[list[str]] = []
    latest: str | None = None
    note: str = ""


class MetricDraft(BaseModel):
    """LLM 이 쓴 지표 초안. 저장하지 않는다 — 사람이 고치고 확인한 뒤 지표 등록으로 저장한다.

    unsure: 계산 방식을 정하지 못한 이유 (그때 sql 은 비어 있다). check: 초안 SQL 의 등록 확인 결과
    full_scans: 초안 SQL 의 실행 계획이 전체를 훑는 큰 테이블 (인덱스를 타지 못한다)
    """

    name: str = ""
    description: str = ""
    synonyms: list[str] = []
    sql: str = ""
    assumptions: list[str] = []
    unsure: str | None = None
    attempts: int = 0
    sample: ValueSample
    check: MetricCheck | None = None
    full_scans: list[str] = []


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
    source: Literal["fk", "manual", "auto"]  # auto: 컬럼 이름 · 타입 · PK 로 추론 (describe.py)


class AskRequest(BaseModel):
    """질문 요청"""

    system_id: UUID
    question: str
    user_id: str = ""  # 질문한 사용자 (로그인 아이디). 질문의 '나', '내 권한' 이 이 사용자다


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
    needs_metric 이면 지표가 필요한 질문인데 맞는 지표가 없다는 뜻이다 — error 에 이유가, missing 에 없는 계산이 있다."""

    id: UUID | None = None
    sql: str | None
    error: str | None
    attempts: int
    clarification: str | None = None
    selected_tables: list[str] = []
    selected_metrics: list[str] = []
    needs_metric: bool = False
    missing: list[MissingMetric] = []
    steps: list[AskStep] = []


class SqlLabLink(BaseModel):
    url: str


class SupersetChart(BaseModel):
    """Superset 에 만든 차트. url 은 차트 편집 화면(Explore), viz_type 은 고른 차트 종류."""

    url: str
    viz_type: str
    dataset_id: int
    chart_id: int


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
    ai_comment: str = ""  # 코멘트가 없을 때 추론한 설명 (describe.py)
    note: str = ""  # 사람이 적은 설명. 코멘트와 추론한 설명보다 먼저 쓴다
    needs_calc: bool = False


class TableInfo(BaseModel):
    """테이블 목록 화면용"""

    id: UUID
    name: str
    comment: str
    ai_comment: str = ""  # 코멘트가 없을 때 추론한 설명 (describe.py)
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


class ColumnNoteInput(BaseModel):
    """사람이 적는 컬럼 설명. 비우면 대상 DB 코멘트나 추론한 설명으로 돌아간다."""

    column: str
    note: str = Field(default="", max_length=2000)


class TablePurposeInput(BaseModel):
    """사람이 적는 테이블 용도. 테이블 추론에 쓰인다."""

    purpose: str


RunStatus = Literal["running", "done", "error", "interrupted"]


class AutoRunRequest(BaseModel):
    """지표 자동 등록. tables 를 비우면 지표가 없는 테이블 중 관계(FK)가 많은 것부터 max_tables 개를 고른다.

    allow_full_scan: 실행 계획이 큰 테이블 전체를 훑어도 결과 비교를 돌린다 (테스트 DB 용 — 운영 DB 에는 끈다)
    """

    tables: list[str] = []
    max_tables: int = 5
    per_table: int = 3
    allow_full_scan: bool = False

    @field_validator("tables")
    @classmethod
    def _clean_tables(cls, values: list[str]) -> list[str]:
        return _clean_names(values)

    @field_validator("max_tables")
    @classmethod
    def _max_tables(cls, value: int) -> int:
        if not 1 <= value <= 50:
            raise ValueError("테이블은 한 번에 1 ~ 50개입니다")
        return value

    @field_validator("per_table")
    @classmethod
    def _per_table(cls, value: int) -> int:
        if not 1 <= value <= 10:
            raise ValueError("테이블마다 지표 후보는 1 ~ 10개입니다")
        return value


class AutoTry(BaseModel):
    """후보 하나의 시도 한 번"""

    sql: str
    stage: str | None = None
    error: str | None = None


class AutoItem(BaseModel):
    """자동 등록 후보 하나. status: registered = 지표로 저장, failed = 확인 실패, skipped = 이름 중복 등으로 건너뜀"""

    id: int
    table_name: str
    name: str
    description: str
    synonyms: list[str]
    sql: str
    rationale: str
    status: Literal["registered", "failed", "skipped"]
    stage: str | None
    reason: str
    attempts: int
    tries: list[AutoTry]
    check: MetricCheck | None
    full_scans: list[str]
    metric_id: UUID | None
    created_at: str


class AutoRun(BaseModel):
    id: UUID
    system_id: UUID
    status: RunStatus
    options: dict
    tables: list[str]
    tables_done: int
    proposed: int
    registered: int
    failed: int
    skipped: int
    prompt_tokens: int
    completion_tokens: int
    error: str | None
    started_at: str
    finished_at: str | None


class AutoRunDetail(AutoRun):
    items: list[AutoItem]


class ValidationRequest(BaseModel):
    """등록된 지표 검증. metric_ids 에 고른 지표만 검증한다 (비우면 돌리지 않는다).

    run_sql: 대상 DB 에서 결과 비교 · 결과 점검 · 실행 계획을 본다. use_llm: LLM 이 SQL 이 이름 · 설명과 맞는지 검토한다.
    allow_full_scan: 실행 계획이 큰 테이블 전체를 훑어도 결과 비교 · 결과 점검을 돌린다 (테스트 DB 용)
    """

    metric_ids: list[UUID] = []
    run_sql: bool = True
    use_llm: bool = True
    allow_full_scan: bool = False


class ValidationCheck(BaseModel):
    """점검 하나. status: pass | warn | fail | skip (앞 단계가 실패했거나 옵션으로 끈 것)"""

    key: str
    label: str
    status: Literal["pass", "warn", "fail", "skip"]
    message: str
    detail: dict = {}


class ValidationItem(BaseModel):
    id: int
    run_id: UUID
    metric_id: UUID
    metric_name: str
    metric_version: int
    verdict: Literal["pass", "warn", "fail"]
    summary: str
    checks: list[ValidationCheck]
    elapsed_ms: int
    created_at: str


class ValidationRun(BaseModel):
    id: UUID
    system_id: UUID
    status: RunStatus
    options: dict
    total: int
    done: int
    passed: int
    warned: int
    failed: int
    prompt_tokens: int
    completion_tokens: int
    error: str | None
    started_at: str
    finished_at: str | None


class ValidationRunDetail(ValidationRun):
    items: list[ValidationItem]


class ApiException(Exception):
    """API 예외"""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


class DescribeRequest(BaseModel):
    """테이블 · 컬럼 설명 자동 채우기와 관계 추론 (describe.py)"""

    table_ids: list[UUID] = []  # 비우면 설명이 빈 테이블 모두
    descriptions: bool = True  # 비어 있는 설명을 LLM 으로 추론한다
    overwrite: bool = False  # 전에 추론한 설명도 다시 추론한다 (대상 DB 코멘트는 언제나 그대로 둔다)
    relations: bool = True  # 컬럼 이름 · 타입 · PK 로 관계를 추론한다


class DescribeRun(BaseModel):
    id: UUID
    system_id: UUID
    status: RunStatus
    options: dict
    tables_total: int
    tables_done: int
    tables_filled: int
    columns_filled: int
    relations_added: int | None
    failures: list[dict]
    error: str | None
    started_at: str
    finished_at: str | None
