import os
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
from langchain_openai import ChatOpenAI

import db
from logging_config import configure_logging
from models import (
    Annotation,
    AnnotationInput,
    ApiException,
    AskRequest,
    AskResponse,
    CompileRequest,
    CompileResponse,
    Datasource,
    DatasourceInput,
    HistoryEntry,
    HistoryPatch,
    Metric,
    MetricHistoryEntry,
    MetricInput,
    Relation,
    RelationInput,
    SyncLog,
    SyncResult,
)
from datasources import (
    list_datasources,
    get_datasource,
    create_datasource,
    update_datasource,
    delete_datasource,
    sync_datasource,
    list_sync_logs,
    get_schema,
)
from metrics import list_metrics, create_metric, update_metric, delete_metric, list_metric_history
from relations import list_relations, create_relation, delete_relation
from annotations import list_annotations, put_annotation
from history import record_ask, list_history, patch_history, delete_history, export_eval_cases
from contract import load_contract
from compiler import Compiler
from ask import run_question
from scheduler import AutoSync

configure_logging()
log = logging.getLogger("nl2sql")
log_request = logging.getLogger("nl2sql.request")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """애플리케이션 생명주기"""
    db.init_pool()
    _init_schema()
    auto_sync = AutoSync.from_env(sync_datasource)
    auto_sync.start()

    yield

    auto_sync.stop()
    db.close_pool()


app = FastAPI(title="nl2sql Studio", lifespan=lifespan)

# CORS 설정
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


# 데이터소스 엔드포인트
@app.get("/api/datasources", response_model=list[Datasource])
def get_datasources():
    return list_datasources()


@app.post("/api/datasources", response_model=Datasource, status_code=status.HTTP_201_CREATED)
def post_datasource(req: DatasourceInput):
    return create_datasource(req)


@app.get("/api/datasources/{datasource_id}", response_model=Datasource)
def get_datasource_detail(datasource_id: UUID):
    return get_datasource(datasource_id)


@app.put("/api/datasources/{datasource_id}", response_model=Datasource)
def put_datasource(datasource_id: UUID, req: DatasourceInput):
    return update_datasource(datasource_id, req)


@app.delete("/api/datasources/{datasource_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_datasource_detail(datasource_id: UUID):
    delete_datasource(datasource_id)


@app.post("/api/datasources/{datasource_id}/sync", response_model=SyncResult)
def post_sync(datasource_id: UUID):
    return sync_datasource(datasource_id)


@app.get("/api/sync-logs", response_model=list[SyncLog])
def get_sync_logs(datasource_id: UUID | None = None, limit: int = Query(100, ge=1, le=500)):
    return list_sync_logs(datasource_id, limit)


@app.get("/api/datasources/{datasource_id}/schema")
def get_datasource_schema(datasource_id: UUID):
    get_datasource(datasource_id)
    return get_schema(datasource_id)


# 관계
@app.get("/api/datasources/{datasource_id}/relations", response_model=list[Relation])
def get_relations(datasource_id: UUID):
    get_datasource(datasource_id)
    return list_relations(datasource_id)


@app.post("/api/datasources/{datasource_id}/relations", response_model=Relation, status_code=status.HTTP_201_CREATED)
def post_relation(datasource_id: UUID, req: RelationInput):
    get_datasource(datasource_id)
    return create_relation(datasource_id, req)


@app.delete("/api/datasources/{datasource_id}/relations/{relation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_relation_detail(datasource_id: UUID, relation_id: UUID):
    delete_relation(datasource_id, relation_id)


# 용어·코드 사전
@app.get("/api/datasources/{datasource_id}/annotations", response_model=list[Annotation])
def get_annotations(datasource_id: UUID):
    get_datasource(datasource_id)
    return list_annotations(datasource_id)


@app.put("/api/datasources/{datasource_id}/annotations/{table}/{column}", response_model=Annotation)
def put_annotation_detail(datasource_id: UUID, table: str, column: str, req: AnnotationInput):
    get_datasource(datasource_id)
    return put_annotation(datasource_id, table, column, req)


# 지표 엔드포인트
@app.get("/api/datasources/{datasource_id}/metrics", response_model=list[Metric])
def get_metrics(datasource_id: UUID):
    get_datasource(datasource_id)
    return list_metrics(datasource_id)


@app.post("/api/datasources/{datasource_id}/metrics", response_model=Metric)
def post_metric(datasource_id: UUID, req: MetricInput):
    get_datasource(datasource_id)
    return create_metric(datasource_id, req)


@app.put("/api/datasources/{datasource_id}/metrics/{metric_id}", response_model=Metric)
def put_metric(datasource_id: UUID, metric_id: UUID, req: MetricInput):
    get_datasource(datasource_id)
    return update_metric(datasource_id, metric_id, req)


@app.get("/api/datasources/{datasource_id}/metrics/{metric_id}/history", response_model=list[MetricHistoryEntry])
def get_metric_history(datasource_id: UUID, metric_id: UUID):
    return list_metric_history(datasource_id, metric_id)


@app.delete("/api/datasources/{datasource_id}/metrics/{metric_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_metric_detail(datasource_id: UUID, metric_id: UUID):
    delete_metric(datasource_id, metric_id)


# 모델 없이 AST 를 컴파일해 본다 — 지표 예시 작성과 평가셋 확인용
@app.post("/api/datasources/{datasource_id}/compile", response_model=CompileResponse)
def post_compile(datasource_id: UUID, req: CompileRequest):
    compiler = Compiler(load_contract(datasource_id))
    try:
        return CompileResponse(sql=compiler.compile(req.ast), error=None)
    except (ValueError, KeyError, TypeError) as error:
        return CompileResponse(sql=None, error=str(error))


# 질문 엔드포인트
@app.post("/api/ask", response_model=AskResponse)
def post_ask(req: AskRequest):
    """질문에서 SQL 생성"""
    started = time.monotonic()

    # 매 요청마다 계약서 새로 로드
    contract = load_contract(req.datasource_id)
    model = ChatOpenAI(
        base_url=os.environ.get("LLM_BASE_URL"),
        api_key=os.environ.get("LLM_API_KEY", "not-needed"),
        model=os.environ.get("LLM_MODEL", "gpt-3.5-turbo"),
        temperature=0.0,
    )
    outcome = run_question(contract, req.question, model)
    elapsed_ms = (time.monotonic() - started) * 1000

    if outcome["sql"]:
        result = "success"
    elif outcome["clarification"]:
        result = "clarify"
    else:
        result = "fail"
    log_request.info(
        "datasource=%s question=%r attempts=%d outcome=%s elapsed_ms=%.0f%s",
        contract["name"],
        req.question,
        outcome["attempts"],
        result,
        elapsed_ms,
        f" detail={outcome['error']}" if result == "fail" else "",
    )
    entry_id = record_ask(req.datasource_id, req.question, outcome, elapsed_ms)
    return AskResponse(
        id=entry_id,
        sql=outcome["sql"],
        error=outcome["error"],
        attempts=outcome["attempts"],
        clarification=outcome["clarification"],
    )


# 질문 기록
@app.get("/api/history", response_model=list[HistoryEntry])
def get_history_list(
    datasource_id: UUID | None = None,
    favorite: bool | None = None,
    feedback: str | None = Query(None, pattern="^(up|down)$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    return list_history(datasource_id, favorite, feedback, limit, offset)


@app.get("/api/history/export")
def get_history_export(datasource_id: UUID):
    return export_eval_cases(datasource_id)


@app.patch("/api/history/{entry_id}", response_model=HistoryEntry)
def patch_history_entry(entry_id: UUID, req: HistoryPatch):
    return patch_history(entry_id, req)


@app.delete("/api/history/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_history_entry(entry_id: UUID):
    delete_history(entry_id)


@app.get("/health")
def health_check():
    """헬스 체크"""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
