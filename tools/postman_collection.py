"""백엔드 OpenAPI 명세로 Postman 컬렉션(v2.1)을 만든다. 이름과 설명은 아래 표의 한글로 바꾼다.

어느 폴더에서든 실행한다 (backend 는 이 파일 기준 ../backend 에서 찾는다):
    uv run python tools/postman_collection.py [저장할 json 경로]

경로를 비우면 이 파일 옆 DATA_PLATFORM.postman_collection.json 에 저장한다.

API 코드는 건드리지 않는다. 표에 없는 API 가 생기면 '기타' 폴더에 영어 이름으로 들어가고 경고를 찍는다.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "backend"))
from app import app  # noqa: E402

COLLECTION_NAME = "DATA_PLATFORM"

FOLDERS = [
    "1. 시스템",
    "2. 테이블 설정",
    "3. 테이블 관계",
    "4. 지표",
    "5. 지표 자동 등록",
    "6. 지표 검증",
    "7. 질문",
    "8. 질문 기록",
    "9. Superset 연동",
    "기타",
]

# 경로에 들어가는 값. Postman 컬렉션 변수로 두어 한 번 넣으면 모든 요청에 쓰인다.
PATH_VARS = {
    "system_id": "시스템 id. '시스템 목록' 응답의 id",
    "table_id": "테이블 id. '테이블 목록' 응답의 id",
    "relation_id": "관계 id. '관계 목록' 응답의 id",
    "metric_id": "지표 id. '지표 목록' 응답의 id",
    "run_id": "실행 id. 자동 등록, 검증, 설명 자동 채우기를 시작한 응답의 id",
    "entry_id": "질문 기록 id. '질문하기' 응답이나 '질문 기록 목록' 의 id",
}

QUERY_DESC = {
    "limit": "최대 건수",
    "offset": "앞에서 건너뛸 건수 (페이지 넘김)",
    "system_id": "이 시스템 것만 봅니다. 비우면 전체",
    "favorite": "true 면 즐겨찾기한 것만",
    "feedback": "up(맞음) 또는 down(틀림) 피드백을 받은 것만",
}

# "메서드 경로": (폴더, 이름, 설명)
OPS = {
    # 시스템
    "GET /api/systems": (
        "1. 시스템", "시스템 목록",
        "등록된 시스템(대상 DB) 목록입니다. 다른 요청에 쓸 system_id 를 여기서 찾습니다.",
    ),
    "POST /api/systems": (
        "1. 시스템", "시스템 등록",
        "대상 DB 접속 정보를 등록합니다.\n\n"
        "- code: 영문 짧은 이름\n- name: 화면에 보일 이름\n- domain_desc: 업무 설명 (LLM 이 참고)\n"
        "- driver: mysql, postgresql, oracle 중 하나\n- port: 0 이면 드라이버 기본 포트\n"
        "- db_schemas: 읽을 스키마 목록. 첫 스키마가 기본 스키마이고, 나머지 스키마의 테이블은 '스키마.테이블' 이름이 됩니다. "
        "비우면 PostgreSQL 은 public, Oracle 은 접속 계정 (MySQL 은 db_name 하나만 읽습니다)",
    ),
    "GET /api/systems/{system_id}": ("1. 시스템", "시스템 조회", "시스템 하나의 정보입니다."),
    "PUT /api/systems/{system_id}": (
        "1. 시스템", "시스템 수정",
        "시스템 정보를 고칩니다. password 를 비워 보내면 기존 비밀번호를 그대로 둡니다.",
    ),
    "DELETE /api/systems/{system_id}": ("1. 시스템", "시스템 삭제", "시스템을 지웁니다."),
    "POST /api/systems/{system_id}/sync": (
        "1. 시스템", "스키마 동기화",
        "대상 DB 에서 테이블과 컬럼 정보를 읽어 저장된 스키마에 합칩니다. "
        "결과로 추가, 변경, 삭제 건수가 옵니다.",
    ),
    "GET /api/sync-logs": ("1. 시스템", "동기화 기록", "스키마 동기화를 언제 했고 무엇이 바뀌었는지 봅니다."),
    # 테이블 설정
    "GET /api/systems/{system_id}/schema": (
        "2. 테이블 설정", "저장된 스키마",
        "저장된 테이블과 컬럼, 설명입니다. 대상 DB 에서 사라진 것은 빠집니다.",
    ),
    "GET /api/systems/{system_id}/tables": (
        "2. 테이블 설정", "테이블 목록",
        "테이블마다 용도, 컬럼 설명, UTC 보정, 계산 필요 표시 같은 설정을 함께 돌려줍니다. table_id 를 여기서 찾습니다.\n\n"
        "컬럼 설명은 셋입니다. note 는 사람이 적은 설명, comment 는 대상 DB 코멘트, ai_comment 는 LLM 이 추론한 설명이며 "
        "LLM 에는 이 순서로 앞의 것을 넘깁니다.",
    ),
    "PUT /api/systems/{system_id}/tables/{table_id}/purpose": (
        "2. 테이블 설정", "테이블 용도 저장",
        "사람이 적는 테이블 용도입니다. 질문에 맞는 테이블을 고를 때 LLM 이 참고합니다.",
    ),
    "PUT /api/systems/{system_id}/tables/{table_id}/column-note": (
        "2. 테이블 설정", "컬럼 설명 저장",
        "사람이 적는 컬럼 설명입니다. 대상 DB 코멘트와 추론한 설명보다 먼저 쓰이고, 동기화나 자동 설명이 지우지 않습니다. "
        "note 를 빈 문자열로 보내면 원래 설명(DB 코멘트나 추론한 설명)으로 돌아갑니다.",
    ),
    "PUT /api/systems/{system_id}/tables/{table_id}/utc-offset": (
        "2. 테이블 설정", "시각 컬럼 UTC 보정",
        "DB 가 시각을 UTC 로 저장할 때 현지 시차를 적습니다 (한국은 9). "
        "utc_offset_hours 를 0 이나 null 로 보내면 보정을 끕니다.",
    ),
    "PUT /api/systems/{system_id}/tables/{table_id}/needs-calc": (
        "2. 테이블 설정", "계산 필요 컬럼 표시",
        "누적값처럼 그대로 더하면 틀리는 컬럼에 표시합니다. 질문 처리와 지표 자동 등록이 이 표시를 참고합니다.",
    ),
    "POST /api/systems/{system_id}/describe/runs": (
        "2. 테이블 설정", "설명 자동 채우기 시작",
        "설명이 빈 테이블과 컬럼을 LLM 이 이름, 타입, 샘플 값 몇 행을 보고 채우고(ai_comment), "
        "컬럼 이름, 타입, PK 로 관계도 추론합니다(source 가 auto 인 관계). 뒤에서 돌고 바로 실행 id 를 돌려줍니다.\n\n"
        "- table_ids: 비우면 설명이 빈 테이블 모두\n- descriptions: 빈 설명을 채웁니다\n"
        "- overwrite: 전에 추론한 설명도 다시 채웁니다. DB 코멘트와 사람이 적은 설명은 언제나 그대로 둡니다\n"
        "- relations: 관계를 추론합니다",
    ),
    "GET /api/systems/{system_id}/describe/runs": ("2. 테이블 설정", "설명 자동 채우기 기록", "설명 자동 채우기를 돌린 기록입니다."),
    "GET /api/systems/{system_id}/describe/runs/{run_id}": (
        "2. 테이블 설정", "설명 자동 채우기 결과",
        "진행 상황과 채운 건수, 실패한 테이블입니다. status 가 running 이면 아직 도는 중이니 다시 불러 보십시오.",
    ),
    # 관계
    "GET /api/systems/{system_id}/relations": ("3. 테이블 관계", "관계 목록", "테이블 사이 관계(JOIN 조건) 목록입니다."),
    "POST /api/systems/{system_id}/relations": (
        "3. 테이블 관계", "관계 추가",
        "DB 에 FK 가 없는 관계를 직접 넣습니다. left 가 참조하는 쪽, right 가 참조되는 쪽입니다.",
    ),
    "DELETE /api/systems/{system_id}/relations/{relation_id}": ("3. 테이블 관계", "관계 삭제", "관계를 지웁니다."),
    # 지표
    "GET /api/systems/{system_id}/metrics": ("4. 지표", "지표 목록", "등록된 지표 목록입니다. metric_id 를 여기서 찾습니다."),
    "POST /api/systems/{system_id}/metrics": (
        "4. 지표", "지표 등록",
        "지표를 SQL 로 등록합니다. SQL 을 구조(AST)로 바꾸고, 다시 SQL 로 만들어 "
        "대상 DB 에서 결과가 같은지 확인한 뒤 저장합니다.",
    ),
    "PUT /api/systems/{system_id}/metrics/{metric_id}": (
        "4. 지표", "지표 수정", "지표를 고칩니다. 등록과 같은 확인을 거칩니다. 이전 정의는 이력에 남습니다.",
    ),
    "DELETE /api/systems/{system_id}/metrics/{metric_id}": (
        "4. 지표", "지표 삭제", "지표를 지웁니다. 정의는 이력에 남습니다.",
    ),
    "POST /api/systems/{system_id}/metrics/check": (
        "4. 지표", "지표 SQL 미리 확인",
        "저장하지 않고 등록 절차만 밟아 봅니다. 실패하면 stage 에 어느 단계에서 막혔는지 나옵니다 "
        "(parse 변환, compile 컴파일, guard 실행 전 확인, verify 결과 비교).",
    ),
    "POST /api/systems/{system_id}/metrics/draft": (
        "4. 지표", "지표 초안 만들기",
        "지표가 없어 멈춘 질문에서 지표 SQL 초안을 LLM 이 씁니다. 저장하지 않습니다. "
        "missing 에는 '질문하기' 응답의 missing 하나를 그대로 넣습니다.",
    ),
    "GET /api/systems/{system_id}/metrics/{metric_id}/history": (
        "4. 지표", "지표 변경 이력", "지표가 언제 어떻게 바뀌었는지 봅니다. 지운 지표도 이력은 남습니다.",
    ),
    "POST /api/systems/{system_id}/compile": (
        "4. 지표", "AST 를 SQL 로 바꾸기", "LLM 없이 조회 구조(AST)를 SQL 로 바꿔 봅니다. 실행하지 않습니다.",
    ),
    # 지표 자동 등록
    "POST /api/systems/{system_id}/metric-auto/runs": (
        "5. 지표 자동 등록", "자동 등록 시작",
        "LLM 이 테이블마다 지표 후보를 만들고, 확인을 통과한 것만 등록합니다. 뒤에서 돌고 바로 실행 id 를 돌려줍니다.\n\n"
        "- tables: 비우면 지표가 없는 테이블 중 관계가 많은 것부터 고릅니다\n"
        "- max_tables: 고를 테이블 수 (1~50)\n- per_table: 테이블마다 만들 후보 수 (1~10)\n"
        "- allow_full_scan: 테이블 전체를 훑는 확인도 돌립니다. 테스트 DB 에서만 켜십시오",
    ),
    "GET /api/systems/{system_id}/metric-auto/runs": ("5. 지표 자동 등록", "자동 등록 기록", "자동 등록을 돌린 기록입니다."),
    "GET /api/systems/{system_id}/metric-auto/runs/{run_id}": (
        "5. 지표 자동 등록", "자동 등록 결과",
        "진행 상황과 후보별 결과입니다. status 가 running 이면 아직 도는 중이니 다시 불러 보십시오.",
    ),
    # 지표 검증
    "POST /api/systems/{system_id}/metric-validations": (
        "6. 지표 검증", "검증 시작",
        "고른 지표를 검증합니다. 뒤에서 돌고 바로 실행 id 를 돌려줍니다.\n\n"
        "- metric_ids: 검증할 지표 id. 비우면 400 오류\n- run_sql: 대상 DB 에서 실행 계획과 결과를 봅니다\n"
        "- use_llm: SQL 이 이름, 설명과 맞는지 LLM 이 봅니다\n"
        "- allow_full_scan: 테이블 전체를 훑는 지표도 결과를 봅니다. 테스트 DB 에서만 켜십시오",
    ),
    "GET /api/systems/{system_id}/metric-validations": ("6. 지표 검증", "검증 기록", "검증을 돌린 기록입니다."),
    "GET /api/systems/{system_id}/metric-validations/{run_id}": (
        "6. 지표 검증", "검증 결과",
        "지표마다 통과, 주의, 실패와 그 이유입니다. status 가 running 이면 다시 불러 보십시오.",
    ),
    "GET /api/systems/{system_id}/metrics/{metric_id}/validations": (
        "6. 지표 검증", "지표별 검증 이력", "지표 하나가 지금까지 받은 검증 결과입니다.",
    ),
    # 질문
    "POST /api/ask": (
        "7. 질문", "질문하기",
        "자연어 질문을 SQL 로 바꿉니다. 테이블 고르기, 지표 판단, SQL 만들기 순서로 돌고 결과는 질문 기록에 남습니다. "
        "SQL 은 실행하지 않습니다. 지표가 없으면 needs_metric 과 missing 이 옵니다.\n\n"
        "- user_id: 질문한 사용자의 로그인 아이디 (선택). 질문의 '나', '내 권한' 이 이 사용자가 됩니다",
    ),
    # 질문 기록
    "GET /api/history": ("8. 질문 기록", "질문 기록 목록", "지금까지 한 질문과 만든 SQL 입니다."),
    "GET /api/history/export": (
        "8. 질문 기록", "평가셋 내보내기", "'맞음' 피드백을 받은 기록을 평가셋 파일 형식으로 내보냅니다.",
    ),
    "GET /api/history/{entry_id}/trace": (
        "8. 질문 기록", "질문 처리 단계", "질문을 처리하며 어떤 테이블과 지표를 왜 골랐는지 단계별로 봅니다.",
    ),
    "PATCH /api/history/{entry_id}": (
        "8. 질문 기록", "즐겨찾기, 피드백 저장",
        "바꿀 것만 보냅니다. feedback 은 up(맞음) 또는 down(틀림)이고, 지우려면 빈 문자열을 보냅니다.",
    ),
    "DELETE /api/history/{entry_id}": ("8. 질문 기록", "질문 기록 삭제", "질문 기록을 지웁니다."),
    # Superset
    "POST /api/systems/{system_id}/superset": (
        "9. Superset 연동", "Superset 에 DB 연결 등록",
        "시스템을 Superset 의 DB 연결로 등록합니다. 이미 있으면 접속 정보를 고칩니다. 데이터는 옮기지 않습니다.",
    ),
    "POST /api/history/{entry_id}/superset/sqllab": (
        "9. Superset 연동", "SQL Lab 링크 만들기", "질문에서 만든 SQL 을 Superset SQL Lab 에 열어 두는 링크입니다.",
    ),
    "POST /api/history/{entry_id}/superset/chart": (
        "9. Superset 연동", "Superset 차트 만들기",
        "질문에서 만든 SQL 로 차트를 만듭니다. 같은 SQL 이면 전에 만든 차트를 엽니다.",
    ),
    # 기타
    "GET /health": ("기타", "서버 상태 확인", "서버가 떠 있으면 {\"status\": \"ok\"} 를 돌려줍니다."),
}

# 명세의 기본값만으로는 뜻이 안 보이는 본문은 예시를 직접 둔다
BODY_EXAMPLES = {
    "POST /api/ask": {"system_id": "{{system_id}}", "question": "지난주 일자별 주문 건수", "user_id": ""},
    "POST /api/systems": {
        "code": "shop",
        "name": "쇼핑몰",
        "domain_desc": "주문과 회원을 다루는 쇼핑몰 DB",
        "driver": "postgresql",
        "host": "localhost",
        "port": 0,
        "db_name": "shop",
        "db_schemas": ["public"],
        "username": "",
        "password": "",
    },
    "POST /api/systems/{system_id}/describe/runs": {
        "table_ids": [],
        "descriptions": True,
        "overwrite": False,
        "relations": True,
    },
    "PUT /api/systems/{system_id}/tables/{table_id}/column-note": {"column": "user_id", "note": "로그인 아이디"},
    "POST /api/systems/{system_id}/metric-validations": {
        "metric_ids": ["{{metric_id}}"],
        "run_sql": True,
        "use_llm": True,
        "allow_full_scan": False,
    },
    "POST /api/systems/{system_id}/metrics": {
        "name": "주문 건수",
        "description": "취소를 뺀 주문 수",
        "synonyms": ["주문수"],
        "sql": "SELECT COUNT(*) AS order_count FROM orders WHERE status <> 'canceled'",
    },
    "POST /api/systems/{system_id}/metrics/check": {
        "sql": "SELECT COUNT(*) AS order_count FROM orders WHERE status <> 'canceled'",
    },
    "PUT /api/systems/{system_id}/tables/{table_id}/utc-offset": {"column": "created_at", "utc_offset_hours": 9},
    "PUT /api/systems/{system_id}/tables/{table_id}/needs-calc": {"column": "odometer", "needs_calc": True},
    "PATCH /api/history/{entry_id}": {"favorite": True, "feedback": "up", "feedback_note": ""},
}
BODY_EXAMPLES["PUT /api/systems/{system_id}/metrics/{metric_id}"] = BODY_EXAMPLES["POST /api/systems/{system_id}/metrics"]
BODY_EXAMPLES["PUT /api/systems/{system_id}"] = BODY_EXAMPLES["POST /api/systems"]


def resolve(spec, schema):
    while "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        schema = spec["components"]["schemas"][name]
    return schema


def example(spec, schema):
    """명세 스키마에서 본문 예시를 만든다. 기본값이 있으면 기본값, 없으면 타입에 맞는 빈 값"""
    schema = resolve(spec, schema)
    if "default" in schema:
        return schema["default"]
    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            if option.get("type") != "null":
                return example(spec, option)
        return None
    kind = schema.get("type")
    if kind == "object":
        result = {}
        for key, prop in schema.get("properties", {}).items():
            result[key] = example(spec, prop)
        return result
    if kind == "array":
        return []
    if kind == "integer":
        return 0
    if kind == "number":
        return 0
    if kind == "boolean":
        return False
    if kind == "string":
        if schema.get("format") == "uuid":
            return "00000000-0000-0000-0000-000000000000"
        return ""
    return {}


def request_item(spec, method, path, op):
    key = method.upper() + " " + path
    folder, name, desc = OPS.get(key, ("기타", op.get("summary", key), ""))
    if key not in OPS:
        print("경고: 표에 없는 API ->", key)

    segments = []
    for part in path.strip("/").split("/"):
        if part.startswith("{") and part.endswith("}"):
            part = "{{" + part[1:-1] + "}}"
        segments.append(part)

    query = []
    for param in op.get("parameters", []):
        if param["in"] != "query":
            continue
        schema = resolve(spec, param.get("schema", {}))
        value = schema.get("default")
        query.append({
            "key": param["name"],
            "value": "" if value is None else str(value).lower() if isinstance(value, bool) else str(value),
            "description": QUERY_DESC.get(param["name"], ""),
            "disabled": value is None and not param.get("required", False),
        })
        if param["name"] == "system_id" and param.get("required"):
            query[-1]["value"] = "{{system_id}}"

    raw = "{{baseUrl}}/" + "/".join(segments)
    if query:
        pairs = []
        for q in query:
            if not q["disabled"]:
                pairs.append(q["key"] + "=" + q["value"])
        if pairs:
            raw = raw + "?" + "&".join(pairs)

    request = {
        "method": method.upper(),
        "header": [],
        "url": {"raw": raw, "host": ["{{baseUrl}}"], "path": segments},
        "description": desc,
    }
    if query:
        request["url"]["query"] = query

    body_schema = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
    if body_schema is not None:
        body = BODY_EXAMPLES.get(key)
        if body is None:
            body = example(spec, body_schema)
        request["header"].append({"key": "Content-Type", "value": "application/json"})
        request["body"] = {
            "mode": "raw",
            "raw": json.dumps(body, ensure_ascii=False, indent=2),
            "options": {"raw": {"language": "json"}},
        }
    return folder, {"name": name, "request": request, "response": []}


def build():
    spec = app.openapi()
    folders = {}
    for name in FOLDERS:
        folders[name] = []

    # 표의 순서대로 넣는다 (명세 순서는 라우터 등록 순서라 뒤섞여 있다)
    order = list(OPS.keys())
    ops = []
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            key = method.upper() + " " + path
            rank = order.index(key) if key in OPS else len(order)
            ops.append((rank, method, path, op))
    ops.sort(key=lambda x: x[0])

    for rank, method, path, op in ops:
        folder, item = request_item(spec, method, path, op)
        folders[folder].append(item)

    items = []
    for name in FOLDERS:
        if folders[name]:
            items.append({"name": name, "item": folders[name]})

    variables = [{"key": "baseUrl", "value": "http://localhost:8000", "description": "백엔드 주소"}]
    for key, desc in PATH_VARS.items():
        variables.append({"key": key, "value": "", "description": desc})

    return {
        "info": {
            "name": COLLECTION_NAME,
            "description": "데이터 플랫폼 백엔드 API. 먼저 컬렉션 Variables 에 baseUrl 과 system_id 를 넣으십시오.",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": items,
        "variable": variables,
    }


if __name__ == "__main__":
    collection = build()
    out = sys.argv[1] if len(sys.argv) > 1 else str(HERE / (COLLECTION_NAME + ".postman_collection.json"))
    with open(out, "w", encoding="utf-8") as f:
        json.dump(collection, f, ensure_ascii=False, indent=2)
    count = 0
    for folder in collection["item"]:
        print(folder["name"], len(folder["item"]))
        count += len(folder["item"])
    print("요청", count, "개 ->", out)
