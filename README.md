# nl2sql Studio

자연어 질문을 SQL 쿼리로 변환하는 웹 애플리케이션입니다. SQL 은 만들어서 보여주기만 하고 대상 DB 에 실행하지 않습니다.

## 질문 처리 흐름

지표 전체를 한 번에 검색(RAG 식)하지 않고, 범위를 좁히면서 단계마다 LLM 이 판단합니다.

```
(사용자가 시스템 선택)
 ① 테이블 추론 (LLM)   업무 영역 설명 + 테이블 한 줄 요약 목록 → 3~5개와 이유
 ② 지표 후보 조회      고른 테이블에 연결된 지표 (metric_table — 검색이 아니라 조회)
 ③ 지표 선택 (LLM)     지표 이름·설명·예시 질문만 보고 1~2개 또는 '없음' (애매하면 되묻기)
 ④ 조회 명세 (LLM)     고른 지표의 정의 + 필요한 테이블 → JSON AST
 ⑤ 컴파일 · 검증       화이트리스트 · 지표 전개 · 날짜 → SQL (실패하면 오류 메시지로 재시도, 최대 4회)
```

단계마다 후보·선택·이유·토큰이 `ask_trace` 에 남아, 틀렸을 때 어느 단계에서 틀렸는지 보입니다.
설계 문서는 `docs/to-be/` (플로우차트 · 프로세스 플로우 · 테이블 정의서).

## 기능

- **여러 데이터베이스 지원**: MySQL, PostgreSQL, Oracle
- **스키마 동기화**: 대상 DB 의 테이블·컬럼·PK·FK 를 읽어 **이름 기준 병합** — 테이블 id 가 유지되어 지표 연결이 끊기지 않고, 사라진 테이블·컬럼은 지우지 않고 표시(soft delete). `AUTO_SYNC_MINUTES` 로 주기 실행
- **메타데이터 보강**: LLM 이 테이블 카드(무엇을 기록하나 · 주요 컬럼 · 한 줄 요약)와 지표 예시 질문 초안을 만들고, 사람이 검수 화면에서 승인한 것만 질문 처리에 씀
- **업무 지표**: 집계형·조회형·파생형(`[매출] - [환불]`), 시계열(`DELTA_SUM`·`CHANGE_COUNT` → `LAG` 서브쿼리), 고정 필터 강제, 동의어, 버전 이력. 승인된 예시 질문이 있어야 `active` 가 되어 질문에 쓰임
- **사전**: 컬럼 동의어·코드값(`03`=배송완료), 업무 용어(`운행시간 = 가동시간 → bms_log.eg_time`) — 질문에 나온 용어만 프롬프트에 실음
- **조회 표현**: 집계·목록, 정렬·개수 제한, 기간 단위 묶음, NULL·부분 일치·OR, HAVING, DISTINCT, 기간 비교(증감률), 관계를 따라가는 조인
- **되묻기**: 테이블·지표를 정할 수 없으면 후보를 보여주고 되묻기 (서버는 이전 질문을 기억하지 않음)
- **질문 기록·평가**: 이력·즐겨찾기·맞음/틀림, 단계별 선택 기록, '맞음' 기록을 평가 문항으로. 평가는 단계별(테이블 재현율 · 지표 정확도 · SQL 일치)로 재고 방식끼리(LLM 선택 / TF-IDF) 비교

## 설치

국토교통부 아파트 매매 실거래가를 PostgreSQL `budongsan` 스키마에 수집하는 방법과 매일 06:00 예약 설정은 [수집 안내](docs/budongsan.md)를 참고하세요.

### 요구사항
- Python 3.12+ ([uv](https://docs.astral.sh/uv/) 권장)
- Node.js 18+
- PostgreSQL (메타데이터 DB)
- OpenAI 호환 LLM 엔드포인트 (개발 환경: gemma-4-31B-it on vLLM, 컨텍스트 16K)

### 백엔드 설정

```bash
uv sync                           # 의존성 (개발 도구 pytest 포함)
cp .env.example backend/.env      # DB_URL, LLM 정보, NL2SQL_SECRET_KEY 채우기
# 암호화 키 만들기
uv run python -c "import base64, os; print(base64.b64encode(os.urandom(32)).decode())"

cd backend
uv run python -m uvicorn app:app --reload
```

### AS-IS 에서 옮기기

AS-IS 메타데이터(`datasource_*`)가 있는 DB 라면 한 번 옮깁니다. AS-IS 표는 지우지 않습니다.

```bash
uv run python backend/migrate_v2.py           # 무엇을 옮길지 보기
uv run python backend/migrate_v2.py --apply   # 옮기기 (비밀번호는 암호화, 지표 연결·상태 재계산, 보강 대기열 등록)
```

`schema.sql` 은 AS-IS `ask_log` 를 `ask_log_v1` 로 이름을 바꿔 두고, 마이그레이션이 새 `ask_log` 로 옮깁니다.
옮긴 뒤에는 보강·검수 화면에서 카드·예시 질문을 만들고 승인해야 지표가 질문에 쓰입니다.

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
│   ├── app.py              # FastAPI 엔드포인트
│   ├── schema.sql          # 메타데이터 DB 스키마 (여러 번 실행해도 안전)
│   ├── migrate_v2.py       # AS-IS → TO-BE 데이터 이전
│   ├── models.py           # Pydantic 모델
│   ├── db.py · secret.py   # DB 커넥션 풀 · 비밀번호 암호화 (AES-256-GCM)
│   ├── catalog.py          # 드라이버별 카탈로그·PK·FK 읽기
│   ├── systems.py          # 시스템 CRUD + 이름 기준 병합 동기화 + 테이블 용도
│   ├── scheduler.py        # 자동 동기화 (AUTO_SYNC_MINUTES)
│   ├── metrics.py          # 지표 CRUD · 검증 · 테이블 연결 · 상태 · 이력
│   ├── relations.py        # 테이블 관계 (FK·수동)
│   ├── annotations.py      # 컬럼 사전 (동의어·코드값)
│   ├── glossary.py         # 업무 용어 사전
│   ├── contract.py         # 계약서 로드, 깨진 지표 판정
│   ├── selector.py         # 테이블 추론 → 지표 후보 → 지표 선택 (TF-IDF 기준선 포함)
│   ├── pruner.py           # TF-IDF 검색 (비교 기준)
│   ├── compiler.py         # AST 정규화·검증 + SQL 조립
│   ├── tool.py · graph.py  # compile_sql · ask_user 도구, LangGraph 루프
│   ├── ask.py              # 질문 하나 처리, 앱과 평가가 함께 씀
│   ├── enrich.py           # 보강 대기열 · LLM 초안 · 검수
│   ├── history.py          # 질문 기록 · 단계 기록 · 피드백 · 평가 문항
│   ├── evaluate.py         # 평가 실행기
│   └── llm.py · logging_config.py
├── docs/to-be/             # TO-BE 설계 문서
├── eval/                   # 샘플 계약서와 평가셋 (reports/ 는 실행 결과, 커밋 안 함)
├── tests/                  # pytest (통합 테스트는 TEST_DB_URL 이 있을 때만)
└── frontend/src/
    ├── api.ts              # API 클라이언트
    └── pages/              # 질문 · 기록 · 시스템(동기화·테이블·관계·사전·용어) · 지표 · 보강·검수
```

## 환경변수

| 변수 | 설명 |
|------|------|
| `DB_URL` | 메타데이터 DB 연결 문자열 (PostgreSQL) |
| `NL2SQL_SECRET_KEY` | 대상 DB 비밀번호 암호화 키 (32바이트 base64). 없으면 기동하지 않음. 잃으면 비밀번호를 다시 입력해야 함 |
| `LLM_BASE_URL` | LLM API 엔드포인트 (OpenAI 호환) |
| `LLM_API_KEY` | LLM API 키 |
| `LLM_MODEL` | 모델 이름 |
| `AUTO_SYNC_MINUTES` | 자동 동기화 주기(분). 0 또는 비우면 끔 (기본) |

## API 엔드포인트

### 시스템 · 동기화 · 테이블
- `GET|POST /api/systems`, `GET|PUT|DELETE /api/systems/{id}` - 시스템 (비밀번호는 응답에 없음, 수정 때 비우면 유지)
- `POST /api/systems/{id}/sync` - 동기화 (추가·변경·삭제 건수, 깨진 지표)
- `GET /api/sync-logs?system_id=` - 동기화 기록
- `GET /api/systems/{id}/schema` - 저장된 스키마
- `GET /api/systems/{id}/tables`, `PUT /api/systems/{id}/tables/{table_id}/purpose` - 테이블 용도·카드 상태

### 관계 · 사전 · 용어
- `GET|POST /api/systems/{id}/relations`, `DELETE .../relations/{relation_id}` - 관계 (FK 관계는 지울 수 없음)
- `GET /api/systems/{id}/annotations`, `PUT .../annotations/{table}/{column}` - 컬럼 동의어·코드값
- `GET|POST /api/systems/{id}/glossary`, `PUT|DELETE .../glossary/{term_id}` - 업무 용어 (전사 공통은 `/api/glossary`)

### 지표
- `GET|POST /api/systems/{id}/metrics`, `PUT|DELETE .../metrics/{metric_id}` - 지표 (파생 지표가 쓰는 지표는 삭제 거부)
- `GET .../metrics/{metric_id}/history` - 변경 이력
- `POST /api/systems/{id}/compile` - 모델 없이 AST 를 SQL 로

### 보강 · 검수 · 평가
- `GET /api/systems/{id}/enrich` - 대기열 상태, `POST` - 처리 시작 (`{"enqueue_all": true}` 면 카드·예시 없는 것 모두)
- `GET /api/systems/{id}/review` - 검수 대기 초안, `POST .../review/{kind}/{item_id}` - 승인·반려 (`table_card` · `metric_example` · `eval_case`)
- `GET /api/systems/{id}/eval-runs` - 평가 실행 기록

### 질문 · 기록
- `POST /api/ask` - `{system_id, question}` → SQL, 고른 테이블·지표, 단계별 기록
- `GET /api/history?system_id=&favorite=&feedback=`, `PATCH|DELETE /api/history/{id}`
- `GET /api/history/{id}/trace` - 단계별 선택 기록
- `POST /api/history/{id}/eval-case` - '맞음' 기록을 평가 문항으로
- `GET /api/history/export?system_id=` - '맞음' 기록을 평가셋 파일 형식으로

## 테스트와 평가

```bash
uv run pytest                                          # 단위 테스트
TEST_DB_URL=postgresql://user:pw@host:5432/nl2sql_itest uv run pytest   # + API 통합 테스트 (전용 DB)

# 평가 문항 초안 만들기 (활성 지표마다 N개) → 보강·검수 화면의 '평가 문항' 에서 승인
uv run python backend/evaluate.py --system-id <uuid> --generate 3

# 승인된 평가 문항으로 실행 → eval_run · eval_result 에 남고 보강·검수 화면에 표시
uv run python backend/evaluate.py --system-id <uuid>                    # TO-BE (LLM 선택)
uv run python backend/evaluate.py --system-id <uuid> --selector tfidf   # 비교 기준 (한 번에 검색)

# 샘플 계약서·평가셋 (DB 없이)
uv run python backend/evaluate.py --cases eval/shoppingmall_cases.json --contract eval/shoppingmall_contract.json --compile-only
```

평가는 단계별로 냅니다: 테이블 재현율(정답 테이블이 후보에 들었나), 지표 정확도('맞는 지표 없음' 이 정답이면 안 고른 게 맞음),
SQL 일치(별칭 이름 차이 무시). 평가 문항은 지표 예시 질문과 따로 만듭니다 — 예시 질문으로 평가하면 점수가 부풀려집니다.

## 주요 설계 원칙

1. **넓게 고르고 점점 좁힌다** - 시스템 → 테이블 → 지표. 앞 단계는 놓치지 않기(재현율), 뒤 단계는 정확히
2. **구조로 알 수 있는 것은 LLM 에 묻지 않는다** - 테이블 → 지표는 `metric_table` 조회. 지표를 저장하면 정의에서 자동으로 만든다
3. **판단할 때 필요한 것만 보낸다** - 고를 때는 요약(이름·설명·예시 질문), SQL 을 만들 때만 상세
4. **초안은 LLM, 책임은 사람** - 카드·예시 질문·평가 문항은 승인한 것만 쓴다
5. **SQL 문자열은 모델이 생성하지 않음** - JSON AST 만 만들고 컴파일러가 검증 후 조립. 조인 ON 조건도 관계 정의에서 옴
6. **고른 지표는 지킨다** - 지표를 고른 뒤 원본 컬럼을 직접 집계하면 오류 없이 틀린 답이 나온다. 첫 시도가 지표를 쓰지 않으면 되돌려 보낸다
7. **동기화는 병합** - id 를 유지하고 사라진 것은 표시만. 지표 연결과 이력이 끊기지 않는다
8. **오류 메시지가 다음 시도의 프롬프트** - 무엇이 가능한지를 함께 실음
9. **요청 간 격리** - 다음 질문은 이전 질문을 모름

## 라이선스

MIT
