-- 의미 검색(임베딩)용 스키마. EMBED_MODEL 이 설정됐을 때만 실행된다 —
-- pgvector 가 없는 PostgreSQL 에서도 나머지 기능은 그대로 뜨도록 schema.sql 과 나눴다.
CREATE EXTENSION IF NOT EXISTS vector;

-- 지표 하나에 벡터가 여러 개: 이름+설명 1개(definition), 예시 질문마다 1개(example).
-- 차원을 고정하지 않는다 — 모델을 바꾸면 model 이 다른 행이 새로 쌓이고 검색은 현재 model 만 본다.
CREATE TABLE IF NOT EXISTS datasource_metric_embedding (
    id           BIGSERIAL PRIMARY KEY,
    metric_id    UUID        NOT NULL REFERENCES datasource_metric (id) ON DELETE CASCADE,
    model        TEXT        NOT NULL,
    kind         TEXT        NOT NULL,  -- definition | example
    source_text  TEXT        NOT NULL,
    content_hash TEXT        NOT NULL,  -- kind + source_text 의 해시. 바뀐 지표만 다시 계산한다
    embedding    vector      NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_metric_embedding_metric ON datasource_metric_embedding (metric_id, model);
