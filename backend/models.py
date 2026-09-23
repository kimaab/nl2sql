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


class MetricKind(str, Enum):
    AGGREGATE = "aggregate"
    PROJECTION = "projection"


AGG_FUNCTIONS = ("SUM", "COUNT", "AVG", "MIN", "MAX")


class MetricInput(BaseModel):
    """업무 지표 입력.

    집계형(aggregate)은 숫자 하나를, 조회형(projection)은 컬럼 몇 개를 낸다.
    어느 쪽이든 fixed_filters는 컴파일러가 강제로 붙인다 — 그것이 지표의 요점이다.
    """
    name: str
    description: str = ""
    kind: MetricKind = MetricKind.AGGREGATE
    table_name: str
    agg_field: str | None = None
    agg_function: str | None = None
    select_columns: list[str] = []
    fixed_filters: list[dict] = []

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
        else:
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
        return self


class Metric(BaseModel):
    """업무 지표"""
    id: UUID
    name: str
    description: str
    kind: MetricKind
    table_name: str
    agg_field: str | None
    agg_function: str | None
    select_columns: list[str]
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
