-- 메타데이터 DB 스키마 (TO-BE). docs/to-be/메타데이터DB_테이블정의서.xlsx 와 같은 내용이다.
-- 여러 번 실행해도 안전하다 (백엔드 기동 때마다 돈다).

-- 시스템 = 조회 대상 DB. 사용자는 질문할 때 시스템을 먼저 고른다.
CREATE TABLE IF NOT EXISTS meta_system (
    id           UUID PRIMARY KEY,
    code         TEXT        NOT NULL UNIQUE,
    name         TEXT        NOT NULL,
    domain_desc  TEXT        NOT NULL DEFAULT '',  -- 업무 영역 설명. 테이블 추론 프롬프트 첫머리
    driver       TEXT        NOT NULL,
    host         TEXT        NOT NULL,
    port         INTEGER     NOT NULL,
    db_name      TEXT        NOT NULL,
    db_schemas   TEXT[]      NOT NULL DEFAULT '{}', -- 읽을 스키마. 첫 스키마가 기본 스키마다 (catalog.py)
    username    TEXT        NOT NULL DEFAULT '',
    password_enc BYTEA       NOT NULL,             -- AES-256-GCM (secret.py). API 로 나가지 않는다
    synced_at    TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_meta_system_name ON meta_system (lower(name));
-- Superset 에 등록한 DB 연결 id (superset_link.py 가 채운다). 비어 있으면 아직 Superset 에 연결하지 않은 것이다.
ALTER TABLE meta_system ADD COLUMN IF NOT EXISTS superset_database_id INTEGER;
-- 스키마를 여러 개 읽는다. 예전 db_schema(하나)는 db_schemas 의 첫 스키마로 옮기고 지운다.
ALTER TABLE meta_system ADD COLUMN IF NOT EXISTS db_schemas TEXT[] NOT NULL DEFAULT '{}';
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'meta_system' AND column_name = 'db_schema') THEN
        UPDATE meta_system SET db_schemas = ARRAY[db_schema] WHERE db_schema <> '' AND db_schemas = '{}';
        ALTER TABLE meta_system DROP COLUMN db_schema;
    END IF;
END $$;

-- 테이블. 동기화는 이름 기준 병합이라 id 가 유지된다 — 지표 연결(metric_table)이 id 를 가리킨다.
CREATE TABLE IF NOT EXISTS meta_table (
    id               UUID PRIMARY KEY,
    system_id        UUID        NOT NULL REFERENCES meta_system (id) ON DELETE CASCADE,
    name             TEXT        NOT NULL,
    comment          TEXT        NOT NULL DEFAULT '',   -- 대상 DB 코멘트 (동기화가 채움)
    purpose          TEXT        NOT NULL DEFAULT '',   -- 테이블 용도 (사람)
    deleted_at       TIMESTAMPTZ,                       -- 대상 DB 에서 사라짐 (soft delete)
    synced_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (system_id, name)
);
CREATE INDEX IF NOT EXISTS idx_meta_table_live ON meta_table (system_id) WHERE deleted_at IS NULL;
-- 기본 스키마가 아닌 테이블의 스키마 (기본 스키마면 빈 값). 그 테이블의 name 은 '스키마.테이블' 이다.
ALTER TABLE meta_table ADD COLUMN IF NOT EXISTS db_schema TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS meta_column (
    id         BIGSERIAL PRIMARY KEY,
    table_id   UUID        NOT NULL REFERENCES meta_table (id) ON DELETE CASCADE,
    name       TEXT        NOT NULL,
    data_type  TEXT        NOT NULL DEFAULT '',
    comment    TEXT        NOT NULL DEFAULT '',
    ordinal    INTEGER     NOT NULL,
    is_pk      BOOLEAN     NOT NULL DEFAULT false,
    deleted_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (table_id, name)
);
CREATE INDEX IF NOT EXISTS idx_meta_column_table ON meta_column (table_id);
-- UTC 로 저장된 시각 컬럼의 현지 시차(시간, 예: KST = 9). 사람이 적고, 동기화는 건드리지 않는다.
-- 컴파일러가 날짜 묶음·표시는 현지 시각으로, 필터 값은 UTC 로 바꿔 비교한다.
ALTER TABLE meta_column ADD COLUMN IF NOT EXISTS utc_offset_hours SMALLINT;
-- 행마다 시차를 담은 같은 테이블의 숫자 컬럼 (예: 차량별 timezone). 있으면 그 행의 시차로, 비면 utc_offset_hours 로 보정한다.
ALTER TABLE meta_column ADD COLUMN IF NOT EXISTS utc_offset_column TEXT NOT NULL DEFAULT '';
-- 계산이 필요한 컬럼 (예: 누적값 — 그대로 합치면 틀린다). 사람이 표시하고, 동기화는 건드리지 않는다.
-- 이 컬럼을 계산해야 답할 수 있는 질문은 지표가 필요하다 — 테이블 선택 단계의 LLM 이 판단한다.
ALTER TABLE meta_column ADD COLUMN IF NOT EXISTS needs_calc BOOLEAN NOT NULL DEFAULT false;
-- 사람이 적은 컬럼 설명. 동기화와 자동 설명은 건드리지 않는다. 대상 DB 코멘트와 추론한 설명보다 먼저 쓴다 (contract.py).
ALTER TABLE meta_column ADD COLUMN IF NOT EXISTS note TEXT NOT NULL DEFAULT '';

-- 테이블 관계. FK 는 동기화가(source='fk'), 나머지는 사람이(source='manual') 관리한다.
CREATE TABLE IF NOT EXISTS meta_relation (
    id              UUID PRIMARY KEY,
    system_id       UUID        NOT NULL REFERENCES meta_system (id) ON DELETE CASCADE,
    left_table_id   UUID        NOT NULL REFERENCES meta_table (id) ON DELETE CASCADE,
    left_column     TEXT        NOT NULL,
    right_table_id  UUID        NOT NULL REFERENCES meta_table (id) ON DELETE CASCADE,
    right_column    TEXT        NOT NULL,
    constraint_name TEXT        NOT NULL DEFAULT '',
    source          TEXT        NOT NULL DEFAULT 'manual',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_meta_relation_system ON meta_relation (system_id);

-- 지표 = 이름 · 설명 · 같은 말 + SQL. SQL 을 조회 명세(AST)로 옮겨 저장하고, 질문마다 LLM 이 조건만 바꿔 쓴다.
CREATE TABLE IF NOT EXISTS metric (
    id            UUID PRIMARY KEY,
    system_id     UUID        NOT NULL REFERENCES meta_system (id) ON DELETE CASCADE,
    name          TEXT        NOT NULL,
    description   TEXT        NOT NULL DEFAULT '',
    synonyms      JSONB       NOT NULL DEFAULT '[]',
    source_sql    TEXT        NOT NULL,                 -- 등록한 SQL 원문
    ast           JSONB       NOT NULL,                 -- SQL 을 옮긴 조회 명세 (compiler.py)
    status        TEXT        NOT NULL DEFAULT 'active', -- active | broken (동기화 뒤 AST 가 컴파일되지 않음)
    broken_reason TEXT,
    version       INTEGER     NOT NULL DEFAULT 1,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (system_id, name)
);
CREATE INDEX IF NOT EXISTS idx_metric_active ON metric (system_id) WHERE status = 'active';

-- 지표가 쓰는 테이블. 테이블 선택 단계에서 테이블마다 지표 설명을 붙일 때 쓴다.
CREATE TABLE IF NOT EXISTS metric_table (
    metric_id UUID NOT NULL REFERENCES metric (id) ON DELETE CASCADE,
    table_id  UUID NOT NULL REFERENCES meta_table (id) ON DELETE CASCADE,
    role      TEXT NOT NULL DEFAULT 'base',  -- base | join
    PRIMARY KEY (metric_id, table_id)
);
CREATE INDEX IF NOT EXISTS idx_metric_table_table ON metric_table (table_id);

-- 지표를 바꾸거나 지울 때마다 그 시점의 정의를 남긴다. 지표가 지워져도 남는다.
CREATE TABLE IF NOT EXISTS metric_history (
    id         BIGSERIAL PRIMARY KEY,
    system_id  UUID        NOT NULL REFERENCES meta_system (id) ON DELETE CASCADE,
    metric_id  UUID        NOT NULL,
    version    INTEGER     NOT NULL,
    action     TEXT        NOT NULL,  -- create | update | delete
    snapshot   JSONB       NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_metric_history_metric ON metric_history (metric_id, version);

CREATE TABLE IF NOT EXISTS meta_sync_log (
    id             BIGSERIAL PRIMARY KEY,
    system_id      UUID        NOT NULL REFERENCES meta_system (id) ON DELETE CASCADE,
    trigger        TEXT        NOT NULL,  -- manual | auto
    status         TEXT        NOT NULL,  -- ok | error
    tables_added   INTEGER,
    tables_changed INTEGER,
    tables_removed INTEGER,
    broken_metrics JSONB       NOT NULL DEFAULT '[]',
    error          TEXT,
    started_at     TIMESTAMPTZ NOT NULL,
    finished_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_meta_sync_log_system ON meta_sync_log (system_id, started_at DESC);

-- 질문 기록. 다음 질문의 프롬프트에는 실리지 않는다 — 사람이 다시 보고 평가하기 위한 기록이다.
CREATE TABLE IF NOT EXISTS ask_log (
    id               UUID PRIMARY KEY,
    system_id        UUID        NOT NULL REFERENCES meta_system (id) ON DELETE CASCADE,
    question         TEXT        NOT NULL,
    selected_tables  JSONB       NOT NULL DEFAULT '[]',
    selected_metrics JSONB       NOT NULL DEFAULT '[]',
    ast              JSONB,
    sql              TEXT,
    error            TEXT,
    clarification    TEXT,
    attempts         INTEGER     NOT NULL DEFAULT 0,
    total_tokens     INTEGER     NOT NULL DEFAULT 0,
    elapsed_ms       INTEGER     NOT NULL DEFAULT 0,
    favorite         BOOLEAN     NOT NULL DEFAULT false,
    feedback         TEXT,        -- up | down
    feedback_note    TEXT        NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ask_log_system ON ask_log (system_id, created_at DESC);

-- 질문마다 각 단계에서 LLM 에 보낸 후보, 고른 것, 이유, 토큰
CREATE TABLE IF NOT EXISTS ask_trace (
    id                BIGSERIAL PRIMARY KEY,
    ask_id            UUID        NOT NULL REFERENCES ask_log (id) ON DELETE CASCADE,
    stage             TEXT        NOT NULL,  -- table | metric | sql
    candidates        JSONB       NOT NULL DEFAULT '[]',
    selected          JSONB       NOT NULL DEFAULT '[]',
    reason            TEXT        NOT NULL DEFAULT '',
    prompt_tokens     INTEGER     NOT NULL DEFAULT 0,
    completion_tokens INTEGER     NOT NULL DEFAULT 0,
    elapsed_ms        INTEGER     NOT NULL DEFAULT 0,
    model             TEXT        NOT NULL DEFAULT '',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ask_trace_ask ON ask_trace (ask_id, stage);

-- 지표 자동 등록 실행. 테이블마다 LLM 이 지표 후보를 쓰고, 등록 확인(AST 변환 → 컴파일 → 결과 비교)을 통과한 것만 지표로 저장한다.
CREATE TABLE IF NOT EXISTS metric_auto_run (
    id                UUID PRIMARY KEY,
    system_id         UUID        NOT NULL REFERENCES meta_system (id) ON DELETE CASCADE,
    status            TEXT        NOT NULL DEFAULT 'running',  -- running | done | error (running 인데 돌고 있지 않으면 중단)
    options           JSONB       NOT NULL DEFAULT '{}',
    tables            JSONB       NOT NULL DEFAULT '[]',       -- 후보를 쓸 테이블
    tables_done       INTEGER     NOT NULL DEFAULT 0,
    proposed          INTEGER     NOT NULL DEFAULT 0,
    registered        INTEGER     NOT NULL DEFAULT 0,
    failed            INTEGER     NOT NULL DEFAULT 0,
    skipped           INTEGER     NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER     NOT NULL DEFAULT 0,
    completion_tokens INTEGER     NOT NULL DEFAULT 0,
    error             TEXT,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_metric_auto_run_system ON metric_auto_run (system_id, started_at DESC);

-- 자동 등록 후보 하나. 등록됐든 아니든 남는다 — 왜 등록되지 않았는지(stage · reason)와 시도마다의 SQL 을 본다.
CREATE TABLE IF NOT EXISTS metric_auto_item (
    id          BIGSERIAL PRIMARY KEY,
    run_id      UUID        NOT NULL REFERENCES metric_auto_run (id) ON DELETE CASCADE,
    table_name  TEXT        NOT NULL,
    name        TEXT        NOT NULL DEFAULT '',
    description TEXT        NOT NULL DEFAULT '',
    synonyms    JSONB       NOT NULL DEFAULT '[]',
    sql         TEXT        NOT NULL DEFAULT '',
    rationale   TEXT        NOT NULL DEFAULT '',          -- LLM 이 이 지표를 고른 이유
    status      TEXT        NOT NULL,                     -- registered | failed | skipped
    stage       TEXT,                                     -- 실패 단계: llm | duplicate | parse | compile | guard | verify | save
    reason      TEXT        NOT NULL DEFAULT '',
    attempts    INTEGER     NOT NULL DEFAULT 0,
    tries       JSONB       NOT NULL DEFAULT '[]',        -- 시도마다 [{sql, stage, error}]
    check_result JSONB,                                   -- 마지막 등록 확인 결과 (MetricCheck)
    full_scans  JSONB       NOT NULL DEFAULT '[]',
    metric_id   UUID,                                     -- 등록된 지표 (지표가 지워져도 기록은 남는다)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_metric_auto_item_run ON metric_auto_item (run_id, id);

-- 등록된 지표 검증 실행. 지표마다 여러 점검을 돌려 통과 · 주의 · 실패를 매긴다.
CREATE TABLE IF NOT EXISTS metric_validation_run (
    id                UUID PRIMARY KEY,
    system_id         UUID        NOT NULL REFERENCES meta_system (id) ON DELETE CASCADE,
    status            TEXT        NOT NULL DEFAULT 'running',  -- running | done | error
    options           JSONB       NOT NULL DEFAULT '{}',
    total             INTEGER     NOT NULL DEFAULT 0,
    passed            INTEGER     NOT NULL DEFAULT 0,
    warned            INTEGER     NOT NULL DEFAULT 0,
    failed            INTEGER     NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER     NOT NULL DEFAULT 0,
    completion_tokens INTEGER     NOT NULL DEFAULT 0,
    error             TEXT,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_metric_validation_run_system ON metric_validation_run (system_id, started_at DESC);

-- 지표 하나의 검증 결과. checks 에 점검마다 {key, label, status, message, detail} 이 있다.
CREATE TABLE IF NOT EXISTS metric_validation_item (
    id             BIGSERIAL PRIMARY KEY,
    run_id         UUID        NOT NULL REFERENCES metric_validation_run (id) ON DELETE CASCADE,
    metric_id      UUID        NOT NULL,
    metric_name    TEXT        NOT NULL,
    metric_version INTEGER     NOT NULL,
    verdict        TEXT        NOT NULL,  -- pass | warn | fail
    summary        TEXT        NOT NULL DEFAULT '',
    checks         JSONB       NOT NULL DEFAULT '[]',
    elapsed_ms     INTEGER     NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_metric_validation_item_run ON metric_validation_item (run_id, id);
CREATE INDEX IF NOT EXISTS idx_metric_validation_item_metric ON metric_validation_item (metric_id, created_at DESC);

-- 자동 설명. 대상 DB 코멘트가 비었을 때 LLM 이 이름 · 타입 · 샘플 값을 보고 추론한 설명 (describe.py).
-- 동기화는 건드리지 않는다. 코멘트가 있으면 코멘트가 먼저다 (contract.py).
ALTER TABLE meta_table ADD COLUMN IF NOT EXISTS ai_comment TEXT NOT NULL DEFAULT '';
ALTER TABLE meta_column ADD COLUMN IF NOT EXISTS ai_comment TEXT NOT NULL DEFAULT '';
-- meta_relation.source 에 'auto' 가 더해진다 — 컬럼 이름 · 타입 · PK 로 추론한 관계 (describe.py). 사람이 지울 수 있다.

-- 자동 설명 · 관계 추론 실행. 화면은 이 기록을 다시 읽어 진행을 본다.
CREATE TABLE IF NOT EXISTS meta_describe_run (
    id              UUID PRIMARY KEY,
    system_id       UUID        NOT NULL REFERENCES meta_system (id) ON DELETE CASCADE,
    status          TEXT        NOT NULL DEFAULT 'running',  -- running | done | error (running 인데 돌고 있지 않으면 중단)
    options         JSONB       NOT NULL DEFAULT '{}',
    tables_total    INTEGER     NOT NULL DEFAULT 0,          -- 설명을 채울 테이블 수
    tables_done     INTEGER     NOT NULL DEFAULT 0,
    tables_filled   INTEGER     NOT NULL DEFAULT 0,          -- 테이블 설명을 채운 수
    columns_filled  INTEGER     NOT NULL DEFAULT 0,          -- 컬럼 설명을 채운 수
    relations_added INTEGER,                                 -- 추론한 관계 수 (관계를 돌리지 않았으면 NULL)
    failures        JSONB       NOT NULL DEFAULT '[]',       -- [{table, error}]
    error           TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_meta_describe_run_system ON meta_describe_run (system_id, started_at DESC);
