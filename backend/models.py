from enum import Enum
from uuid import UUID
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


class MetricInput(BaseModel):
    """업무 지표 입력"""
    name: str
    description: str = ""
    table_name: str
    agg_field: str
    agg_function: str
    fixed_filters: list[dict] = []


class Metric(BaseModel):
    """업무 지표"""
    id: UUID
    name: str
    description: str
    table_name: str
    agg_field: str
    agg_function: str
    fixed_filters: list[dict]


class AskRequest(BaseModel):
    """질문 요청"""
    datasource_id: UUID
    question: str


class AskResponse(BaseModel):
    """질문 응답"""
    sql: str | None
    error: str | None
    attempts: int


class SyncResult(BaseModel):
    """동기화 결과"""
    table_count: int
    column_count: int
    synced_at: str


class ApiException(Exception):
    """API 예외"""
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
