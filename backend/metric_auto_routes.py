"""지표 자동 등록 · 지표 검증 API. 둘 다 스레드에서 돌고, 화면은 실행 기록을 다시 읽어 진행을 본다."""

from uuid import UUID

from fastapi import APIRouter, Query, status

from llm import make_model
from metric_auto import get_run, list_runs, start_run
from metric_validate import get_validation, list_validations, metric_validations, start_validation
from models import (
    AutoRun,
    AutoRunDetail,
    AutoRunRequest,
    ValidationItem,
    ValidationRequest,
    ValidationRun,
    ValidationRunDetail,
)
from systems import get_system

router = APIRouter(prefix="/api/systems/{system_id}")


# ── 지표 자동 등록 ──────────────────────────────────────────────────────────
@router.post("/metric-auto/runs", response_model=AutoRun, status_code=status.HTTP_202_ACCEPTED)
def post_auto_run(system_id: UUID, req: AutoRunRequest):
    get_system(system_id)
    return start_run(system_id, req, make_model())


@router.get("/metric-auto/runs", response_model=list[AutoRun])
def get_auto_runs(system_id: UUID, limit: int = Query(50, ge=1, le=200)):
    get_system(system_id)
    return list_runs(system_id, limit)


@router.get("/metric-auto/runs/{run_id}", response_model=AutoRunDetail)
def get_auto_run(system_id: UUID, run_id: UUID):
    return get_run(system_id, run_id)


# ── 지표 검증 ───────────────────────────────────────────────────────────────
@router.post("/metric-validations", response_model=ValidationRun, status_code=status.HTTP_202_ACCEPTED)
def post_validation(system_id: UUID, req: ValidationRequest):
    get_system(system_id)
    return start_validation(system_id, req, make_model() if req.use_llm else None)


@router.get("/metric-validations", response_model=list[ValidationRun])
def get_validations(system_id: UUID, limit: int = Query(50, ge=1, le=200)):
    get_system(system_id)
    return list_validations(system_id, limit)


@router.get("/metric-validations/{run_id}", response_model=ValidationRunDetail)
def get_validation_detail(system_id: UUID, run_id: UUID):
    return get_validation(system_id, run_id)


@router.get("/metrics/{metric_id}/validations", response_model=list[ValidationItem])
def get_metric_validations(system_id: UUID, metric_id: UUID, limit: int = Query(20, ge=1, le=100)):
    get_system(system_id)
    return metric_validations(system_id, metric_id, limit)
