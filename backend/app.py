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
from pydantic import BaseModel

import db
import enrich
import secret
from logging_config import configure_logging
from models import (
    Annotation, AnnotationInput, ApiException, AskRequest, AskResponse, AskStep, CompileRequest, CompileResponse,
    EnrichStatus, EvalRun, GlossaryInput, GlossaryTerm, HistoryEntry, HistoryPatch, Metric, MetricHistoryEntry,
    MetricInput, Relation, RelationInput, ReviewDecision, ReviewItem, SyncLog, SyncResult, System, SystemInput,
    TableInfo, TablePurposeInput,
)
from systems import (
    create_system, delete_system, get_schema, get_system, list_sync_logs, list_systems, list_tables,
    set_table_purpose, sync_system, update_system,
)
from metrics import create_metric, delete_metric, list_metric_history, list_metrics, preview_metric, update_metric
from relations import create_relation, delete_relation, list_relations
from annotations import list_annotations, put_annotation
from glossary import delete_term, list_terms, save_term
from history import (
    delete_history, export_eval_cases, get_trace, list_history, patch_history, promote_to_eval_case, record_ask,
)
from contract import load_contract
from compiler import Compiler
from ask import run_question
from llm import make_model
from scheduler import AutoSync

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


# ── 관계 · 컬럼 사전 · 업무 용어 ─────────────────────────────────────────
@app.get("/api/systems/{system_id}/relations", response_model=list[Relation])
def get_relations(system_id: UUID):
    get_system(system_id)
    return list_relations(system_id)


@app.post("/api/systems/{system_id}/relations", response_model=Relation, status_code=status.HTTP_201_CREATED)
def post_relation(system_id: UUID, req: RelationInput):
    get_system(system_id)
    return create_relation(system_id, req)


@app.delete("/api/systems/{system_id}/relations/{relation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_relation_detail(system_id: UUID, relation_id: UUID):
    delete_relation(system_id, relation_id)


@app.get("/api/systems/{system_id}/annotations", response_model=list[Annotation])
def get_annotations(system_id: UUID):
    get_system(system_id)
    return list_annotations(system_id)


@app.put("/api/systems/{system_id}/annotations/{table}/{column}", response_model=Annotation)
def put_annotation_detail(system_id: UUID, table: str, column: str, req: AnnotationInput):
    get_system(system_id)
    return put_annotation(system_id, table, column, req)


@app.get("/api/systems/{system_id}/glossary", response_model=list[GlossaryTerm])
def get_glossary(system_id: UUID):
    get_system(system_id)
    return list_terms(system_id)


@app.post("/api/systems/{system_id}/glossary", response_model=GlossaryTerm, status_code=status.HTTP_201_CREATED)
def post_glossary(system_id: UUID, req: GlossaryInput):
    get_system(system_id)
    return save_term(system_id, req)


@app.put("/api/systems/{system_id}/glossary/{term_id}", response_model=GlossaryTerm)
def put_glossary(system_id: UUID, term_id: int, req: GlossaryInput):
    return save_term(system_id, req, term_id)


@app.delete("/api/systems/{system_id}/glossary/{term_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_glossary(system_id: UUID, term_id: int):
    delete_term(system_id, term_id)


# 전사 공통 용어 (system_id 없음)
@app.get("/api/glossary", response_model=list[GlossaryTerm])
def get_global_glossary():
    return list_terms(None)


@app.post("/api/glossary", response_model=GlossaryTerm, status_code=status.HTTP_201_CREATED)
def post_global_glossary(req: GlossaryInput):
    return save_term(None, req)


@app.put("/api/glossary/{term_id}", response_model=GlossaryTerm)
def put_global_glossary(term_id: int, req: GlossaryInput):
    return save_term(None, req, term_id)


@app.delete("/api/glossary/{term_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_global_glossary(term_id: int):
    delete_term(None, term_id)


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


# 저장하지 않고 정의를 SQL 로 — 등록 화면의 미리보기
@app.post("/api/systems/{system_id}/metrics/preview", response_model=CompileResponse)
def post_metric_preview(system_id: UUID, req: MetricInput):
    get_system(system_id)
    try:
        return CompileResponse(sql=preview_metric(system_id, req), error=None)
    except ApiException as error:
        return CompileResponse(sql=None, error=str(error.detail))
    except (ValueError, KeyError, TypeError) as error:
        return CompileResponse(sql=None, error=str(error))


@app.get("/api/systems/{system_id}/metrics/{metric_id}/history", response_model=list[MetricHistoryEntry])
def get_metric_history(system_id: UUID, metric_id: UUID):
    return list_metric_history(system_id, metric_id)


@app.delete("/api/systems/{system_id}/metrics/{metric_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_metric_detail(system_id: UUID, metric_id: UUID):
    delete_metric(system_id, metric_id)


# 모델 없이 AST 를 컴파일해 본다 — 지표 예시 작성과 평가셋 확인용
@app.post("/api/systems/{system_id}/compile", response_model=CompileResponse)
def post_compile(system_id: UUID, req: CompileRequest):
    compiler = Compiler(load_contract(system_id))
    try:
        return CompileResponse(sql=compiler.compile(req.ast), error=None)
    except (ValueError, KeyError, TypeError) as error:
        return CompileResponse(sql=None, error=str(error))


# ── 메타데이터 보강 · 검수 ───────────────────────────────────────────────
class EnrichRequest(BaseModel):
    enqueue_all: bool = False   # 카드·예시가 없는 것을 모두 대기열에 넣고 시작


@app.get("/api/systems/{system_id}/enrich", response_model=EnrichStatus)
def get_enrich_status(system_id: UUID):
    get_system(system_id)
    return enrich.status(system_id)


@app.post("/api/systems/{system_id}/enrich", response_model=EnrichStatus)
def post_enrich(system_id: UUID, req: EnrichRequest):
    get_system(system_id)
    if req.enqueue_all:
        enrich.enqueue_all(system_id)
    enrich.start(system_id, make_model(temperature=0.3))
    return enrich.status(system_id)


@app.get("/api/systems/{system_id}/review", response_model=list[ReviewItem])
def get_review(system_id: UUID):
    get_system(system_id)
    return enrich.list_review(system_id)


@app.post("/api/systems/{system_id}/review/{kind}/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def post_review(system_id: UUID, kind: str, item_id: str, req: ReviewDecision):
    enrich.decide(system_id, kind, item_id, req)


@app.get("/api/systems/{system_id}/eval-runs", response_model=list[EvalRun])
def get_eval_runs(system_id: UUID):
    rows = db.query("SELECT * FROM eval_run WHERE system_id = %s ORDER BY started_at DESC LIMIT 50", str(system_id))
    return [EvalRun(**{**r, "table_recall": _num(r["table_recall"]), "metric_accuracy": _num(r["metric_accuracy"]),
                       "sql_accuracy": _num(r["sql_accuracy"]), "started_at": r["started_at"].isoformat(),
                       "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None}) for r in rows]


def _num(value):
    return float(value) if value is not None else None


# ── 질문 ────────────────────────────────────────────────────────────────
@app.post("/api/ask", response_model=AskResponse)
def post_ask(req: AskRequest):
    """질문에서 SQL 생성: 테이블 추론 → 지표 후보 조회 → 지표 선택 → 조회 명세 → 컴파일"""
    started = time.monotonic()
    contract = load_contract(req.system_id, active_only=True)
    outcome = run_question(contract, req.question, make_model())
    elapsed_ms = (time.monotonic() - started) * 1000

    result = "success" if outcome["sql"] else "clarify" if outcome["clarification"] else "fail"
    log_request.info(
        "system=%s question=%r tables=%s metrics=%s attempts=%d tokens=%d outcome=%s elapsed_ms=%.0f%s",
        contract["code"], req.question, outcome["tables"], outcome["metrics"], outcome["attempts"],
        outcome["total_tokens"], result, elapsed_ms, f" detail={outcome['error']}" if result == "fail" else "",
    )
    entry_id = record_ask(req.system_id, req.question, outcome, elapsed_ms)
    return AskResponse(
        id=entry_id, sql=outcome["sql"], error=outcome["error"], attempts=outcome["attempts"],
        clarification=outcome["clarification"], selected_tables=outcome["tables"],
        selected_metrics=outcome["metrics"], steps=[AskStep(**{k: v for k, v in s.items() if k != "model"})
                                                    for s in outcome["steps"]],
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


@app.post("/api/history/{entry_id}/eval-case")
def post_history_eval_case(entry_id: UUID):
    return {"id": promote_to_eval_case(entry_id)}


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
