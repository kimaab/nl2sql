"""지표 의미 검색 — 지표 문장을 벡터로 바꿔 pgvector 에 두고, 질문 벡터와 가까운 지표를 찾는다.

EMBED_MODEL 이 비어 있으면 전부 꺼진다 (TF-IDF 만 쓴다). 모델은 앱 프로세스 안에서
sentence-transformers 로 돌린다 — `uv sync --extra embedding` 으로 설치한다.
"""
import hashlib
import logging
import os
import threading
from uuid import UUID

import db

log = logging.getLogger("nl2sql.embedding")

_model = None
_model_lock = threading.Lock()


def model_name() -> str:
    return os.environ.get("EMBED_MODEL", "").strip()


def enabled() -> bool:
    return bool(model_name())


def min_score() -> float:
    """이보다 덜 비슷한 지표는 후보로 치지 않는다. 임베딩 유사도는 0 이 거의 나오지 않는다."""
    return float(os.environ.get("EMBED_MIN_SCORE", "0.5"))


def _load():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                try:
                    from sentence_transformers import SentenceTransformer
                except ImportError:
                    raise RuntimeError(
                        "EMBED_MODEL 이 설정됐지만 sentence-transformers 가 없습니다 (uv sync --extra embedding)"
                    ) from None
                log.info("임베딩 모델 로드: %s", model_name())
                _model = SentenceTransformer(model_name(), device=os.environ.get("EMBED_DEVICE") or None)
    return _model


def encode(texts: list[str]) -> list[list[float]]:
    """정규화된 벡터 — 코사인 유사도를 그대로 쓸 수 있다."""
    vectors = _load().encode(texts, normalize_embeddings=True, convert_to_numpy=True)
    return vectors.tolist()


def _literal(vector: list[float]) -> str:
    """pgvector 입력 형식 '[0.1,0.2,...]' — 파이썬 쪽 pgvector 패키지 없이 ::vector 로 넘긴다."""
    return "[" + ",".join(f"{v:.7g}" for v in vector) + "]"


def metric_texts(row: dict) -> list[tuple[str, str]]:
    """지표 하나가 검색에 내놓는 문장들: [(kind, text)]. 예시 질문은 사용자 말투라 질문과 가장 잘 맞는다."""
    description = str(row.get("description") or "").strip()
    out = [("definition", f"{row['name']}: {description}" if description else row["name"])]
    for example in row.get("examples") or []:
        question = str(example.get("question") or "").strip()
        if question:
            out.append(("example", question))
    return out


def _hash(kind: str, text: str) -> str:
    return hashlib.sha256(f"{kind}\n{text}".encode("utf-8")).hexdigest()


def refresh(datasource_id: UUID) -> int:
    """데이터소스의 지표 벡터를 정의와 맞춘다. 바뀐 지표만 다시 계산하고, 다시 계산한 지표 수를 돌려준다."""
    model = model_name()
    rows = db.query(
        "SELECT id, name, description, examples FROM datasource_metric WHERE datasource_id = %s",
        str(datasource_id),
    )
    with db.connection() as conn:
        with conn.cursor() as cur:
            # 지표 저장과 질문이 동시에 refresh 해도 같은 지표를 두 번 넣지 않게
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"embedding:{datasource_id}",))
            cur.execute("""
                SELECT e.metric_id::text, e.content_hash
                  FROM datasource_metric_embedding e
                  JOIN datasource_metric m ON m.id = e.metric_id
                 WHERE m.datasource_id = %s AND e.model = %s
            """, (str(datasource_id), model))
            stored: dict[str, set] = {}
            for metric_id, content_hash in cur.fetchall():
                stored.setdefault(metric_id, set()).add(content_hash)

            changed = 0
            for row in rows:
                texts = metric_texts(row)
                if {_hash(k, t) for k, t in texts} == stored.get(str(row["id"]), set()):
                    continue
                vectors = encode([t for _, t in texts])
                cur.execute("DELETE FROM datasource_metric_embedding WHERE metric_id = %s", (str(row["id"]),))
                for (kind, text), vector in zip(texts, vectors):
                    cur.execute("""
                        INSERT INTO datasource_metric_embedding
                        (metric_id, model, kind, source_text, content_hash, embedding)
                        VALUES (%s, %s, %s, %s, %s, %s::vector)
                    """, (str(row["id"]), model, kind, text, _hash(kind, text), _literal(vector)))
                changed += 1
    if changed:
        log.info("지표 벡터 갱신: datasource=%s 지표 %d개", datasource_id, changed)
    return changed


def search(datasource_id: UUID, question: str) -> dict[str, float]:
    """{지표 이름: 유사도}. 지표의 문장(정의·예시 질문) 중 질문과 가장 가까운 것의 유사도를 쓴다."""
    vector = _literal(encode([question])[0])
    rows = db.query("""
        SELECT m.name, MAX(1 - (e.embedding <=> %s::vector)) AS score
          FROM datasource_metric_embedding e
          JOIN datasource_metric m ON m.id = e.metric_id
         WHERE m.datasource_id = %s AND e.model = %s
         GROUP BY m.name
    """, vector, str(datasource_id), model_name())
    return {r["name"]: float(r["score"]) for r in rows}


def semantic_scores(datasource_id: UUID, question: str) -> dict[str, float] | None:
    """질문 처리용. 모든 지표의 유사도를 돌려준다 — 기준(min_score) 거르기는 Pruner 가 하고 로그에 남긴다.

    꺼져 있거나 실패하면 None — 질문은 TF-IDF 만으로 계속 처리된다.
    """
    if not enabled():
        return None
    try:
        refresh(datasource_id)
        return search(datasource_id, question)
    except Exception:
        log.exception("의미 검색 실패 — TF-IDF 만으로 계속합니다")
        return None


def refresh_quietly(datasource_id: UUID) -> None:
    """지표 저장 직후용. 실패해도 저장은 성공이다 — 다음 질문 때 refresh 가 다시 맞춘다."""
    if not enabled():
        return
    try:
        refresh(datasource_id)
    except Exception:
        log.exception("지표 벡터 갱신 실패 — 다음 질문 때 다시 시도합니다")


def warm_up() -> None:
    """기동 직후 백그라운드: 모델을 미리 올리고 모든 데이터소스의 벡터를 맞춘다. 첫 질문이 느려지지 않게."""
    try:
        _load()
        for row in db.query("SELECT id FROM datasource"):
            refresh(row["id"])
        log.info("임베딩 준비 완료: %s", model_name())
    except Exception:
        log.exception("임베딩 준비 실패 — 질문 때 다시 시도합니다")
