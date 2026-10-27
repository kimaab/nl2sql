import time
import logging
from pathlib import Path
from uuid import UUID
from contextlib import asynccontextmanager

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
# 어느 디렉터리에서 서버를 띄우든 backend/.env를 읽는다.
load_dotenv(BASE_DIR / ".env")

from fastapi import FastAPI, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import db
import secret
from logging_config import configure_logging
from models import (
    ApiException,
    AskRequest,
    AskResponse,
    AskStep,
    CompileRequest,
    CompileResponse,
    HistoryEntry,
    HistoryPatch,
    Metric,
    MetricCheck,
    MetricCheckRequest,
    MetricDraft,
    MetricDraftRequest,
    MetricHistoryEntry,
    MetricInput,
    NeedsCalcInput,
    Relation,
    RelationInput,
    SqlLabLink,
    SupersetChart,
    SyncLog,
    SyncResult,
    System,
    SystemInput,
    TableInfo,
    TablePurposeInput,
    UtcOffsetInput,
)
from systems import (
    create_system,
    delete_system,
    get_schema,
    get_system,
    list_sync_logs,
    list_systems,
    connection_row,
    list_tables,
    set_needs_calc,
    set_table_purpose,
    set_utc_offset,
    sync_system,
    update_system,
)
from metrics import (
    check_metric,
    create_metric,
    delete_metric,
    list_metric_history,
    list_metrics,
    update_metric,
)
from relations import create_relation, delete_relation, list_relations
from history import (
    delete_history,
    export_eval_cases,
    get_history,
    get_trace,
    list_history,
    patch_history,
    record_ask,
)
from contract import load_contract
from compiler import Compiler
from ask import run_question
from metric_draft import draft_metric
from superset_link import create_chart, link_database, sqllab_link
from llm import make_model
from scheduler import AutoSync
from metric_auto_routes import router as metric_auto_router
from describe_routes import router as describe_router

configure_logging()
log = logging.getLogger("nl2sql")
log_request = logging.getLogger("nl2sql.request")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """애플리케이션 생명주기"""
    secret.check()
    db.init_pool()
    _init_schema()
    auto_sync = AutoSync.from_env(sync_system)
    auto_sync.start()

    yield

    auto_sync.stop()
    db.close_pool()


app = FastAPI(title="nl2sql Studio", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# 지표 자동 등록 · 지표 검증 (metric_auto_routes.py)
app.include_router(metric_auto_router)
# 테이블 · 컬럼 설명 자동 채우기 · 관계 추론 (describe_routes.py)
app.include_router(describe_router)


def _init_schema():
    """schema.sql 초기화.

    실패하면 기동을 멈춘다. 로그만 남기고 계속 뜨면 화면은 멀쩡해 보이는데
    모든 요청이 깨지고, 원인이 기동 로그 한 줄에만 남는다.
    """
    schema = (BASE_DIR / "schema.sql").read_text(encoding="utf-8")
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(schema)
    log.info("Database schema initialized")


@app.exception_handler(ApiException)
def _api_exception(request, exc: ApiException):
    """서비스 계층의 ApiException 을 HTTP 오류로"""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# ── 시스템 · 동기화 ─────────────────────────────────────────────────────
@app.get("/api/systems", response_model=list[System])
def get_systems():
    return list_systems()


@app.post("/api/systems", response_model=System, status_code=status.HTTP_201_CREATED)
def post_system(req: SystemInput):
    return create_system(req)


@app.get("/api/systems/{system_id}", response_model=System)
def get_system_detail(system_id: UUID):
    return get_system(system_id)


@app.put("/api/systems/{system_id}", response_model=System)
def put_system(system_id: UUID, req: SystemInput):
    return update_system(system_id, req)


@app.delete("/api/systems/{system_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_system_detail(system_id: UUID):
    delete_system(system_id)


@app.post("/api/systems/{system_id}/sync", response_model=SyncResult)
def post_sync(system_id: UUID):
    return sync_system(system_id)


# 시스템을 Superset DB 연결로 등록한다 (이미 있으면 접속 정보 갱신). 데이터는 옮기지 않는다.
@app.post("/api/systems/{system_id}/superset", response_model=System)
def post_superset(system_id: UUID):
    return link_database(system_id)


# Superset 으로 보내는 SQL 은 화면에서 받지 않는다 — 질문 기록에 남은 생성 SQL 만 쓴다.
# 화면이 보낸 SQL 을 받으면 API 를 직접 불러 아무 SQL 이나 대상 DB 에 실행시킬 수 있다.
def _generated_sql(entry_id: UUID) -> HistoryEntry:
    entry = get_history(entry_id)
    if not entry.sql:
        raise ApiException(400, "SQL 을 만들지 못한 질문입니다")
    return entry


# 생성한 SQL 을 Superset SQL Lab 에 열어 두는 링크. 실행은 SQL Lab 에서 사람이 한다.
@app.post("/api/history/{entry_id}/superset/sqllab", response_model=SqlLabLink)
def post_superset_sqllab(entry_id: UUID):
    entry = _generated_sql(entry_id)
    return SqlLabLink(url=sqllab_link(entry.system_id, entry.sql, entry.question))


# 생성한 SQL 로 Superset 차트를 만든다. 결과 컬럼 모양으로 차트 종류를 정하고, 같은 SQL 이면 전에 만든 차트를 연다.
@app.post("/api/history/{entry_id}/superset/chart", response_model=SupersetChart)
def post_superset_chart(entry_id: UUID):
    entry = _generated_sql(entry_id)
    return SupersetChart(**create_chart(entry.system_id, entry.sql, entry.question))


@app.get("/api/sync-logs", response_model=list[SyncLog])
def get_sync_logs(system_id: UUID | None = None, limit: int = Query(100, ge=1, le=500)):
    return list_sync_logs(system_id, limit)


@app.get("/api/systems/{system_id}/schema")
def get_system_schema(system_id: UUID):
    get_system(system_id)
    return get_schema(system_id)


@app.get("/api/systems/{system_id}/tables", response_model=list[TableInfo])
def get_tables(system_id: UUID):
    get_system(system_id)
    return list_tables(system_id)


@app.put("/api/systems/{system_id}/tables/{table_id}/purpose", response_model=TableInfo)
def put_table_purpose(system_id: UUID, table_id: UUID, req: TablePurposeInput):
    return set_table_purpose(system_id, table_id, req.purpose)


@app.put("/api/systems/{system_id}/tables/{table_id}/utc-offset", response_model=TableInfo)
def put_utc_offset(system_id: UUID, table_id: UUID, req: UtcOffsetInput):
    return set_utc_offset(system_id, table_id, req.column, req.utc_offset_hours)


@app.put("/api/systems/{system_id}/tables/{table_id}/needs-calc", response_model=TableInfo)
def put_needs_calc(system_id: UUID, table_id: UUID, req: NeedsCalcInput):
    return set_needs_calc(system_id, table_id, req.column, req.needs_calc)


# ── 관계 ───────────────────────────────────────────────────────────────
@app.get("/api/systems/{system_id}/relations", response_model=list[Relation])
def get_relations(system_id: UUID):
    get_system(system_id)
    return list_relations(system_id)


@app.post(
    "/api/systems/{system_id}/relations",
    response_model=Relation,
    status_code=status.HTTP_201_CREATED,
)
def post_relation(system_id: UUID, req: RelationInput):
    get_system(system_id)
    return create_relation(system_id, req)


@app.delete(
    "/api/systems/{system_id}/relations/{relation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_relation_detail(system_id: UUID, relation_id: UUID):
    delete_relation(system_id, relation_id)


# ── 지표 ────────────────────────────────────────────────────────────────
@app.get("/api/systems/{system_id}/metrics", response_model=list[Metric])
def get_metrics(system_id: UUID):
    get_system(system_id)
    return list_metrics(system_id)


@app.post("/api/systems/{system_id}/metrics", response_model=Metric)
def post_metric(system_id: UUID, req: MetricInput):
    get_system(system_id)
    return create_metric(system_id, req)


@app.put("/api/systems/{system_id}/metrics/{metric_id}", response_model=Metric)
def put_metric(system_id: UUID, metric_id: UUID, req: MetricInput):
    get_system(system_id)
    return update_metric(system_id, metric_id, req)


# 저장하지 않고 등록 절차만 밟아 본다: SQL → AST → SQL → 대상 DB 에서 결과 비교
@app.post("/api/systems/{system_id}/metrics/check", response_model=MetricCheck)
def post_metric_check(system_id: UUID, req: MetricCheckRequest):
    get_system(system_id)
    return check_metric(system_id, req.sql)


# 지표가 없어 멈춘 질문에서 지표 SQL 초안을 쓴다 (저장하지 않는다). 값 샘플은 인덱스를 타는 조회로만 읽는다.
@app.post("/api/systems/{system_id}/metrics/draft", response_model=MetricDraft)
def post_metric_draft(system_id: UUID, req: MetricDraftRequest):
    return draft_metric(system_id, req, make_model(), connection_row(system_id))


@app.get(
    "/api/systems/{system_id}/metrics/{metric_id}/history",
    response_model=list[MetricHistoryEntry],
)
def get_metric_history(system_id: UUID, metric_id: UUID):
    return list_metric_history(system_id, metric_id)


@app.delete(
    "/api/systems/{system_id}/metrics/{metric_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_metric_detail(system_id: UUID, metric_id: UUID):
    delete_metric(system_id, metric_id)


# 모델 없이 AST 를 컴파일해 본다
@app.post("/api/systems/{system_id}/compile", response_model=CompileResponse)
def post_compile(system_id: UUID, req: CompileRequest):
    compiler = Compiler(load_contract(system_id))
    try:
        return CompileResponse(sql=compiler.compile(req.ast), error=None)
    except (ValueError, KeyError, TypeError) as error:
        return CompileResponse(sql=None, error=str(error))


# ── 질문 ────────────────────────────────────────────────────────────────
@app.post("/api/ask", response_model=AskResponse)
def post_ask(req: AskRequest):
    """질문에서 SQL 생성: 테이블 선택(지표 필요 판단) → 조회 명세 → 컴파일"""
    started = time.monotonic()
    contract = load_contract(req.system_id, active_only=True)

    outcome = run_question(contract, req.question, make_model())
    elapsed_ms = (time.monotonic() - started) * 1000

    result = (
        "success"
        if outcome["sql"]
        else "clarify"
        if outcome["clarification"]
        else "needs_metric"
        if outcome["needs_metric"]
        else "fail"
    )
    log_request.info(
        "system=%s question=%r tables=%s metrics=%s attempts=%d tokens=%d outcome=%s elapsed_ms=%.0f%s",
        contract["code"],
        req.question,
        outcome["tables"],
        outcome["metrics"],
        outcome["attempts"],
        outcome["total_tokens"],
        result,
        elapsed_ms,
        f" detail={outcome['error']}" if result in ("fail", "needs_metric") else "",
    )
    entry_id = record_ask(req.system_id, req.question, outcome, elapsed_ms)
    steps = []
    for s in outcome["steps"]:
        fields = dict(s)
        fields.pop("model", None)
        steps.append(AskStep(**fields))
    return AskResponse(
        id=entry_id,
        sql=outcome["sql"],
        error=outcome["error"],
        attempts=outcome["attempts"],
        clarification=outcome["clarification"],
        selected_tables=outcome["tables"],
        selected_metrics=outcome["metrics"],
        needs_metric=outcome["needs_metric"],
        missing=outcome["missing"],
        steps=steps,
    )


# ── 질문 기록 ───────────────────────────────────────────────────────────
@app.get("/api/history", response_model=list[HistoryEntry])
def get_history_list(
    system_id: UUID | None = None,
    favorite: bool | None = None,
    feedback: str | None = Query(None, pattern="^(up|down)$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    return list_history(system_id, favorite, feedback, limit, offset)


@app.get("/api/history/export")
def get_history_export(system_id: UUID):
    return export_eval_cases(system_id)


@app.get("/api/history/{entry_id}/trace", response_model=list[AskStep])
def get_history_trace(entry_id: UUID):
    return get_trace(entry_id)


@app.patch("/api/history/{entry_id}", response_model=HistoryEntry)
def patch_history_entry(entry_id: UUID, req: HistoryPatch):
    return patch_history(entry_id, req)


@app.delete("/api/history/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_history_entry(entry_id: UUID):
    delete_history(entry_id)


@app.get("/health")
def health_check():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
