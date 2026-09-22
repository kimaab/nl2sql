import os
import json
import time
import logging
from pathlib import Path
from uuid import UUID
from contextlib import asynccontextmanager

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
# 어느 디렉터리에서 서버를 띄우든 backend/.env를 읽는다.
load_dotenv(BASE_DIR / ".env")

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage

import db
from logging_config import configure_logging
from models import (
    Datasource,
    DatasourceInput,
    AskRequest,
    AskResponse,
    MetricInput,
    Metric,
    ApiException,
)
from datasources import (
    list_datasources,
    get_datasource,
    create_datasource,
    update_datasource,
    delete_datasource,
    sync_datasource,
    get_schema,
)
from metrics import list_metrics, create_metric, delete_metric
from contract import load_contract
from pruner import Pruner
from compiler import Compiler
from tool import make_compile_tool
from graph import build_graph

configure_logging()
log = logging.getLogger("nl2sql")
log_request = logging.getLogger("nl2sql.request")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """애플리케이션 생명주기"""
    # 시작
    db.init_pool()

    # schema.sql 실행
    _init_schema()

    yield

    # 종료
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


def _handle_api_exception(exc: ApiException):
    """API 예외 처리"""
    raise HTTPException(status_code=exc.status_code, detail=exc.detail)


# 데이터소스 엔드포인트
@app.get("/api/datasources", response_model=list[Datasource])
def get_datasources():
    try:
        return list_datasources()
    except ApiException as e:
        _handle_api_exception(e)


@app.post("/api/datasources", response_model=Datasource, status_code=status.HTTP_201_CREATED)
def post_datasource(req: DatasourceInput):
    try:
        return create_datasource(req)
    except ApiException as e:
        _handle_api_exception(e)


@app.get("/api/datasources/{datasource_id}", response_model=Datasource)
def get_datasource_detail(datasource_id: UUID):
    try:
        return get_datasource(datasource_id)
    except ApiException as e:
        _handle_api_exception(e)


@app.put("/api/datasources/{datasource_id}", response_model=Datasource)
def put_datasource(datasource_id: UUID, req: DatasourceInput):
    try:
        return update_datasource(datasource_id, req)
    except ApiException as e:
        _handle_api_exception(e)


@app.delete("/api/datasources/{datasource_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_datasource_detail(datasource_id: UUID):
    try:
        delete_datasource(datasource_id)
    except ApiException as e:
        _handle_api_exception(e)


@app.post("/api/datasources/{datasource_id}/sync")
def post_sync(datasource_id: UUID):
    try:
        return sync_datasource(datasource_id)
    except ApiException as e:
        _handle_api_exception(e)


@app.get("/api/datasources/{datasource_id}/schema")
def get_datasource_schema(datasource_id: UUID):
    try:
        # 데이터소스 존재 확인
        get_datasource(datasource_id)
        return get_schema(datasource_id)
    except ApiException as e:
        _handle_api_exception(e)


# 지표 엔드포인트
@app.get("/api/datasources/{datasource_id}/metrics", response_model=list[Metric])
def get_metrics(datasource_id: UUID):
    try:
        # 데이터소스 존재 확인
        get_datasource(datasource_id)
        return list_metrics(datasource_id)
    except ApiException as e:
        _handle_api_exception(e)


@app.post("/api/datasources/{datasource_id}/metrics", response_model=Metric)
def post_metric(datasource_id: UUID, req: MetricInput):
    try:
        # 데이터소스 존재 확인
        get_datasource(datasource_id)
        return create_metric(datasource_id, req)
    except ApiException as e:
        _handle_api_exception(e)


@app.delete("/api/datasources/{datasource_id}/metrics/{metric_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_metric_detail(datasource_id: UUID, metric_id: UUID):
    try:
        delete_metric(datasource_id, metric_id)
    except ApiException as e:
        _handle_api_exception(e)


# 질문 엔드포인트
@app.post("/api/ask", response_model=AskResponse)
def post_ask(req: AskRequest):
    """질문에서 SQL 생성"""
    started = time.monotonic()

    try:
        # 매 요청마다 계약서 새로 로드
        contract = load_contract(req.datasource_id)
        pruned = Pruner(contract).prune(req.question, top_k=3)
        compiler = Compiler(contract)
        compile_tool = make_compile_tool(compiler, req.question)

        # LLM 모델 초기화
        model = ChatOpenAI(
            base_url=os.environ.get("LLM_BASE_URL"),
            api_key=os.environ.get("LLM_API_KEY", "not-needed"),
            model=os.environ.get("LLM_MODEL", "gpt-3.5-turbo"),
            temperature=0.0,
        )
        graph = build_graph(model, compile_tool)

        # 에이전트 루프 실행
        result = graph.invoke(
            {
                "messages": [
                    SystemMessage(_system_prompt(contract, pruned)),
                    HumanMessage(req.question)
                ]
            },
            {"recursion_limit": 11},  # MAX_ATTEMPTS * 2 + 3
        )

        # 결과 추출
        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        last = tool_messages[-1] if tool_messages else None
        attempts = len(tool_messages)
        elapsed_ms = (time.monotonic() - started) * 1000

        if last is not None and str(last.content).startswith("SQL:"):
            sql = str(last.content)[len("SQL:"):].strip()
            log_request.info(
                "datasource=%s question=%r attempts=%d outcome=success elapsed_ms=%.0f",
                contract["name"],
                req.question,
                attempts,
                elapsed_ms,
            )
            return AskResponse(sql=sql, error=None, attempts=attempts)

        detail = str(last.content) if last is not None else "모델이 조회를 시도하지 않았습니다"
        log_request.info(
            "datasource=%s question=%r attempts=%d outcome=fail elapsed_ms=%.0f detail=%s",
            contract["name"],
            req.question,
            attempts,
            elapsed_ms,
            detail,
        )
        return AskResponse(sql=None, error=detail, attempts=attempts)

    except ApiException as e:
        _handle_api_exception(e)


def _system_prompt(contract: dict, pruned: dict) -> str:
    """시스템 프롬프트 생성"""
    return (
        f"당신은 자연어 질문을 '{contract['name']}' 데이터베이스"
        f"({contract['driver']}) 조회로 바꾸는 시맨틱 파서입니다.\n"
        "아래 [허용된 메타데이터]에 없는 테이블·컬럼·지표 이름은 절대 만들어내지 "
        "마십시오.\n\n"
        f"[허용된 메타데이터]\n{json.dumps(pruned, ensure_ascii=False, indent=2)}\n\n"
        "ast는 아래 모양의 JSON 객체입니다. 키 이름을 바꾸지 마십시오 "
        "(column이 아니라 field, table이 아니라 target_table):\n"
        "{\"target_table\": \"테이블명\", \"aggregations\": [{\"field\": \"컬럼명 또는 *\", \"function\": \"COUNT\"}], \"filters\": [{\"field\": \"컬럼명\", \"operator\": \"equals\", \"value\": \"값\"}], \"group_by\": [\"컬럼명\"]}\n"
        "집계 함수는 SUM, COUNT, AVG, MIN, MAX 중 하나이고 괄호 없이 이름만 적습니다 "
        "(\"COUNT(*)\"가 아니라 function은 \"COUNT\", field는 \"*\").\n\n"
        "질문을 분석해 compile_sql 도구를 ast 인자로 부르십시오. 지표를 쓸 때는 "
        "target_table과 aggregations 대신 metric 키에 지표 이름만 적으면 됩니다 "
        "— 그 지표의 집계식과 고정 필터는 다시 적지 않아도 자동으로 적용됩니다.\n"
        "도구가 \"SQL:\"로 시작하는 결과를 돌려주면 그것으로 답이 끝난 것입니다.\n"
        "도구가 \"error:\"로 시작하는 결과를 돌려주면 그 메시지를 읽고 고쳐서 "
        "다시 부르십시오."
    )


@app.get("/health")
def health_check():
    """헬스 체크"""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
