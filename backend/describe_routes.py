"""테이블 · 컬럼 설명 자동 채우기와 관계 추론 API. 스레드에서 돌고, 화면은 실행 기록을 다시 읽어 진행을 본다."""

from uuid import UUID

from fastapi import APIRouter, Query, status

from describe import get_describe_run, list_describe_runs, start_describe
from llm import make_model
from models import DescribeRequest, DescribeRun
from systems import get_system

router = APIRouter(prefix="/api/systems/{system_id}")


@router.post("/describe/runs", response_model=DescribeRun, status_code=status.HTTP_202_ACCEPTED)
def post_describe_run(system_id: UUID, req: DescribeRequest):
    get_system(system_id)
    return start_describe(system_id, req, make_model() if req.descriptions else None)


@router.get("/describe/runs", response_model=list[DescribeRun])
def get_describe_runs(system_id: UUID, limit: int = Query(50, ge=1, le=200)):
    get_system(system_id)
    return list_describe_runs(system_id, limit)


@router.get("/describe/runs/{run_id}", response_model=DescribeRun)
def get_describe_run_detail(system_id: UUID, run_id: UUID):
    return get_describe_run(system_id, run_id)
