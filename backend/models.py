import re
from enum import Enum
from uuid import UUID
from typing import Literal
from pydantic import BaseModel, field_validator, model_validator

from compiler import GRAINS, ROLLUPS


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


class JoinOn(BaseModel):
    """ON 조건 한 쌍. 양쪽 모두 '테이블.컬럼'."""

    left: str
    right: str


class JoinSpec(BaseModel):
    """지표의 기본 테이블에 붙는 조인 하나"""

    table: str
    type: Literal["inner", "left"] = "inner"
    on: list[JoinOn]


def _clean_names(values: list[str]) -> list[str]:
    seen, out = set(), []
    for v in values:
        text = (v or "").strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


def _dimension(item):
    """디멘젼 하나: 컬럼 이름이나 {"field": 날짜컬럼, "grain": "day"}"""
    if isinstance(item, str):
        text = item.strip()
        if not text:
            raise ValueError("디멘젼 컬럼이 비어 있습니다")
        return text
    if isinstance(item, dict) and str(item.get("field") or "").strip():
        grain = str(item.get("grain") or "").strip().lower()
        if grain and grain not in GRAINS:
            raise ValueError(f"grain 은 {', '.join(GRAINS)} 중 하나입니다 -> {grain!r}")
        field = str(item["field"]).strip()
        return {"field": field, "grain": grain} if grain else field
    raise ValueError(f'디멘젼은 컬럼 이름이나 {{"field", "grain"}} 입니다 -> {item!r}')


class MetricInput(BaseModel):
    """업무 지표 = 테이블(조인) + 디멘젼 + 허용 계산식.

    무엇을 조회·집계할지는 질문마다 LLM 이 정한다. 디멘젼은 집계 계산식이 뜻을 갖는 단위다
    (예: 장비 × 일 — 누적값의 MAX - MIN 이 하루 가동시간이 되는 단위). fixed_filters 는 컴파일러가 늘 붙인다.
    """

    name: str
    description: str = ""
    table_name: str
    joins: list[JoinSpec] = []
    dimensions: list[str | dict] = []
    formulas: list[str] = []
    fixed_filters: list[dict] = []
    synonyms: list[str] = []

    @field_validator("synonyms", "formulas")
    @classmethod
    def _clean_lists(cls, values: list[str]) -> list[str]:
        return _clean_names(values)

    @field_validator("dimensions")
    @classmethod
    def _clean_dimensions(cls, values: list) -> list:
        out = []
        for v in values:
            d = _dimension(v)
            if d not in out:
                out.append(d)
        return out

    @field_validator("name", "table_name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned


class Metric(BaseModel):
    """업무 지표"""

    id: UUID
    name: str
    description: str
    table_name: str
    joins: list[JoinSpec]
    dimensions: list[str | dict]
    formulas: list[str]
    fixed_filters: list[dict]
    synonyms: list[str]
    status: Literal["draft", "active", "broken", "retired"]
    broken_reason: str | None
    version: int
    updated_at: str | None


class FormulaMode(str, Enum):
    FORCED = "forced"  # 질문에 '계산된 <이름>' 이 있을 때만 쓴다
    ORGANIC = "organic"  # LLM 이 맞다고 보면 이름이 없어도 쓴다


class FormulaInput(BaseModel):
    """시스템 계산식. expr 이 없고 source_sql 이 있으면 SQL 을 풀어 expr 을 만든다."""

    name: str
    description: str = ""
    column_name: str | None = None
    mode: FormulaMode = FormulaMode.ORGANIC
    expr: dict | None = None
    source_sql: str | None = None
    rollup: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        cleaned = re.sub(r"\s+", " ", (value or "").strip())
        if not cleaned:
            raise ValueError("must not be blank")
        if "[" in cleaned or "]" in cleaned:
            raise ValueError("계산식 이름에는 [ ] 를 쓸 수 없습니다")
        return cleaned

    @field_validator("column_name", "source_sql")
    @classmethod
    def _blank_none(cls, value):
        text = (value or "").strip()
        return text or None

    @field_validator("rollup")
    @classmethod
    def _rollup(cls, value):
        if value is None or not str(value).strip():
            return None
        text = str(value).strip().upper()
        if text not in ROLLUPS:
            raise ValueError(f"rollup 은 {', '.join(ROLLUPS)} 중 하나입니다")
        return text

    @model_validator(mode="after")
    def _has_body(self):
        if self.expr is None and not self.source_sql:
            raise ValueError("계산식은 식(expr)이나 SQL(source_sql)이 필요합니다")
        return self


class Formula(BaseModel):
    """저장된 계산식. level 은 식에서 정해진다 (집계 함수가 있으면 aggregate)."""

    id: UUID
    name: str
    description: str
    column_name: str | None
    mode: FormulaMode
    expr: dict
    text: str
    level: Literal["row", "aggregate"]
    source_sql: str | None
    rollup: str | None
    refs: list[str]
    used_by: list[str]
    updated_at: str | None


class FormulaParseRequest(BaseModel):
    sql: str


class ParsedFormula(BaseModel):
    name: str | None
    expr: dict
    text: str
    level: Literal["row", "aggregate"]


class FormulaParseResponse(BaseModel):
    """SQL 을 푼 결과. 디멘젼·테이블은 지표 등록에 쓸 후보, outer 는 질문이 정할 바깥 집계 참고."""

    formulas: list[ParsedFormula]
    dimensions: list[str | dict]
    table: str | None
    outer: list[str]


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


class TimeColumn(BaseModel):
    """시각을 포함한 날짜 컬럼과 UTC 보정값 (None = 보정 없음)"""

    name: str
    data_type: str
    utc_offset_hours: int | None = None


class TableInfo(BaseModel):
    """테이블 목록 화면용"""

    id: UUID
    name: str
    comment: str
    purpose: str
    column_count: int
    metric_count: int
    time_columns: list[TimeColumn] = []


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


class TablePurposeInput(BaseModel):
    """사람이 적는 테이블 용도. 테이블 추론에 쓰인다."""

    purpose: str


class ApiException(Exception):
    """API 예외"""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
