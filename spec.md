# nl2sql 스튜디오 구현 프롬프트

> 이 문서 전체를 새 프로젝트의 구현 지시서로 쓰십시오. 사전 지식 없이 이 문서만으로
> 전부 구현할 수 있도록 쓰여 있습니다.

---

## 0. 무엇을 만드는가

여러 데이터베이스를 등록해두고, 자연어 질문을 받아 **실행 가능한 SQL 문장을 만들어
보여주는** 웹 애플리케이션입니다. React 프론트엔드 + FastAPI 백엔드.

**만든 SQL을 실행하지는 않습니다.** 컴파일된 SQL 텍스트를 화면에 보여주는 것이 최종
산출물입니다. 행을 조회해 오는 기능은 이 범위에 없습니다.

### 두 개의 화면

**① 데이터소스 화면** — 조회 대상 DB를 등록하고, 그 DB의 스키마를 읽어와 저장합니다.

```
등록 (이름·드라이버·호스트·포트·DB명·스키마·계정·비밀번호)
   → 저장 (우리 메타데이터 DB에 행 하나)
   → "스키마 읽기" 버튼
   → 대상 DB에 접속해 테이블·컬럼·코멘트를 읽어옴
   → 우리 메타데이터 DB에 통째로 저장
   → 추가로, 사람이 "업무 지표"를 손으로 정의해 붙임
```

**② 질문 화면** — 데이터소스를 고르고 질문하면 SQL이 나옵니다.

```
질문 입력 + 데이터소스 선택
   → POST /api/ask
   → 저장된 스키마에서 질문과 관련된 테이블·지표만 추림 (TF-IDF)
   → LLM이 그것만 보고 JSON 조회 명세(AST)를 만듦
   → 컴파일러가 화이트리스트로 검증하고 SQL로 조립
   → 실패하면 에러를 LLM에게 돌려주고 최대 4번까지 스스로 고치게 함
   → 완성된 SQL을 화면에 표시 (이전 결과 아래에 쌓임)
```

### 세 개의 데이터베이스를 구분할 것

혼동하기 쉬우므로 이름을 정해둡니다.

| | 무엇 | 언제 접속하나 |
|---|---|---|
| **메타데이터 DB** | 이 앱이 자기 데이터를 쌓는 PostgreSQL. 등록된 데이터소스 목록, 읽어온 스키마, 업무 지표가 여기 있다 | 항상 |
| **대상 DB** | 사용자가 등록한 조회 대상. MySQL/PostgreSQL/Oracle | **스키마 동기화할 때만** |
| (없음) | 질의 실행 | **한 번도 안 함** — SQL을 만들기만 한다 |

질문에 답할 때 대상 DB에 **전혀 접속하지 않는다**는 점이 중요합니다. 필요한 건 이미
메타데이터 DB에 저장돼 있습니다.

---

## 1. 설계 원칙 — 반드시 지킬 것

### 1-1. 모델은 SQL 문자열을 쓰지 않는다

모델이 만드는 것은 JSON 조회 명세(AST)뿐입니다. `target_table`(또는 `metric`),
`aggregations`, `filters`, `group_by` — 이 다섯 키 외에는 받지 않습니다. 컴파일러가
등록된 테이블·컬럼·함수·연산자 화이트리스트로 검증한 뒤에만 SQL이 만들어집니다.

**모델이 쓴 문자열이 SQL 구조에 끼어들 자리가 없어야 합니다.** 값(리터럴)만 이스케이프를
거쳐 들어가고, 절과 식별자는 전부 코드가 조립합니다.

### 1-2. 등록과 동기화를 분리한다

데이터소스 저장(`POST /api/datasources`)과 스키마 읽기
(`POST /api/datasources/{id}/sync`)는 **별개의 엔드포인트**입니다. 하나로 합치지
마십시오.

이유는 실패의 성격이 다르기 때문입니다. 등록은 우리 DB에 행 하나를 쓰는 일이라 거의
실패하지 않고, 동기화는 남의 DB에 TCP로 붙는 일이라 방화벽·권한·호스트 오타로 늘
실패합니다. 하나로 묶으면 **접속이 안 될 때 등록 자체가 롤백되어, 고쳐서 다시 시도할
대상조차 남지 않습니다.**

대가로 "등록은 됐지만 스키마가 없는" 상태가 생깁니다. 그 상태를 빈 값으로 두지 말고
화면과 API가 명시적으로 말하게 하십시오 — `"스키마 없음 — 조회 불가"`.

### 1-3. 동기화는 통째로 교체한다

읽어온 스키마를 기존 것과 병합하지 마십시오. **지우고 다시 넣습니다.** 갱신 방식으로
덮어쓰기만 하면 대상 DB에서 드롭된 테이블이 영원히 남고, 모델은 존재하지 않는 테이블을
조회하는 SQL을 자신 있게 만들어냅니다.

그리고 **삭제와 삽입은 한 트랜잭션 안에서** 하십시오. 나눠서 커밋하면 그 사이에 들어온
질문이 "스키마가 비었다"는 답을 받습니다.

### 1-4. 실패는 예외가 아니라 문자열로 돌려준다 (도구 안에서만)

`compile_sql` 도구 안에서 컴파일 실패는 `raise`가 아니라 `return "error: ..."`입니다.
예외를 올리면 실행이 끊겨 모델이 고칠 기회가 없습니다. 문자열이면 그것이 대화에 들어가고
모델이 읽고 고쳐서 다시 부릅니다.

**에러 메시지는 항상 무엇이 가능한지를 함께 실어야 합니다** — 사용 가능한 테이블 목록,
지표 목록, 연산자 목록. 이 메시지가 곧 다음 시도의 프롬프트입니다.

(도구 바깥, 예컨대 데이터소스 CRUD의 실패는 평범하게 HTTP 에러로 처리하십시오.)

### 1-5. 성공 판정은 모델이 아니라 서버가 한다

`compile_sql`이 `"SQL: "`로 시작하는 문자열을 돌려주면, 모델의 다음 판단을 기다리지 않고
그 자리에서 루프를 끝냅니다. 도구 하나짜리 좁은 루프에서는 성공 여부를 서버가
결정론적으로 알 수 있고, 그럴 수 있으면 모델 판단에 맡기지 않는 편이 안전하고 빠릅니다.

### 1-6. 에이전트 루프는 질문 하나에만 존재한다

한 번의 `POST /api/ask` 요청 안에서 LLM이 도구를 최대 4번 부르는 것이 루프의 전부입니다.
요청이 끝나면 그 상태는 완전히 버려집니다.

**다음 질문은 이전 질문을 전혀 모릅니다.** 체크포인터를 달지 않고 `thread_id`도 쓰지
않습니다. 요청마다 그래프를 새로 만들고 새 메시지 목록으로 실행합니다 — 이전 요청의
메시지를 참조할 방법 자체가 코드에 없어야 합니다.

단, **화면에는 이전 질문·답변을 계속 보여줍니다.** "서버가 기억하지 않는 것"과 "화면에
기록을 남기는 것"은 다른 문제입니다. 프론트는 결과를 리스트에 쌓지만, 매 요청의 HTTP
body에는 이번 질문 하나만 담습니다.

### 1-7. 업무 지표는 컴파일러가 결정론적으로 전개한다

모델이 "이 질문은 `active_revenue` 지표다"까지만 판단하면, 그 지표의 정의와 고정 필터는
컴파일러가 자동으로 채웁니다. 모델이 지표를 알아보고도 고정 필터를 깜빡할 가능성을 아예
없애는 것이 목적입니다 — 정확성의 보증이 **모델의 기억이 아니라 컴파일러의 강제**에
있어야 합니다.

지표에는 두 종류가 있고, **고정 필터의 강제는 양쪽에 똑같이 적용됩니다.**

| 종류 | 무엇을 내나 | 정의 |
|---|---|---|
| `aggregate` | 숫자 하나 | `agg_function(agg_field)` |
| `projection` | 컬럼 몇 개 | `select_columns` 목록 |

조회형을 집계형과 같은 표에 두는 이유가 이 강제 때문입니다. `활성회원명단`이
`SELECT MEMBER_NM, EMAIL FROM TB_MEMBER WHERE STATUS_CD='ACTIVE'`일 때, 그
`STATUS_CD='ACTIVE'`가 빠지면 탈퇴 회원이 섞여 나갑니다 — 합계가 틀리는 것만큼 나쁩니다.
"지표는 숫자다"라는 직관을 따라 조회를 밖으로 빼면, 조회만 이 보증을 못 받습니다.

조회형 지표에 `group_by`를 함께 보내면 거부합니다. 컬럼 목록 자체가 이미 정의라,
거기에 집계를 겹치면 지표가 약속한 것과 다른 질의가 됩니다.

조회형은 행 수를 제한하지 않습니다(`limit`은 9절대로 만들지 않습니다). 큰 표를 가리키는
조회형 지표는 그대로 전체 조회가 되므로, 지표를 정의하는 사람이 알고 있어야 합니다.

### 1-8. 비밀번호는 들어가기만 하고 나오지 않는다

응답 모델에 `password` 필드를 두지 마십시오. 숨기는 로직이 아니라 **타입의 모양**으로
막습니다 — 비밀번호를 실을 수 있는 응답 형태가 존재하지 않게 합니다.

---

## 2. 프로젝트 구조

```
nl2sql-studio/
  backend/
    app.py                 # FastAPI 진입점
    db.py                  # 메타데이터 DB 커넥션 풀
    schema.sql             # 메타데이터 DB 스키마 (기동 시 적용)
    models.py              # pydantic 모델
    datasources.py         # 데이터소스 CRUD + 동기화
    catalog.py             # 드라이버별 카탈로그 읽기
    metrics.py             # 업무 지표 CRUD
    contract.py            # 메타데이터 DB → 계약서 dict
    pruner.py              # TF-IDF 프루너
    compiler.py            # AST 검증 + SQL 조립
    tool.py                # compile_sql 도구 (로그 포함)
    graph.py               # 에이전트 루프
    logging_config.py
    logs/
  frontend/
    src/
      App.tsx
      DatasourcePage.tsx   # 등록·동기화·지표 관리
      AskPage.tsx          # 질문·결과
      api.ts
```

**백엔드 의존성**: `fastapi`, `uvicorn`, `pydantic`, `psycopg[binary]`, `psycopg_pool`,
`PyMySQL`, `oracledb`, `langgraph`, `langchain-core`, `langchain-openai`,
`scikit-learn`, `numpy`.

**환경변수**

| 변수 | 뜻 |
|---|---|
| `DB_URL` | 메타데이터 DB (PostgreSQL) 접속 문자열 |
| `LLM_BASE_URL` | OpenAI 호환 엔드포인트 |
| `LLM_API_KEY` | 게이트웨이가 처리하면 아무 값이나 |
| `LLM_MODEL` | 모델 이름 |

---

## 3. 메타데이터 DB 스키마 (`schema.sql`)

기동할 때 이 파일을 실행해 없으면 만들게 하십시오.

```sql
CREATE TABLE IF NOT EXISTS datasource (
    id          UUID PRIMARY KEY,
    -- 화면과 로그에서 이 데이터소스를 부르는 이름. 유일해야 한다.
    name        TEXT        NOT NULL UNIQUE,
    description TEXT        NOT NULL DEFAULT '',
    -- 'mysql' | 'postgresql' | 'oracle'. 카탈로그 질의와 식별자 인용 문자가
    -- 여기서 갈린다.
    driver      TEXT        NOT NULL,
    host        TEXT        NOT NULL,
    port        INTEGER     NOT NULL,
    db_name     TEXT        NOT NULL,
    -- MySQL은 스키마와 데이터베이스가 같은 것이라 db_name을 그대로 쓴다.
    -- PostgreSQL은 비우면 'public', Oracle은 테이블 소유자(owner)를 뜻한다.
    db_schema   TEXT        NOT NULL DEFAULT '',
    username    TEXT        NOT NULL DEFAULT '',
    -- 평문이다. 동기화 시점에 대상 DB에 실제로 접속해야 하므로 복원 가능해야
    -- 하고, 이 앱에는 키 저장소가 없다. API로는 절대 나가지 않는다.
    password    TEXT        NOT NULL DEFAULT '',
    synced_at   TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS datasource_table (
    id            UUID PRIMARY KEY,
    datasource_id UUID NOT NULL REFERENCES datasource (id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    -- 대상 DB의 테이블 코멘트. 프루너가 검색하는 본문이라, 비어 있으면 그
    -- 테이블은 이름으로만 걸린다.
    description   TEXT NOT NULL DEFAULT '',
    UNIQUE (datasource_id, name)
);

CREATE TABLE IF NOT EXISTS datasource_column (
    id          BIGSERIAL PRIMARY KEY,
    table_id    UUID    NOT NULL REFERENCES datasource_table (id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    data_type   TEXT    NOT NULL DEFAULT '',
    description TEXT    NOT NULL DEFAULT '',
    -- 대상 DB에서의 컬럼 순서. 키가 앞에 온다는 정보가 여기 들어 있어서,
    -- 사람이 읽을 때도 모델이 읽을 때도 원래 순서가 낫다.
    ordinal     INTEGER NOT NULL,
    UNIQUE (table_id, name)
);

-- 업무 지표. 스키마에서 자동으로 뽑을 수 없는 개념이라 사람이 정의한다.
-- 동기화가 이 표를 건드리지 않는 것이 중요하다 — 스키마를 다시 읽어도
-- 지표 정의는 남아야 한다.
CREATE TABLE IF NOT EXISTS datasource_metric (
    id            UUID PRIMARY KEY,
    datasource_id UUID NOT NULL REFERENCES datasource (id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    table_name    TEXT NOT NULL,
    agg_field     TEXT NOT NULL,
    -- SUM | COUNT | AVG | MIN | MAX
    agg_function  TEXT NOT NULL,
    -- [{"field": "...", "operator": "equals", "value": "..."}, ...]
    fixed_filters JSONB NOT NULL DEFAULT '[]',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (datasource_id, name)
);

CREATE INDEX IF NOT EXISTS idx_datasource_table_ds ON datasource_table (datasource_id);
CREATE INDEX IF NOT EXISTS idx_datasource_column_table ON datasource_column (table_id);
CREATE INDEX IF NOT EXISTS idx_datasource_metric_ds ON datasource_metric (datasource_id);
```

---

## 4. 데이터소스 등록 (CRUD)

### 모델

```python
from enum import Enum
from uuid import UUID
from pydantic import BaseModel, field_validator, model_validator


class Driver(str, Enum):
    MYSQL = "mysql"
    POSTGRESQL = "postgresql"
    ORACLE = "oracle"


DEFAULT_PORTS = {Driver.MYSQL: 3306, Driver.POSTGRESQL: 5432, Driver.ORACLE: 1521}


class DatasourceInput(BaseModel):
    """등록 화면이 보내는 것. 비밀번호는 들어오기만 한다."""
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
        # 0은 "안 적었다"는 뜻이다. 폼에서 비워둔 것을 거절하는 대신 드라이버의
        # 기본 포트를 쓴다 — 3306과 5432를 외우게 할 이유가 없다.
        if self.port == 0:
            self.port = DEFAULT_PORTS[self.driver]
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        return self


class Datasource(BaseModel):
    """저장된 데이터소스. password가 없는 것이 이 클래스의 요점이다."""
    id: UUID
    name: str
    description: str
    driver: Driver
    host: str
    port: int
    db_name: str
    db_schema: str
    username: str
    synced_at: str | None      # 한 번도 동기화하지 않았으면 null
    table_count: int
    metric_count: int
```

### 엔드포인트

| 메서드 | 경로 | 하는 일 |
|---|---|---|
| GET | `/api/datasources` | 목록 (이름순) |
| POST | `/api/datasources` | 등록 → 201 |
| GET | `/api/datasources/{id}` | 조회 |
| PUT | `/api/datasources/{id}` | 수정 |
| DELETE | `/api/datasources/{id}` | 삭제 → 204 (스키마·지표도 CASCADE) |
| POST | `/api/datasources/{id}/sync` | 스키마 읽기 |
| GET | `/api/datasources/{id}/schema` | 저장된 스키마 |

### 목록 질의

`table_count`와 `metric_count`는 상관 서브질의로 함께 가져오십시오. 목록 행이 "테이블
N개"를 보여줘야 하는데 그 값은 다른 표에 있습니다.

```sql
SELECT d.*,
       (SELECT count(*) FROM datasource_table t WHERE t.datasource_id = d.id) AS table_count,
       (SELECT count(*) FROM datasource_metric m WHERE m.datasource_id = d.id) AS metric_count
  FROM datasource d
 ORDER BY d.name;
```

### 이름 중복은 저장 시점에 막는다

`UNIQUE` 제약에만 맡기지 마십시오. 거기까지 가면 드라이버의 제약 위반 예외가 **500**으로
나가고 화면에는 원인을 알 수 없는 메시지가 뜹니다. 게다가 `UNIQUE`는 대소문자를
구분하므로 `Sales`와 `sales`가 둘 다 들어갑니다.

```python
def _guard_duplicate_name(name: str, except_id: UUID | None = None) -> None:
    """@raises 409"""
    if except_id is None:
        row = db.one("SELECT id FROM datasource WHERE lower(name) = lower(%s)", name)
    else:
        row = db.one("SELECT id FROM datasource WHERE lower(name) = lower(%s) AND id <> %s",
                      name, except_id)
    if row:
        raise ApiException(409, f"'{name}' 이름의 데이터소스가 이미 있습니다")
```

### 수정할 때 빈 비밀번호는 "바꾸지 않음"이다

조회 응답에 비밀번호가 없으니 편집 폼은 그것을 되돌려 보낼 수 없습니다. 빈 값을 그대로
`UPDATE`하면 **이름 하나 고칠 때마다 대상 DB 접속이 끊깁니다.**

```sql
UPDATE datasource SET
    name = %s, description = %s, driver = %s, host = %s, port = %s,
    db_name = %s, db_schema = %s, username = %s,
    password = CASE WHEN %s = '' THEN password ELSE %s END,
    updated_at = now()
WHERE id = %s
```

같은 파라미터를 두 번 바인딩하는 것이 의도입니다.

---

## 5. 동기화 — 대상 DB의 카탈로그 읽기

```python
CONNECT_TIMEOUT = 10   # 방화벽에 막히면 기본값은 분 단위로 매달린다
```

### 흐름

```python
def sync(datasource_id: UUID) -> SyncResult:
    # 이 조회만 raw dict을 쓴다 — 대상 DB에 붙으려면 비밀번호가 필요하다.
    row = db.one("SELECT * FROM datasource WHERE id = %s", datasource_id)
    if row is None:
        raise ApiException(404, f"데이터소스를 찾을 수 없습니다: {datasource_id}")

    driver = Driver(row["driver"])
    if driver is Driver.MYSQL:
        tables = read_mysql(row)
    elif driver is Driver.ORACLE:
        tables = read_oracle(row)
    else:
        tables = read_postgresql(row)

    return _replace_schema(datasource_id, tables)
```

### 세 드라이버 공통 규칙

- **테이블 질의 1회 + 컬럼 질의 1회**, 메모리에서 테이블 이름으로 합칩니다. 테이블마다
  컬럼 질의를 따로 날리면 테이블 수만큼 왕복입니다 — 수백 개짜리 스키마에서 버튼이
  고장 난 것처럼 보일 만큼 느려집니다.
- 테이블 질의가 걸러낸 목록에 없는 테이블의 컬럼은 **버립니다.** 컬럼 질의에 딸려 올 수
  있고, 없는 테이블에 컬럼을 붙일 수는 없습니다.
- `try/finally`로 연결을 닫습니다.
- 이름·코멘트를 문자열로 정규화합니다 — 드라이버에 따라 `bytes`로 오는 경우가 있고,
  그대로 두면 나중에 검증이나 직렬화에서 동기화 전체가 죽습니다.

```python
def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value) if value is not None else ""
```

### PostgreSQL

**`information_schema`가 아니라 `pg_catalog`를 씁니다.** PostgreSQL은 코멘트를
`pg_description`에 따로 두고 information_schema로는 꺼낼 수 없습니다(표준에 없는
것이라서). 코멘트 없이 가져오면 프루너가 검색할 본문이 이름밖에 남지 않습니다.

```python
import psycopg
from psycopg.conninfo import make_conninfo

def read_postgresql(row) -> list[dict]:
    schema = row["db_schema"] or "public"
    conninfo = make_conninfo(
        host=row["host"], port=row["port"],
        user=row["username"] or None, password=row["password"] or None,
        dbname=row["db_name"], connect_timeout=CONNECT_TIMEOUT,
    )
    try:
        connection = psycopg.connect(conninfo)
    except Exception as error:
        raise _unreachable(row, error) from error

    try:
        with connection.cursor() as cur:
            # relkind: r=테이블, v=뷰, m=구체화 뷰, p=파티션 부모, f=외부 테이블
            cur.execute("""
                SELECT c.relname, coalesce(obj_description(c.oid, 'pg_class'), '')
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = %s AND c.relkind IN ('r','v','m','p','f')
                 ORDER BY c.relname
            """, (schema,))
            tables = {name: {"name": name, "description": comment, "columns": []}
                      for name, comment in cur.fetchall()}

            cur.execute("""
                SELECT c.relname, a.attname,
                       format_type(a.atttypid, a.atttypmod),
                       coalesce(col_description(c.oid, a.attnum), '')
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                  JOIN pg_attribute a ON a.attrelid = c.oid
                 WHERE n.nspname = %s AND c.relkind IN ('r','v','m','p','f')
                   -- attnum > 0은 시스템 컬럼(ctid, xmin)을 뺀다. 드롭된 컬럼은
                   -- 행이 남아 있으므로 attisdropped도 함께 본다.
                   AND a.attnum > 0 AND NOT a.attisdropped
                 ORDER BY c.relname, a.attnum
            """, (schema,))
            for table_name, column, data_type, comment in cur.fetchall():
                if table_name in tables:
                    tables[table_name]["columns"].append(
                        {"name": column, "type": _text(data_type),
                         "description": _text(comment)})
    finally:
        connection.close()
    return list(tables.values())
```

### MySQL / MariaDB

```python
import pymysql

def read_mysql(row) -> list[dict]:
    try:
        connection = pymysql.connect(
            host=row["host"], port=row["port"], user=row["username"],
            password=row["password"], database=row["db_name"],
            connect_timeout=CONNECT_TIMEOUT, read_timeout=30, charset="utf8mb4",
        )
    except Exception as error:
        raise _unreachable(row, error) from error

    try:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME, TABLE_COMMENT FROM information_schema.TABLES"
                " WHERE TABLE_SCHEMA = %s AND TABLE_TYPE IN ('BASE TABLE','VIEW')"
                " ORDER BY TABLE_NAME", (row["db_name"],))
            tables = {_text(n): {"name": _text(n), "description": _text(c), "columns": []}
                      for n, c in cur.fetchall()}

            # DATA_TYPE이 아니라 COLUMN_TYPE이다. 전자는 'varchar'만 주고,
            # 후자는 'varchar(36)'과 enum 값까지 준다 — 모델이 보는 계약서에서
            # 그 둘은 다른 정보다.
            cur.execute(
                "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT"
                "  FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s"
                " ORDER BY TABLE_NAME, ORDINAL_POSITION", (row["db_name"],))
            for table_name, column, column_type, comment in cur.fetchall():
                table = tables.get(_text(table_name))
                if table is not None:
                    table["columns"].append(
                        {"name": _text(column), "type": _text(column_type),
                         "description": _text(comment)})
    finally:
        connection.close()
    return list(tables.values())
```

### Oracle

```python
import oracledb

def read_oracle(row) -> list[dict]:
    # owner는 스키마 칸, 비어 있으면 접속 계정. 대문자로 올리는 것은 Oracle이
    # 따옴표 없는 식별자를 대문자로 저장하기 때문이다 — 'hr'로 적은 스키마는
    # 데이터 딕셔너리에서 'HR'이고, 그대로 넣으면 한 건도 걸리지 않는다.
    owner = (row["db_schema"] or row["username"] or "").strip().upper()
    if not owner:
        raise ApiException(400, "Oracle 데이터소스는 스키마나 계정 중 하나가 필요합니다")

    try:
        connection = oracledb.connect(
            user=row["username"], password=row["password"],
            host=row["host"], port=row["port"], service_name=row["db_name"],
            # `connect_timeout`이 아니다 — python-oracledb가 받는 이름은
            # 이것이고, 틀린 이름은 TypeError로 동기화 전체가 죽는다.
            tcp_connect_timeout=CONNECT_TIMEOUT,
        )
    except Exception as error:
        raise _unreachable(row, error) from error

    try:
        with connection.cursor() as cur:
            # all_tables가 아니라 all_tab_comments를 읽는다 — all_tables에는
            # 뷰가 없고, 다른 두 경로는 뷰를 포함한다.
            # user_*가 아니라 all_*인 것도 중요하다. user_*는 접속 계정 소유
            # 객체만 보여주므로, 조회 전용 계정으로 남의 스키마를 읽는 흔한
            # 구성에서 빈 스키마가 돌아온다.
            cur.execute("""
                SELECT table_name, comments FROM all_tab_comments
                 WHERE owner = :owner AND table_type IN ('TABLE','VIEW')
                 ORDER BY table_name
            """, owner=owner)
            tables = {_text(n): {"name": _text(n), "description": _text(c), "columns": []}
                      for n, c in cur.fetchall()}

            cur.execute("""
                SELECT c.table_name, c.column_name, c.data_type, c.data_length,
                       c.data_precision, c.data_scale, cc.comments
                  FROM all_tab_columns c
                  LEFT JOIN all_col_comments cc
                    ON cc.owner = c.owner AND cc.table_name = c.table_name
                   AND cc.column_name = c.column_name
                 WHERE c.owner = :owner
                 ORDER BY c.table_name, c.column_id
            """, owner=owner)
            for tname, column, type_name, length, precision, scale, comment in cur.fetchall():
                table = tables.get(_text(tname))
                if table is not None:
                    table["columns"].append(
                        {"name": _text(column),
                         "type": _oracle_type(type_name, length, precision, scale),
                         "description": _text(comment)})
    finally:
        connection.close()
    return list(tables.values())


def _oracle_type(name, length, precision, scale) -> str:
    """`VARCHAR2(36)`, `NUMBER(10,2)` — 길이까지 붙인 컬럼 타입.

    다른 두 드라이버가 길이를 싣는 것과 맞춘다.
    """
    base = _text(name).upper()
    if base == "NUMBER":
        if precision is None:
            return "NUMBER"
        return f"NUMBER({precision},{scale})" if scale else f"NUMBER({precision})"
    if base in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW") and length:
        return f"{base}({length})"
    return base
```

### 접속 실패 보고

```python
def _unreachable(row, error) -> ApiException:
    """드라이버가 말한 첫 줄만 싣는다.

    뒤에 붙는 스택과 힌트는 화면에서 읽을 수 없을 만큼 길고, 실제 원인은 언제나
    첫 줄에 있다 — "Access denied for user", "Can't connect to MySQL server".
    """
    reason = str(error).strip().splitlines()
    return ApiException(
        502,
        f"{row['name']}의 스키마를 읽지 못했습니다 "
        f"({row['host']}:{row['port']}/{row['db_name']}): "
        f"{reason[0] if reason else type(error).__name__}"
    )
```

**502이지 500이 아닙니다** — 우리 버그가 아니라 저쪽이 안 되는 것이고, 그 둘을 구분해야
고칠 곳을 압니다.

### 저장 — 한 트랜잭션에서 통째 교체

```python
def _replace_schema(datasource_id: UUID, tables: list[dict]) -> SyncResult:
    # 행을 먼저 전부 메모리에서 만든다. 컬럼마다 execute를 부르면 컬럼마다
    # 왕복이고, 수천 컬럼이면 그 루프만 수십 초다. executemany는 한 번의
    # 교환으로 밀어 넣는다.
    table_rows, column_rows = [], []
    for table in tables:
        table_id = uuid4()
        table_rows.append((table_id, datasource_id, table["name"], table["description"]))
        for ordinal, column in enumerate(table["columns"]):
            column_rows.append((table_id, column["name"], column["type"],
                                 column["description"], ordinal))

    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM datasource_table WHERE datasource_id = %s", (datasource_id,))
        if table_rows:
            cur.executemany(
                "INSERT INTO datasource_table (id, datasource_id, name, description)"
                " VALUES (%s,%s,%s,%s)", table_rows)
        if column_rows:
            cur.executemany(
                "INSERT INTO datasource_column (table_id, name, data_type, description, ordinal)"
                " VALUES (%s,%s,%s,%s,%s)", column_rows)
        cur.execute("UPDATE datasource SET synced_at = now(), updated_at = now()"
                    " WHERE id = %s RETURNING synced_at", (datasource_id,))
        synced = cur.fetchone()

    log.info("datasource %s synced: %d tables, %d columns",
             datasource_id, len(tables), len(column_rows))
    return SyncResult(table_count=len(tables), column_count=len(column_rows),
                       synced_at=synced[0].isoformat())
```

`datasource_metric`은 건드리지 않습니다 — 스키마를 다시 읽어도 지표 정의는 남아야 합니다.

**0건은 에러가 아닙니다.** 접속은 됐는데 읽을 게 없었다는 뜻이고, Oracle owner 오타나
PostgreSQL 스키마 이름 오타가 전부 여기로 떨어집니다. 502와 달리 아무것도 빨갛지 않으므로
실무에서 제일 오래 붙잡게 되는 실패입니다 — 화면에 "테이블 0개"를 **경고 색으로** 띄우십시오.

---

## 6. 업무 지표

스키마에서 자동으로 뽑을 수 없는 개념입니다. `active_revenue`가
`SUM(sales_amount) WHERE contract_status='ACTIVE'`라는 사실은 DB 어디에도 없고 사람만
압니다. 그래서 사람이 등록합니다.

| 메서드 | 경로 |
|---|---|
| GET | `/api/datasources/{id}/metrics` |
| POST | `/api/datasources/{id}/metrics` |
| DELETE | `/api/datasources/{id}/metrics/{metric_id}` |

```python
class MetricKind(str, Enum):
    AGGREGATE = "aggregate"      # 숫자 하나
    PROJECTION = "projection"    # 컬럼 몇 개


class MetricInput(BaseModel):
    name: str
    description: str = ""
    kind: MetricKind = MetricKind.AGGREGATE
    table_name: str
    # kind=aggregate일 때만
    agg_field: str | None = None
    agg_function: str | None = None    # SUM | COUNT | AVG | MIN | MAX
    # kind=projection일 때만
    select_columns: list[str] = []
    fixed_filters: list[dict] = []
```

**종류에 맞지 않는 필드는 저장 전에 비우십시오.** 집계로 저장했는데 컬럼 목록이 딸려
있는 행이 생기면, 나중에 어느 쪽이 진짜 정의인지 알 방법이 없습니다.

### 등록 시점에 검증하십시오

지표가 가리키는 테이블·컬럼이 **저장된 스키마에 실제로 있는지** 확인하고, 없으면 400으로
거부하십시오. 나중에 질문 시점에 발견하면 사용자는 왜 실패하는지 알 수 없습니다.

- `table_name`이 그 데이터소스의 `datasource_table`에 있는가
- `aggregate`이면 `agg_field`가 그 테이블의 컬럼인가 (또는 `*`),
  `agg_function`이 다섯 개 중 하나인가
- `projection`이면 `select_columns`가 비어 있지 않고, **그 전부가** 그 테이블의 컬럼인가
- 각 `fixed_filters[].field`가 그 테이블의 컬럼인가
- 날짜 컬럼을 가리키는 `fixed_filters[].value`가 `YYYY-MM-DD` 또는
  `YYYY-MM-DD HH:MM:SS` 이고 달력상 존재하는 날짜인가 (9절)

### 동기화 후 깨진 지표

동기화는 스키마를 통째로 교체하므로, 대상 DB에서 컬럼이 사라지면 지표가 **깨진 상태**로
남을 수 있습니다. 계약서를 만들 때(7절) 검사해서 **깨진 지표는 모델에게 보여주지 말고
경고 로그를 남기십시오.** 화면에서도 그 지표에 "스키마와 맞지 않음" 표시를 하십시오.

검사 범위는 종류마다 다릅니다. 집계형은 `agg_field` 하나지만 조회형은
`select_columns` **전부**가 살아 있어야 합니다. `fixed_filters[].field`는 양쪽 다
확인하십시오 — 필터가 깨지면 지표가 보장하려던 조건이 조용히 빠집니다.

---

## 7. `contract.py` — 메타데이터 DB에서 계약서 만들기

프루너와 컴파일러가 쓸 형태로 읽어옵니다.

```python
def load_contract(datasource_id: UUID) -> dict:
    """@raises ApiException 400 스키마가 비었을 때"""
    row = db.one("SELECT id, name, driver FROM datasource WHERE id = %s", datasource_id)
    if row is None:
        raise ApiException(404, "데이터소스를 찾을 수 없습니다")

    rows = db.query("""
        SELECT t.name AS table_name, t.description AS table_note,
               c.name AS column_name, c.data_type, c.description AS column_note
          FROM datasource_table t
          LEFT JOIN datasource_column c ON c.table_id = t.id
         WHERE t.datasource_id = %s
         ORDER BY t.name, c.ordinal
    """, datasource_id)

    tables = {}
    for r in rows:
        table = tables.setdefault(r["table_name"], {
            "name": r["table_name"], "description": r["table_note"] or "", "columns": []})
        # LEFT JOIN이라 컬럼이 하나도 없는 테이블은 NULL 한 줄로 온다.
        if r["column_name"] is not None:
            table["columns"].append({"name": r["column_name"],
                                      "type": r["data_type"] or "",
                                      "description": r["column_note"] or ""})

    if not tables:
        raise ApiException(400, f"'{row['name']}'의 스키마가 비어 있습니다. "
                                 "데이터소스 화면에서 스키마 읽기를 먼저 실행하십시오")

    metrics = []
    for m in db.query("SELECT * FROM datasource_metric WHERE datasource_id = %s",
                      datasource_id):
        table = tables.get(m["table_name"])
        # 동기화로 테이블·컬럼이 사라졌으면 이 지표는 더 이상 유효하지 않다.
        # 모델에게 보여주면 컴파일 단계에서 반드시 실패하는 AST를 유도한다.
        if table is None or (m["agg_field"] != "*" and
                              m["agg_field"] not in [c["name"] for c in table["columns"]]):
            log.warning("깨진 지표를 제외합니다: %s (%s.%s)",
                        m["name"], m["table_name"], m["agg_field"])
            continue
        metrics.append({
            "name": m["name"], "description": m["description"],
            "table": m["table_name"],
            "aggregation": {"field": m["agg_field"], "function": m["agg_function"]},
            "fixed_filters": m["fixed_filters"],
        })

    return {"name": row["name"], "driver": row["driver"],
            "tables": list(tables.values()), "metrics": metrics}
```

**이 함수를 매 요청마다 호출하십시오. 프로세스에 캐시하지 마십시오.** 캐시가 있으면
동기화를 눌러도 서버를 재시작할 때까지 옛 스키마가 계속 쓰이고, 그 증상은 원인이 전혀
보이지 않습니다.

---

## 8. `pruner.py` — TF-IDF

스키마 전체를 프롬프트에 넣으면 테이블이 늘어날수록 토큰과 노이즈가 같이 늘고, 모델이
엉뚱한 테이블을 고를 여지도 커집니다. 수백 개짜리 스키마가 들어올 수 있으므로 질문과
관련된 것만 추립니다.

**scikit-learn의 기본 토큰화기를 그대로 쓰면 안 됩니다.** 기본 설정은 공백으로 구분된
덩어리를 통째로 하나의 토큰으로 봅니다. 한국어는 조사가 붙어서 `"매출액의"`,
`"매출액을"`, `"매출액이"`가 전부 다른 토큰이 되어, 계약서 설명문의 `"매출액"`과
겹치지 않습니다. 한글은 2-gram으로 쪼개는 토큰화기를 직접 넘기십시오.

```python
import re
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


def korean_aware_tokens(text: str) -> list[str]:
    """영문/숫자는 통째로, 한글은 2-gram으로. 조사 차이를 흡수한다."""
    found = []
    for match in re.finditer(r"[a-zA-Z0-9_]+|[가-힣]+", text.lower()):
        chunk = match.group()
        if chunk[0].isascii() or len(chunk) == 1:
            found.append(chunk)
        else:
            found.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
    return found


class Pruner:
    def __init__(self, contract: dict):
        self.entries = []
        for table in contract["tables"]:
            # 코멘트가 검색 본문의 대부분이다. 코멘트 없는 스키마는 이름으로만
            # 걸린다 — 동기화가 코멘트를 반드시 읽어와야 하는 이유다.
            text = " ".join(
                [table["name"], table.get("description", "")]
                + [c.get("description", "") for c in table["columns"]]
                + [c["name"] for c in table["columns"]])
            self.entries.append(("table", table, text))
        for metric in contract.get("metrics", []):
            text = f"{metric['name']} {metric.get('description', '')} {metric['table']}"
            self.entries.append(("metric", metric, text))

        self._tables_by_name = {t["name"]: t for t in contract["tables"]}
        self.vectorizer = TfidfVectorizer(tokenizer=korean_aware_tokens, token_pattern=None)
        self.matrix = self.vectorizer.fit_transform([e[2] for e in self.entries])

    def prune(self, question: str, top_k: int = 3) -> dict:
        query_vec = self.vectorizer.transform([question])
        scores = cosine_similarity(query_vec, self.matrix).flatten()
        order = np.argsort(scores)[::-1][:top_k]

        tables, metrics, seen = [], [], set()
        for idx in order:
            if scores[idx] <= 0:
                continue
            kind, item, _ = self.entries[idx]
            if kind == "table" and item["name"] not in seen:
                tables.append(item)
                seen.add(item["name"])
            elif kind == "metric":
                metrics.append(item)
                # 지표가 걸리면 그 지표가 속한 테이블도 함께 실어야 모델이
                # 필터에 쓸 컬럼을 볼 수 있다.
                if item["table"] not in seen:
                    tables.append(self._tables_by_name[item["table"]])
                    seen.add(item["table"])

        # 아무것도 걸리지 않으면 상위 몇 개라도 돌려준다. 빈 스키마를 받은
        # 모델은 컬럼을 지어내기 시작하고, 그 편이 나쁘다.
        if not tables:
            for idx in order[:top_k]:
                kind, item, _ = self.entries[idx]
                if kind == "table":
                    tables.append(item)
        return {"tables": tables, "metrics": metrics}
```

---

## 9. `compiler.py` — AST 검증 + SQL 조립

```python
class Compiler:
    KEYS = {"target_table", "metric", "aggregations", "filters", "group_by"}

    # 모델이 자주 시도하지만 이 컴파일러가 만들지 않는 것들. 이름을 알아보고
    # 무엇이 없는지 말해주는 편이, 낯선 키라고만 하는 것보다 고쳐 쓰기 쉽다.
    UNSUPPORTED = {
        "having": "집계 결과 필터(HAVING)",
        "join": "테이블 조인(JOIN)", "joins": "테이블 조인(JOIN)",
        "distinct": "중복 제거(DISTINCT)",
        "order_by": "정렬(ORDER BY)",
        "limit": "행수 제한(LIMIT)",
    }
    _FUNCTIONS = ("SUM", "COUNT", "AVG", "MIN", "MAX")
    # >=와 <=가 함께 있는 이유: >와 <만으로 기간을 자르면 시작일과 종료일이
    # 조용히 빠진다.
    _OPERATORS = {"equals": "=", "not_equals": "!=", "greater_than": ">",
                  "greater_or_equal": ">=", "less_than": "<", "less_or_equal": "<="}
    # 하나의 대상에 값이 여럿 매달린 질문이 흔하다. 없으면 같은 질문을 값의
    # 개수만큼 반복해야 한다.
    _LIST_OPERATORS = {"in": "IN", "not_in": "NOT IN"}
    # 식별자 인용 문자. 잘못 고르면 예약어 컬럼(status, order, rank)에서
    # 문법 오류가 난다.
    _QUOTE = {"mysql": "`", "postgresql": '"', "oracle": '"'}

    def __init__(self, contract: dict):
        self.allowed_tables = {t["name"]: [c["name"] for c in t["columns"]]
                                for t in contract["tables"]}
        self.metrics = {m["name"]: m for m in contract.get("metrics", [])}
        self.quote = self._QUOTE.get(contract["driver"], '"')

    def compile(self, ast: dict) -> str:
        # 모르는 키를 무시하면 "상위 10개만"이라는 질문에 그 조건이 빠진
        # 쿼리가 나가고, 아무도 그 사실을 말하지 않는다. 모델은 답했다고
        # 믿고 사용자에게 내놓는다 — 에러보다 나쁜 결과다.
        unknown = set(ast) - self.KEYS
        if unknown:
            named = [self.UNSUPPORTED[k] for k in sorted(unknown) if k in self.UNSUPPORTED]
            other = sorted(k for k in unknown if k not in self.UNSUPPORTED)
            parts = []
            if named:
                parts.append("이 도구는 " + ", ".join(dict.fromkeys(named)) + "을(를) 만들지 않습니다")
            if other:
                parts.append(f"모르는 항목입니다 -> {', '.join(other)}")
            raise ValueError("; ".join(parts) + ". 쓸 수 있는 키: " + ", ".join(sorted(self.KEYS)))

        metric_name = ast.get("metric")
        if metric_name:
            metric = self.metrics.get(metric_name)
            if metric is None:
                raise ValueError(f"등록되지 않은 지표입니다 -> {metric_name!r} "
                                  f"(사용 가능: {', '.join(self.metrics) or '없음'})")
            table = metric["table"]
            aggregations = [{**metric["aggregation"], "alias": metric_name}]
            # 지표의 고정 필터가 먼저, 모델이 낸 필터가 뒤에. 모델이 고정 필터를
            # 잊어도 여기서 항상 붙는다 — 이것이 지표 시스템의 요점이다.
            filters = list(metric.get("fixed_filters", [])) + list(ast.get("filters") or [])
        else:
            table = ast.get("target_table")
            aggregations = ast.get("aggregations") or []
            filters = ast.get("filters") or []

        resolved_table = self._match(table, self.allowed_tables)
        if resolved_table is None:
            raise ValueError(f"조회할 수 없는 테이블입니다 -> {table!r} "
                              f"(사용 가능: {', '.join(self.allowed_tables)})")
        allowed = self.allowed_tables[resolved_table]

        select = []
        for agg in aggregations:
            field = agg.get("field")
            function = str(agg.get("function", "")).upper()
            if function not in self._FUNCTIONS:
                raise ValueError(f"허용되지 않은 집계 함수입니다 -> {function!r} "
                                  f"(사용 가능: {', '.join(self._FUNCTIONS)})")
            if field == "*":
                target = "*"   # COUNT(*)의 별은 컬럼이 아니라 문법이라 감싸면 안 된다
            else:
                resolved = self._match(field, allowed)
                if resolved is None:
                    raise ValueError(f"존재하지 않는 컬럼입니다 -> {resolved_table}.{field}")
                target = self._identifier(resolved)
            alias = agg.get("alias") or f"{function.lower()}_{field}"
            select.append(f"{function}({target}) AS {self._alias(alias)}")

        group_cols = []
        for col in (ast.get("group_by") or []):
            resolved = self._match(col, allowed)
            if resolved is None:
                raise ValueError(f"그룹화할 수 없는 컬럼입니다 -> {resolved_table}.{col}")
            group_cols.append(self._identifier(resolved))

        # 그룹 컬럼을 SELECT 맨 앞에도 넣는다. 합계만 나오면 그게 어느 그룹의
        # 숫자인지 알 길이 없다.
        select = group_cols + select

        clauses = [f"SELECT {', '.join(select) or '*'}",
                   f"FROM {self._identifier(resolved_table)}"]
        where = [self._filter(f, resolved_table, allowed) for f in filters]
        if where:
            clauses.append("WHERE " + " AND ".join(where))
        if group_cols:
            clauses.append("GROUP BY " + ", ".join(group_cols))
        return " ".join(clauses) + ";"

    def _filter(self, spec: dict, table: str, allowed: list) -> str:
        if not isinstance(spec, dict):
            raise ValueError(f"filters의 항목은 객체여야 합니다 -> {spec!r}")
        resolved = self._match(spec.get("field"), allowed)
        if resolved is None:
            raise ValueError(f"필터링할 수 없는 컬럼입니다 -> {table}.{spec.get('field')}")
        column = self._identifier(resolved)
        operator = spec.get("operator")

        if operator in self._LIST_OPERATORS:
            values = spec.get("value")
            if not isinstance(values, (list, tuple)) or not values:
                raise ValueError(f"{operator!r}의 value는 비어 있지 않은 목록이어야 합니다 -> {values!r}")
            rendered = ", ".join(self._literal(v) for v in values)
            return f"{column} {self._LIST_OPERATORS[operator]} ({rendered})"

        if operator not in self._OPERATORS:
            raise ValueError(f"허용되지 않는 비교 연산자입니다 -> {operator!r} (사용 가능: "
                              f"{', '.join(list(self._OPERATORS) + list(self._LIST_OPERATORS))})")
        return f"{column} {self._OPERATORS[operator]} {self._literal(spec.get('value'))}"

    def _match(self, name, options):
        """대소문자를 무시하고 찾아, 계약서에 적힌 표기로 돌려준다.

        모델은 소문자로 적어 보내는 경향이 있고, Oracle 계약서의 이름은 전부
        대문자다.
        """
        if not isinstance(name, str):
            return None
        for candidate in options:
            if candidate.lower() == name.lower():
                return candidate
        return None

    def _identifier(self, name: str) -> str:
        """계약서에 있는 이름을 드라이버의 인용 문자로 감싼다.

        영문/숫자/밑줄만 받으면 안 된다. 실제 스키마에는 `평균기온(°C)`,
        `사고원인-대분류` 같은 컬럼이 있고, 인용만 하면 DB는 문제없이 받는다.
        인용 부호 안에서 특별한 문자는 인용 부호 자신뿐이라, 그것과 NUL만
        거부하면 인용이 깨지지 않는다.
        """
        text = str(name)
        if not text:
            raise ValueError("빈 식별자는 쓸 수 없습니다")
        if self.quote in text or "\x00" in text:
            raise ValueError(f"식별자에 {self.quote}나 NUL을 쓸 수 없습니다 -> {name!r}")
        return f"{self.quote}{text}{self.quote}"

    def _alias(self, alias: str) -> str:
        """별칭은 계약서에 없는, 모델이 그때그때 지어내는 유일한 식별자다.

        화이트리스트가 받쳐주지 않으므로 길이와 공백까지 본다 — 계약서의
        이름과 달리 모델의 별칭은 그 둘을 지킨다는 보장이 없다.
        """
        text = str(alias)
        if not text or text != text.strip():
            raise ValueError(f"별칭은 비어 있거나 공백으로 끝날 수 없습니다 -> {alias!r}")
        if len(text) > 64:
            raise ValueError(f"별칭은 64자를 넘을 수 없습니다 -> {text[:20]}...")
        if self.quote in text or "\x00" in text:
            raise ValueError(f"별칭에 {self.quote}나 NUL을 쓸 수 없습니다 -> {alias!r}")
        return f"{self.quote}{text}{self.quote}"

### 날짜 컬럼은 타입이 붙은 리터럴로 나간다

`WHERE d >= '2026-01-01'` 은 PostgreSQL·MySQL 에서는 통하지만 **Oracle 에서 깨집니다.**
문자열→DATE 암묵 변환이 `NLS_DATE_FORMAT` 에 좌우되고, 기본값이 `DD-MON-RR` 인 환경에서는
`ORA-01861` 이 납니다. 따옴표를 빼는 것은 답이 아닙니다 — `>= 2026-01-01` 은 산술식
(`2026-1-1 = 2024`)이 되어 **에러 없이 조용히 틀립니다.**

컬럼 타입을 보고 `DATE '2026-01-01'` / `TIMESTAMP '2026-01-31 18:30:00'` 로 조립하십시오.
ANSI 리터럴이라 세 드라이버가 같은 뜻으로 받습니다. 그래서 `Compiler` 는 컬럼 이름만이 아니라
**타입도 들고 있어야 합니다.**

### 시각을 포함한 컬럼에 날짜만 주면 하루를 온전히 포함시킨다

Oracle 의 `DATE` 와 PostgreSQL 의 `timestamp` 는 시·분·초를 포함합니다. `d <= '2026-01-31'`
은 31일 **00:00:00** 까지만 걸려서 그날 낮에 들어온 행이 조용히 빠집니다. `>=`/`<=` 를 둔
이유(기간의 끝이 빠지는 것을 막으려고)가 여기서 무너집니다.

| 연산자 | 보정 | 이유 |
|---|---|---|
| `less_or_equal` | `< 다음날` | 그날 낮 데이터가 빠짐 |
| `greater_than` | `>= 다음날` | 그날 낮 데이터가 들어옴 |
| `greater_or_equal`, `less_than` | 그대로 | 이미 맞음 |
| `equals`, `not_equals`, `in`, `not_in` | **거부** | 자정인 행만 걸려 거의 항상 0건 |

보정하지 않고 화면 경고로 끝내지 마십시오. 경고는 지표를 **등록하는 사람**이 보는데, 그 지표로
질문하는 것은 몇 주 뒤의 **다른 사람**입니다 — 1-7 절과 같은 이유로, 보증은 사람의 기억이 아니라
컴파일러에 있어야 합니다. 이 앱은 SQL 을 실행하지 않고 보여주므로, 바뀐 결과(`< DATE '2026-02-01'`)
가 화면에 그대로 드러나 감춰지지 않습니다.

`equals` 를 `>= AND <` 로 자동 확장하지는 마십시오. 필터 하나가 조건 두 개로 늘어나 화면의 SQL
이 정의와 눈에 띄게 달라집니다. 1-4 절대로 무엇을 쓰면 되는지 말해주고 거부하는 편이 낫습니다.

**지표 등록 시점에도 같은 규칙으로 검증하십시오**(6 절) — 형식(`YYYY-MM-DD`), 달력 유효성
(`2026-02-31` 거부), 그리고 위 연산자 제약까지.

    def _literal(self, value) -> str:
        """값을 SQL 리터럴로. 식별자와 달리 값은 화이트리스트로 막을 수 없다."""
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float)):
            return str(value)
        if value is None:
            return "NULL"
        if isinstance(value, (list, tuple, dict)):
            raise ValueError(f"값 하나를 받는 자리에 목록이 왔습니다 -> {value!r}")

        text = str(value)
        if "\x00" in text:
            raise ValueError("값에 NUL 문자를 넣을 수 없습니다")
        # MySQL/MariaDB는 기본 sql_mode에 NO_BACKSLASH_ESCAPES가 없어서 문자열
        # 안의 백슬래시가 이스케이프 문자다. 홑따옴표 겹치기만으로 막으면
        # `\' OR 1=1 -- ` 같은 값이 리터럴을 빠져나간다. PostgreSQL은
        # standard_conforming_strings가 기본 on이라 평범한 문자이므로,
        # 제약은 그것이 필요한 드라이버에만 건다.
        if self.quote == "`" and "\\" in text:
            raise ValueError("값에 백슬래시를 넣을 수 없습니다 "
                              "(MySQL에서 백슬래시는 이스케이프 문자입니다)")
        return "'" + text.replace("'", "''") + "'"
```

`Compiler`에 넘기는 `contract`는 **프루닝 전 전체 계약서**입니다. 모델에게 보여주는
후보(프롬프트 예산 절약용)와 실제로 허용되는 것(정확성 보장용)은 분리돼 있어야 합니다.

---

## 10. `tool.py` — compile_sql 도구

```python
import json
import logging
from langchain_core.tools import tool

log = logging.getLogger("nl2sql.compile")


def make_compile_tool(compiler: "Compiler", question: str):
    """compiler와 question을 클로저로 갖는 도구를 만든다.

    question을 받는 이유는 로그 한 줄에 "어떤 질문의 몇 번째 시도가 무슨
    결과였는지"를 같이 남기기 위해서다. 모델이 보는 도구 스키마에는 question이
    없다 — 모델은 ast만 채운다.
    """
    attempt = {"n": 0}

    @tool("compile_sql")
    def compile_sql(ast: str) -> str:
        """제안한 조회 명세(JSON AST)를 검증하고 SQL로 컴파일합니다.

        ast는 아래 키만 담은 JSON 객체를 문자열로 인코딩한 것입니다:
        target_table 또는 metric, aggregations, filters, group_by.

        성공하면 "SQL: "로 시작하는 문자열을 돌려줍니다 — 그것이 최종 답이니
        이 도구를 더 부르지 말고 답변을 마치십시오.
        실패하면 "error: "로 시작하는 문자열을 돌려줍니다 — 그 메시지가 무엇을
        고쳐야 하는지 말해주고 있으니, 읽고 고쳐서 다시 이 도구를 부르십시오.
        """
        attempt["n"] += 1

        payload = ast.strip()
        # 모델이 코드펜스를 붙여 보내는 일이 흔하고, 그것 때문에 실패시킬
        # 이유가 없다.
        if payload.startswith("```"):
            payload = payload.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as error:
            detail = f"ast가 올바른 JSON이 아닙니다 -> {error}"
            log.info("attempt=%d outcome=error question=%r detail=%s",
                      attempt["n"], question, detail)
            return f"error: {detail}"

        if not isinstance(parsed, dict):
            detail = f"ast는 JSON 객체여야 합니다 (받은 것: {type(parsed).__name__})"
            log.info("attempt=%d outcome=error question=%r detail=%s",
                      attempt["n"], question, detail)
            return f"error: {detail}"

        try:
            sql = compiler.compile(parsed)
        except (ValueError, KeyError, TypeError) as error:
            log.info("attempt=%d outcome=error question=%r ast=%s detail=%s",
                      attempt["n"], question, payload, error)
            return f"error: {error}"

        log.info("attempt=%d outcome=success question=%r sql=%s",
                  attempt["n"], question, sql)
        return f"SQL: {sql}"

    return compile_sql
```

`ast`를 구조화된 객체가 아니라 **문자열**로 받는 것이 의도입니다. 프레임워크의 자동 JSON
스키마 바인딩에 맡기면 코드펜스나 형식 오류를 이 함수 안에서 직접 다룰 수 없습니다.

**성공이든 실패든 매 시도를 로그 한 줄로 남기십시오.** 이 로그만 보고 "이 질문이 몇 번
만에 됐는지, 왜 처음엔 실패했는지"를 재구성할 수 있어야 합니다.

---

## 11. `graph.py` — 에이전트 루프

```python
from typing import Annotated, TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langchain_core.messages import ToolMessage

MAX_ATTEMPTS = 4   # 이 질문 하나에 대해 컴파일을 몇 번까지 다시 시도할지


class State(TypedDict):
    messages: Annotated[list, add_messages]


def build_graph(model, compile_tool):
    bound = model.bind_tools([compile_tool])

    def agent(state: State) -> dict:
        return {"messages": [bound.invoke(state["messages"])]}

    def route_after_agent(state: State) -> str:
        last = state["messages"][-1]
        return "compile" if getattr(last, "tool_calls", None) else END

    def route_after_tool(state: State) -> str:
        last = state["messages"][-1]
        # 성공은 모델의 판단을 기다리지 않고 여기서 바로 끝낸다. 도구가 이미
        # 유효한 SQL을 돌려줬는데 모델에게 한 턴을 더 줄 이유가 없다 — 그
        # 왕복이 곧 응답 지연이다.
        if isinstance(last, ToolMessage) and str(last.content).startswith("SQL:"):
            return END
        attempts = sum(1 for m in state["messages"] if isinstance(m, ToolMessage))
        return END if attempts >= MAX_ATTEMPTS else "agent"

    graph = StateGraph(State)
    graph.add_node("agent", agent)
    graph.add_node("compile", ToolNode([compile_tool]))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_after_agent, {"compile": "compile", END: END})
    graph.add_conditional_edges("compile", route_after_tool, {"agent": "agent", END: END})

    # 체크포인터를 달지 않는다. 다음 질문은 완전히 새 invoke() 호출이라
    # 이전 메시지를 볼 방법 자체가 없어야 한다.
    return graph.compile()
```

---

## 12. `app.py` — 질문 엔드포인트

```python
class AskRequest(BaseModel):
    datasource_id: UUID
    question: str


class AskResponse(BaseModel):
    sql: str | None
    error: str | None
    attempts: int


@app.post("/api/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    started = time.monotonic()

    # 매 요청마다 메타데이터 DB에서 새로 읽는다 — 프로세스에 캐시하지 않는다.
    contract = load_contract(req.datasource_id)
    pruned = Pruner(contract).prune(req.question, top_k=3)
    compiler = Compiler(contract)
    compile_tool = make_compile_tool(compiler, req.question)

    model = ChatOpenAI(
        base_url=os.environ["LLM_BASE_URL"],
        api_key=os.environ.get("LLM_API_KEY", "not-needed"),
        model=os.environ["LLM_MODEL"],
        temperature=0.0,
    )
    graph = build_graph(model, compile_tool)

    # 이 invoke() 호출 하나가 루프의 전부다. 이전 요청의 메시지는 여기 어디에도
    # 없다 — 애초에 참조할 방법이 없다.
    result = graph.invoke(
        {"messages": [SystemMessage(_system_prompt(contract, pruned)),
                      HumanMessage(req.question)]},
        {"recursion_limit": 2 * MAX_ATTEMPTS + 3},
    )

    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    last = tool_messages[-1] if tool_messages else None
    attempts = len(tool_messages)
    elapsed_ms = (time.monotonic() - started) * 1000

    if last is not None and str(last.content).startswith("SQL:"):
        sql = str(last.content)[len("SQL:"):].strip()
        log_request.info("datasource=%s question=%r attempts=%d outcome=success elapsed_ms=%.0f",
                          contract["name"], req.question, attempts, elapsed_ms)
        return AskResponse(sql=sql, error=None, attempts=attempts)

    detail = str(last.content) if last is not None else "모델이 조회를 시도하지 않았습니다"
    log_request.info("datasource=%s question=%r attempts=%d outcome=fail elapsed_ms=%.0f detail=%s",
                      contract["name"], req.question, attempts, elapsed_ms, detail)
    return AskResponse(sql=None, error=detail, attempts=attempts)


def _system_prompt(contract: dict, pruned: dict) -> str:
    return (
        f"당신은 자연어 질문을 '{contract['name']}' 데이터베이스"
        f"({contract['driver']}) 조회로 바꾸는 시맨틱 파서입니다.\n"
        "아래 [허용된 메타데이터]에 없는 테이블·컬럼·지표 이름은 절대 만들어내지 "
        "마십시오.\n\n"
        f"[허용된 메타데이터]\n{json.dumps(pruned, ensure_ascii=False, indent=2)}\n\n"
        "질문을 분석해 compile_sql 도구를 ast 인자로 부르십시오. 지표를 쓸 때는 "
        "target_table과 aggregations 대신 metric 키에 지표 이름만 적으면 됩니다 "
        "— 그 지표의 집계식과 고정 필터는 다시 적지 않아도 자동으로 적용됩니다.\n"
        "도구가 \"SQL:\"로 시작하는 결과를 돌려주면 그것으로 답이 끝난 것입니다.\n"
        "도구가 \"error:\"로 시작하는 결과를 돌려주면 그 메시지를 읽고 고쳐서 "
        "다시 부르십시오."
    )
```

요청/응답은 한 번의 HTTP 왕복입니다 — 실행 없이 컴파일까지만 하므로 스트리밍이 필요
없습니다.

---

## 13. `logging_config.py`

```python
import logging
import logging.handlers
import os
import sys


def configure_logging() -> None:
    os.makedirs("logs", exist_ok=True)
    root = logging.getLogger("nl2sql")
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # 콘솔 로그는 서버를 재시작하면 사라진다. 어떤 질문이 몇 번 시도 끝에
    # 됐는지 나중에 다시 들여다보려면 파일에도 남아야 한다.
    file_handler = logging.handlers.RotatingFileHandler(
        "logs/nl2sql.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
```

`app.py` 맨 위에서 한 번 호출하십시오.

---

## 14. React 프론트엔드

### 데이터소스 화면 (`DatasourcePage.tsx`)

목록 + 등록/편집 폼 + 행마다 버튼들.

- **등록 폼**: 드라이버 선택에 따라 필드가 달라집니다. MySQL은 스키마 칸을 감추고
  (스키마 = 데이터베이스라 같은 질문을 두 번 하는 셈), Oracle은 "데이터베이스" 라벨을
  **"서비스 이름"** 으로 바꾸십시오 (SID가 아닙니다). 포트는 비우면 드라이버 기본값이
  들어간다고 안내하십시오.
- **편집 시 비밀번호 칸**: 항상 빈 값으로 시작하고, "비워두면 저장된 비밀번호를 그대로
  씁니다"라고 안내하십시오. 서버가 비밀번호를 내려주지 않으므로 폼이 되돌려 보낼 값이
  없습니다.
- **행 표시**: 동기화 전이면 `"스키마 없음 — 조회 불가"`를 경고 색으로. 동기화 후에는
  `"테이블 N개 · 지표 M개 · 3분 전"`.
- **"스키마 읽기" 버튼**: 누르면 그 행만 잠그고 `"대상 DB에 접속 중..."`. 성공하면
  `"테이블 N개, 컬럼 M개를 읽었습니다"`를 그 행 아래에 표시.
- **삭제**: 한 번 더 확인받으십시오 (스키마와 지표가 함께 사라집니다).
- **지표 관리**: 행을 펼치면 그 데이터소스의 지표 목록과 추가 폼. 깨진 지표에는
  `"스키마와 맞지 않음"` 표시.

폼을 편집 대상에 따라 리마운트하십시오(React의 `key`에 데이터소스 id를 주면 됩니다).
그러지 않으면 A를 편집하다 B를 눌렀을 때 A의 값이 그대로 남습니다.

### 질문 화면 (`AskPage.tsx`)

**이전 질문·답변을 지우지 않고 아래에 쌓습니다.** 단, 서버로는 매번 이번 질문 하나만
보냅니다.

```tsx
interface Entry {
  question: string;
  sql: string | null;
  error: string | null;
  attempts: number;
}

export function AskPage() {
  const [datasourceId, setDatasourceId] = useState<string>("");
  const [question, setQuestion] = useState("");
  const [history, setHistory] = useState<Entry[]>([]);
  const [loading, setLoading] = useState(false);

  async function submit() {
    const asked = question;
    setLoading(true);
    try {
      const res = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // 서버로는 이번 질문 하나만 나간다. history 배열은 여기 없다 —
        // 화면에 쌓아 보여주는 것과 서버에 보내는 것은 다른 문제다.
        body: JSON.stringify({ datasource_id: datasourceId, question: asked }),
      });
      const data: Omit<Entry, "question"> = await res.json();
      setHistory((prev) => [...prev, { question: asked, ...data }]);
      setQuestion("");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div>
      <select value={datasourceId} onChange={(e) => setDatasourceId(e.target.value)}>
        {/* 동기화되지 않은 데이터소스는 비활성화하고 이유를 보여줄 것 */}
      </select>
      <textarea value={question} onChange={(e) => setQuestion(e.target.value)} />
      <button onClick={submit} disabled={loading || !datasourceId}>
        {loading ? "생성 중..." : "질문"}
      </button>

      <ul>
        {history.map((entry, i) => (
          <li key={i}>
            <p className="question">Q. {entry.question}</p>
            {entry.sql
              ? <pre>{entry.sql}</pre>
              : <p className="error">{entry.error} ({entry.attempts}번 시도)</p>}
          </li>
        ))}
      </ul>
    </div>
  );
}
```

`history`는 컴포넌트의 로컬 상태일 뿐입니다 — 새로고침하면 사라지고, 서버는 이 배열의
존재 자체를 모릅니다.

**데이터소스 선택 목록에서 동기화되지 않은 것은 고를 수 없게** 하고, 이유를 보여주십시오
(`"스키마 없음 — 먼저 스키마 읽기를 실행하세요"`).

---

## 15. 반드시 통과해야 하는 케이스

**등록**
1. 이름에 이미 있는 값(대소문자만 다른 것 포함) → 409, 화면에 이유가 뜬다.
2. 포트를 비우면 드라이버 기본값이 저장된다.
3. `GET /api/datasources` 응답 어디에도 `password`가 없다.
4. 편집에서 비밀번호를 비우고 이름만 바꿔 저장해도 동기화가 계속 된다.

**동기화**
5. 접속 실패 → 502, 드라이버 메시지 첫 줄이 화면에 보인다 (500이 아니다).
6. 성공하면 `table_count`와 `synced_at`이 갱신된다.
7. 대상 DB에서 테이블을 지우고 다시 동기화하면 그 테이블이 저장된 스키마에서도 사라진다.
8. 읽은 테이블이 0개여도 에러가 아니라 경고로 표시된다.
9. 동기화해도 등록해둔 지표는 남아 있다.
10. PostgreSQL에서 테이블·컬럼 코멘트가 실제로 저장된다.
11. MySQL 컬럼 타입이 `varchar(36)` 형태로 저장된다 (`varchar`가 아니라).
12. Oracle에서 소문자 스키마 이름을 줘도 테이블이 읽힌다.
13. 뷰가 세 드라이버 모두에서 포함된다.

**지표**
14. 존재하지 않는 컬럼으로 지표를 등록하려 하면 400으로 거부된다.
15. 지표가 가리키는 컬럼이 동기화로 사라지면, 그 지표는 모델에게 전달되지 않고 경고
    로그가 남는다.

**프루닝**
16. `"매출액의"`로 질문해도 계약서의 `"매출액"`과 겹치는 테이블이 잡힌다.
17. 지표가 걸리면 그 지표의 테이블도 함께 실린다.

**컴파일러**
18. `{"metric": "active_revenue", "filters": [{"field": "country_code", "operator": "equals", "value": "KR"}]}`
    → SQL에 고정 필터와 `country_code = 'KR'`이 **둘 다** 있다.
19. 모델이 고정 필터를 언급하지 않아도 18번 결과는 동일하다.
20. `{"having": {...}}` → "HAVING을 만들지 않습니다" + 쓸 수 있는 키 목록.
21. `group_by: ["country_code"]` → SELECT 절 맨 앞에 그 컬럼이 들어간다.
22. 컬럼명이 예약어(`order`)여도 인용되어 정상 조립된다.
23. MySQL 데이터소스에서 값에 백슬래시 → 거부. PostgreSQL에서 같은 값 → 통과.
24. 별칭에 인용 부호나 65자 → 거부.

**에이전트 루프**
25. 컴파일러가 거부하면 모델이 에러를 읽고 고쳐 다음 시도에서 성공한다.
26. `MAX_ATTEMPTS`를 넘겨도 실패하면 `sql: null`과 **마지막** 에러가 응답에 담긴다.
27. 첫 시도에서 성공하면 도구가 딱 한 번만 불린다.

**요청 간 격리**
28. 질문 A 다음에 "그거 말고 국가만 바꿔줘" 같은 후속 질문을 보내면, 서버는 A를 전혀
    모른 채 답하려 한다 — **이건 의도된 동작이다.**
29. 동기화를 다시 실행하면 서버 재시작 없이 바로 다음 질문부터 반영된다.

**로그**
30. `logs/nl2sql.log`에 시도마다 attempt 번호·outcome·detail(또는 sql)이 남는다.
31. 질문별로 데이터소스 이름·총 시도 횟수·최종 결과·소요 시간이 남는다.

---

## 16. 하지 말 것

- ❌ 등록과 동기화를 한 엔드포인트로 합치기.
- ❌ 동기화를 병합(upsert)으로 구현하기 — 통째 교체여야 한다.
- ❌ 동기화의 삭제와 삽입을 서로 다른 트랜잭션으로 나누기.
- ❌ 컬럼마다 `INSERT`를 따로 실행하기 — `executemany`를 쓸 것.
- ❌ 응답 모델에 `password` 넣기.
- ❌ 이름 중복을 DB의 `UNIQUE` 제약에만 맡기기.
- ❌ 계약서를 프로세스 메모리에 캐시하기.
- ❌ 모델에게 SQL 문자열을 받는 인자를 만들기.
- ❌ AST의 모르는 키를 조용히 무시하기.
- ❌ 지표의 고정 필터를 LLM 프롬프트 안내만으로 신뢰하기 — 컴파일러가 강제해야 한다.
- ❌ `MemorySaver`나 `thread_id`를 붙여 요청 간에 대화를 이어가기.
- ❌ 프론트에서 이전 질문·답변을 다음 요청 body에 실어 보내기.
- ❌ `route_after_tool`에서 모델이 스스로 멈추길 기다리기.
- ❌ `compile_sql` 도구 안에서 컴파일 실패를 예외로 다시 올리기.
- ❌ 컴파일된 SQL을 대상 DB에 실행해보기 — 이 범위에 없다.
- ❌ 로그에 시도 기록 없이 최종 결과만 찍기.
