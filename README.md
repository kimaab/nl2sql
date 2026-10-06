# nl2sql Studio

자연어 질문을 SQL 쿼리로 변환하는 웹 애플리케이션입니다. SQL 은 만들어서 보여주기만 하고 대상 DB 에 실행하지 않습니다.

## 기능

- **여러 데이터베이스 지원**: MySQL, PostgreSQL, Oracle
- **스키마 자동 동기화**: 대상 DB에서 테이블·컬럼·FK 관계를 읽어 메타데이터 DB에 저장. `AUTO_SYNC_MINUTES` 로 주기 실행, 실행마다 기록과 깨진 지표 알림
- **자연어 → SQL**: LLM이 조회 명세(JSON AST)를 만들고 컴파일러가 검증·조립
- **조회 표현**: 집계·목록 조회, 정렬·개수 제한, 날짜 기간 단위 묶음(일/주/월/분기/년), NULL·부분 일치·OR 조건, HAVING, DISTINCT·COUNT_DISTINCT, 기간 비교(증감률)
- **조인**: 지표 정의의 조인, 등록된 관계(FK·수동)를 따라가는 질의 조인 — ON 조건은 모델이 아니라 관계 정의에서 옴
- **업무 지표**: 집계형·조회형·파생형(`[매출] - [환불]`), 고정 필터 강제, 예시 질문(few-shot), 수정·버전 이력
- **용어·코드 사전**: 컬럼 동의어와 코드값 뜻(`03`=배송완료). 질문의 업무 용어를 코드로 바꾸고 사전에 없는 값은 거부
- **되묻기**: 질문만으로 조회를 정할 수 없으면 모델이 되묻고, 사용자는 질문을 완성해 다시 보냄 (서버는 이전 질문을 기억하지 않음)
- **질문 기록·평가**: 질문 이력·즐겨찾기·맞음/틀림 피드백, '맞음' 기록을 평가셋으로 내보내 회귀 평가

## 설치

### 요구사항
- Python 3.12+ ([uv](https://docs.astral.sh/uv/) 권장)
- Node.js 18+
- PostgreSQL (메타데이터 DB로 사용)

### 백엔드 설정

```bash
# 의존성 설치 (개발 도구 pytest 포함)
uv sync

# 환경변수 설정
cp .env.example backend/.env
# backend/.env 수정: DB_URL, LLM 정보 등

# 백엔드 실행
cd backend
uv run python -m uvicorn app:app --reload
```

### 프론트엔드 설정

```bash
cd frontend
npm install
npm run dev
```

## 구조

```
nl2sql/
├── backend/
│   ├── app.py              # FastAPI 애플리케이션 (엔드포인트)
│   ├── schema.sql          # 메타데이터 DB 스키마 (여러 번 실행해도 안전한 이행 포함)
│   ├── models.py           # Pydantic 모델
│   ├── db.py               # DB 커넥션 풀
│   ├── catalog.py          # 드라이버별 카탈로그·FK 읽기
│   ├── datasources.py      # 데이터소스 CRUD + 동기화 + 동기화 기록
│   ├── scheduler.py        # 자동 동기화 (AUTO_SYNC_MINUTES)
│   ├── metrics.py          # 업무 지표 CRUD + 검증 + 버전 이력
│   ├── relations.py        # 테이블 관계 (FK·수동)
│   ├── annotations.py      # 용어·코드 사전
│   ├── contract.py         # 계약서 로드, 깨진 지표 판정
│   ├── pruner.py           # TF-IDF 프루닝
│   ├── compiler.py         # AST 정규화·검증 + SQL 조립
│   ├── tool.py             # compile_sql · ask_user 도구
│   ├── graph.py            # LangGraph 에이전트 루프
│   ├── ask.py              # 질문 하나 처리 (프롬프트 + 루프), 앱과 평가가 함께 씀
│   ├── history.py          # 질문 기록·피드백·평가셋 내보내기
│   ├── evaluate.py         # 평가셋 실행기
│   └── logging_config.py   # 로깅 설정
├── eval/                   # 샘플 계약서와 평가셋 (reports/ 는 실행 결과, 커밋 안 함)
├── tests/                  # pytest (통합 테스트는 TEST_DB_URL 이 있을 때만)
└── frontend/src/
    ├── App.tsx             # 라우팅
    ├── api.ts              # API 클라이언트
    ├── lib/metrics.ts      # 지표 정의 표시·깨진 지표 판정 (백엔드와 같은 규칙)
    └── pages/              # 질문, 질문 기록, 데이터소스·동기화·관계·사전, 지표
```

## 환경변수

| 변수 | 설명 |
|------|------|
| `DB_URL` | 메타데이터 DB 연결 문자열 (PostgreSQL) |
| `LLM_BASE_URL` | LLM API 엔드포인트 (OpenAI 호환) |
| `LLM_API_KEY` | LLM API 키 |
| `LLM_MODEL` | 모델 이름 (예: gpt-3.5-turbo) |
| `AUTO_SYNC_MINUTES` | 자동 동기화 주기(분). 0 또는 비우면 끔 (기본) |

## API 엔드포인트

### 데이터소스·동기화
- `GET /api/datasources` - 목록
- `POST /api/datasources` - 등록
- `GET /api/datasources/{id}` - 조회
- `PUT /api/datasources/{id}` - 수정
- `DELETE /api/datasources/{id}` - 삭제
- `POST /api/datasources/{id}/sync` - 스키마·FK 동기화 (결과에 깨진 지표 목록 포함)
- `GET /api/sync-logs?datasource_id=` - 동기화 실행 기록
- `GET /api/datasources/{id}/schema` - 저장된 스키마 조회

### 관계·사전
- `GET|POST /api/datasources/{id}/relations`, `DELETE /api/datasources/{id}/relations/{relation_id}` - 관계 (FK 관계는 지울 수 없음)
- `GET /api/datasources/{id}/annotations` - 용어·코드 사전 목록
- `PUT /api/datasources/{id}/annotations/{table}/{column}` - 컬럼 사전 저장 (둘 다 비우면 삭제)

### 지표
- `GET /api/datasources/{id}/metrics` - 지표 목록
- `POST /api/datasources/{id}/metrics` - 지표 등록
- `PUT /api/datasources/{id}/metrics/{metric_id}` - 지표 수정 (버전 증가)
- `GET /api/datasources/{id}/metrics/{metric_id}/history` - 변경 이력
- `DELETE /api/datasources/{id}/metrics/{metric_id}` - 지표 삭제 (파생 지표가 쓰는 지표는 거부)
- `POST /api/datasources/{id}/compile` - 모델 없이 AST 를 SQL 로 컴파일

### 질문·기록
- `POST /api/ask` - 질문에서 SQL 생성 (`clarification` 이 있으면 모델이 되물은 것)
- `GET /api/history?datasource_id=&favorite=&feedback=` - 질문 기록
- `PATCH /api/history/{id}` - 즐겨찾기·피드백(`up`/`down`)·메모
- `DELETE /api/history/{id}` - 기록 삭제
- `GET /api/history/export?datasource_id=` - '맞음' 기록을 평가셋 형식으로

## 테스트와 평가

```bash
# 단위 테스트 (컴파일러, 프루닝, 에이전트 흐름, 평가셋 일관성)
uv run pytest

# API 통합 테스트 — 비어 있는 PostgreSQL 을 하나 준비해서
TEST_DB_URL=postgresql://postgres:pw@localhost:55432/postgres uv run pytest

# 평가셋: 모델 없이 AST → 기대 SQL 확인
uv run python backend/evaluate.py --cases eval/shoppingmall_cases.json --contract eval/shoppingmall_contract.json --compile-only

# 평가셋: 실제 LLM 으로 정확도 측정 (리포트는 eval/reports/)
uv run python backend/evaluate.py --cases eval/shoppingmall_cases.json --contract eval/shoppingmall_contract.json

# 질문 기록에서 내보낸 평가셋을 실제 데이터소스 계약서로
uv run python backend/evaluate.py --cases eval-내보낸파일.json --datasource-id <uuid>
```

평가는 기대 SQL 과의 문자열 일치(`accuracy`)와 별칭 이름 차이를 무시한 일치(`accuracy_alias_insensitive`)를 함께 냅니다.
조인 순서, `IN`/`OR`, 조회 컬럼 수처럼 의미가 같은 변형은 둘 다 오답으로 셉니다 — 리포트의 실패 목록을 사람이 확인하십시오.

## 주요 설계 원칙

1. **SQL 문자열은 모델이 생성하지 않음** - JSON AST만 생성하고 컴파일러가 검증 후 SQL 조립. 조인의 ON 조건도 관계 정의에서 옴
2. **등록과 동기화는 분리** - 실패의 성격이 다르므로 별도 엔드포인트
3. **동기화는 통째로 교체** - 병합하지 않음 (테이블·FK 삭제를 반영하기 위해). 사람이 등록한 관계·사전은 이름으로 들고 있어 남음
4. **요청 간 격리** - 다음 질문은 이전 질문을 모름. 되묻기도 사용자가 질문을 완성해 새로 보내는 방식
5. **지표의 고정 필터는 컴파일러가 강제** - 프롬프트만으로 신뢰하지 않음. 파생 지표는 구성 지표의 필터를 CASE WHEN 으로 품음
6. **오류 메시지가 다음 시도의 프롬프트** - 무엇이 가능한지(사용 가능한 테이블, 연산자, 코드값, 필요한 joins)를 함께 실음

## 라이선스

MIT
