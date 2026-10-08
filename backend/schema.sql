-- 메타데이터 DB 스키마 (TO-BE). docs/to-be/메타데이터DB_테이블정의서.xlsx 와 같은 내용이다.
-- 여러 번 실행해도 안전하다. AS-IS 표(datasource_*)는 건드리지 않는다 — 옮기는 것은 migrate_v2.py.

-- AS-IS ask_log 는 TO-BE 와 이름이 같고 모양이 다르다 (datasource_id). 그대로 두면 CREATE TABLE IF NOT EXISTS 가
-- 옛 표를 남겨 모든 질문 기록이 깨진다. 옛 표를 ask_log_v1 로 비켜 두고 migrate_v2.py 가 옮긴다.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = 'ask_log' AND column_name = 'datasource_id') THEN
        ALTER TABLE ask_log RENAME TO ask_log_v1;
        ALTER INDEX IF EXISTS ask_log_pkey RENAME TO ask_log_v1_pkey;
    END IF;
END $$;

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
    db_schema    TEXT        NOT NULL DEFAULT '',
    username     TEXT        NOT NULL DEFAULT '',
    password_enc BYTEA       NOT NULL,             -- AES-256-GCM (secret.py). API 로 나가지 않는다
    synced_at    TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_meta_system_name ON meta_system (lower(name));

-- 테이블. 동기화는 이름 기준 병합이라 id 가 유지된다 — 지표 연결(metric_table)이 id 를 가리킨다.
CREATE TABLE IF NOT EXISTS meta_table (
    id               UUID PRIMARY KEY,
    system_id        UUID        NOT NULL REFERENCES meta_system (id) ON DELETE CASCADE,
    name             TEXT        NOT NULL,
    comment          TEXT        NOT NULL DEFAULT '',   -- 대상 DB 코멘트 (동기화가 채움)
    purpose          TEXT        NOT NULL DEFAULT '',   -- 테이블 용도 (사람)
    card             TEXT        NOT NULL DEFAULT '',   -- LLM 요약 카드 (승인 후 사용)
    card_line        TEXT        NOT NULL DEFAULT '',   -- 테이블 추론에 보내는 한 줄 요약
    card_status      TEXT        NOT NULL DEFAULT 'none',  -- none | draft | approved
    card_source_hash TEXT,                              -- 카드를 만든 시점의 구조 해시
    deleted_at       TIMESTAMPTZ,                       -- 대상 DB 에서 사라짐 (soft delete)
    synced_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (system_id, name)
);
CREATE INDEX IF NOT EXISTS idx_meta_table_live ON meta_table (system_id) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS meta_column (
    id         BIGSERIAL PRIMARY KEY,
    table_id   UUID        NOT NULL REFERENCES meta_table (id) ON DELETE CASCADE,
    name       TEXT        NOT NULL,
    data_type  TEXT        NOT NULL DEFAULT '',
    comment    TEXT        NOT NULL DEFAULT '',
    ordinal    INTEGER     NOT NULL,
    is_pk      BOOLEAN     NOT NULL DEFAULT false,
    synonyms   JSONB       NOT NULL DEFAULT '[]',
    codes      JSONB       NOT NULL DEFAULT '[]',   -- [{"code": "03", "label": "배송완료"}]
    deleted_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (table_id, name)
);
CREATE INDEX IF NOT EXISTS idx_meta_column_table ON meta_column (table_id);

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

-- 지표. 정의는 구조화 방식(AS-IS 와 같음)이라 컴파일러가 전개한다.
CREATE TABLE IF NOT EXISTS metric (
    id             UUID PRIMARY KEY,
    system_id      UUID        NOT NULL REFERENCES meta_system (id) ON DELETE CASCADE,
    name           TEXT        NOT NULL,
    description    TEXT        NOT NULL DEFAULT '',
    kind           TEXT        NOT NULL DEFAULT 'aggregate',  -- aggregate | projection | derived
    -- 테이블은 동기화로는 지워지지 않는다 (deleted_at). 실제로 지워지는 것은 시스템을 지울 때뿐이다.
    base_table_id  UUID        NOT NULL REFERENCES meta_table (id) ON DELETE CASCADE,
    joins          JSONB       NOT NULL DEFAULT '[]',
    agg_field      TEXT,
    agg_function   TEXT,
    select_columns JSONB       NOT NULL DEFAULT '[]',
    expression     TEXT,
    series         JSONB,
    measures       JSONB       NOT NULL DEFAULT '[]',  -- 집계형·조회형의 출력 컬럼 [{name, expr(식 트리)}]
    fixed_filters  JSONB       NOT NULL DEFAULT '[]',
    synonyms       JSONB       NOT NULL DEFAULT '[]',
    status         TEXT        NOT NULL DEFAULT 'draft',  -- draft | active | broken | retired
    broken_reason  TEXT,
    version        INTEGER     NOT NULL DEFAULT 1,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (system_id, name)
);
ALTER TABLE metric ADD COLUMN IF NOT EXISTS measures JSONB NOT NULL DEFAULT '[]';
CREATE INDEX IF NOT EXISTS idx_metric_active ON metric (system_id) WHERE status = 'active';

-- 지표가 쓰는 테이블. 질의 생성의 '테이블 → 지표' 는 검색이 아니라 이 표의 조회다.
CREATE TABLE IF NOT EXISTS metric_table (
    metric_id UUID NOT NULL REFERENCES metric (id) ON DELETE CASCADE,
    table_id  UUID NOT NULL REFERENCES meta_table (id) ON DELETE CASCADE,
    role      TEXT NOT NULL DEFAULT 'base',        -- base | join
    origin    TEXT NOT NULL DEFAULT 'definition',  -- definition | manual
    PRIMARY KEY (metric_id, table_id)
);
CREATE INDEX IF NOT EXISTS idx_metric_table_table ON metric_table (table_id);

CREATE TABLE IF NOT EXISTS metric_example (
    id          BIGSERIAL PRIMARY KEY,
    metric_id   UUID        NOT NULL REFERENCES metric (id) ON DELETE CASCADE,
    question    TEXT        NOT NULL,
    ast         JSONB,
    origin      TEXT        NOT NULL,                 -- llm | human | feedback
    status      TEXT        NOT NULL DEFAULT 'draft', -- draft | approved | rejected
    reviewed_by TEXT,
    reviewed_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_metric_example_approved ON metric_example (metric_id) WHERE status = 'approved';

-- 지표를 바꾸거나 지울 때마다 그 시점의 정의를 남긴다. 지표가 지워져도 남는다.
CREATE TABLE IF NOT EXISTS metric_history (
    id         BIGSERIAL PRIMARY KEY,
    system_id  UUID        NOT NULL REFERENCES meta_system (id) ON DELETE CASCADE,
    metric_id  UUID        NOT NULL,
    version    INTEGER     NOT NULL,
    action     TEXT        NOT NULL,  -- create | update | delete | retire
    snapshot   JSONB       NOT NULL,
    actor      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_metric_history_metric ON metric_history (metric_id, version);

CREATE TABLE IF NOT EXISTS glossary (
    id         BIGSERIAL PRIMARY KEY,
    system_id  UUID REFERENCES meta_system (id) ON DELETE CASCADE,  -- NULL = 전사 공통
    term       TEXT        NOT NULL,
    synonyms   JSONB       NOT NULL DEFAULT '[]',
    meaning    TEXT        NOT NULL DEFAULT '',
    maps_to    JSONB       NOT NULL DEFAULT '[]',
    status     TEXT        NOT NULL DEFAULT 'draft',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- NULL system_id 끼리도 용어가 겹치지 않게 (UNIQUE 는 NULL 을 서로 다르게 본다)
CREATE UNIQUE INDEX IF NOT EXISTS uq_glossary_term
    ON glossary (coalesce(system_id, '00000000-0000-0000-0000-000000000000'::uuid), lower(term));

-- LLM 으로 테이블 카드·지표 예시 질문 초안을 만들 대상
CREATE TABLE IF NOT EXISTS enrich_job (
    id          BIGSERIAL PRIMARY KEY,
    target_type TEXT        NOT NULL,  -- table | metric
    target_id   UUID        NOT NULL,
    reason      TEXT        NOT NULL,  -- new | changed | rejected
    note        TEXT        NOT NULL DEFAULT '',
    status      TEXT        NOT NULL DEFAULT 'queued',  -- queued | running | done | failed
    attempts    INTEGER     NOT NULL DEFAULT 0,
    error       TEXT,
    model       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_enrich_job_queue ON enrich_job (status, created_at) WHERE status = 'queued';
-- 같은 대상이 두 번 대기하지 않게
CREATE UNIQUE INDEX IF NOT EXISTS uq_enrich_job_queued ON enrich_job (target_type, target_id) WHERE status = 'queued';

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

CREATE TABLE IF NOT EXISTS eval_case (
    id                 BIGSERIAL PRIMARY KEY,
    system_id          UUID        NOT NULL REFERENCES meta_system (id) ON DELETE CASCADE,
    question           TEXT        NOT NULL,
    expected_tables    JSONB       NOT NULL DEFAULT '[]',  -- [table_id]
    expected_metric_id UUID,                               -- NULL = '맞는 지표 없음' 이 정답
    expected_sql       TEXT,
    origin             TEXT        NOT NULL,               -- synthetic | human
    style              TEXT        NOT NULL DEFAULT '',
    status             TEXT        NOT NULL DEFAULT 'draft',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_eval_case_system ON eval_case (system_id) WHERE status = 'approved';

CREATE TABLE IF NOT EXISTS eval_run (
    id              UUID PRIMARY KEY,
    system_id       UUID        NOT NULL REFERENCES meta_system (id) ON DELETE CASCADE,
    label           TEXT        NOT NULL,
    config          JSONB       NOT NULL DEFAULT '{}',
    case_count      INTEGER     NOT NULL DEFAULT 0,
    table_recall    NUMERIC,
    metric_accuracy NUMERIC,
    sql_accuracy    NUMERIC,
    avg_tokens      INTEGER,
    avg_elapsed_ms  INTEGER,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_eval_run_system ON eval_run (system_id, started_at DESC);

CREATE TABLE IF NOT EXISTS eval_result (
    run_id             UUID    NOT NULL REFERENCES eval_run (id) ON DELETE CASCADE,
    case_id            BIGINT  NOT NULL REFERENCES eval_case (id),
    selected_tables    JSONB   NOT NULL DEFAULT '[]',
    selected_metric_id UUID,
    sql                TEXT,
    table_hit          BOOLEAN NOT NULL DEFAULT false,
    metric_hit         BOOLEAN NOT NULL DEFAULT false,
    sql_hit            BOOLEAN,
    error              TEXT,
    total_tokens       INTEGER NOT NULL DEFAULT 0,
    elapsed_ms         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, case_id)
);
