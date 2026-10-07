"""업무 용어 사전. 질문에 나온 용어만 테이블 추론·지표 선택 프롬프트에 싣는다."""
import json
import logging
from uuid import UUID

import psycopg

import db
from models import ApiException, GlossaryInput, GlossaryTerm

log = logging.getLogger("nl2sql.glossary")


def list_terms(system_id: UUID | None, include_global: bool = True) -> list[GlossaryTerm]:
    rows = db.query("""
        SELECT * FROM glossary
         WHERE (system_id = %s::uuid) OR (%s AND system_id IS NULL)
         ORDER BY system_id NULLS FIRST, lower(term)
    """, str(system_id) if system_id else None, include_global)
    return [GlossaryTerm(**{k: r[k] for k in ("id", "system_id", "term", "synonyms", "meaning", "maps_to", "status")})
            for r in rows]


def save_term(system_id: UUID | None, data: GlossaryInput, term_id: int | None = None) -> GlossaryTerm:
    values = (data.term, json.dumps(data.synonyms, ensure_ascii=False), data.meaning.strip(),
              json.dumps(data.maps_to, ensure_ascii=False), data.status)
    try:
        with db.connection() as conn:
            if term_id is None:
                cur = conn.execute("""
                    INSERT INTO glossary (system_id, term, synonyms, meaning, maps_to, status)
                    VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
                """, (str(system_id) if system_id else None, *values))
            else:
                cur = conn.execute("""
                    UPDATE glossary SET term = %s, synonyms = %s, meaning = %s, maps_to = %s, status = %s,
                           updated_at = now()
                     WHERE id = %s AND system_id IS NOT DISTINCT FROM %s::uuid RETURNING id
                """, (*values, term_id, str(system_id) if system_id else None))
            row = cur.fetchone()
    except psycopg.errors.UniqueViolation:
        raise ApiException(409, f"'{data.term}' 용어가 이미 있습니다") from None
    if row is None:
        raise ApiException(404, "용어를 찾을 수 없습니다")
    return next(t for t in list_terms(system_id) if t.id == row[0])


def delete_term(system_id: UUID | None, term_id: int) -> None:
    with db.connection() as conn:
        conn.execute("DELETE FROM glossary WHERE id = %s AND system_id IS NOT DISTINCT FROM %s::uuid",
                     (term_id, str(system_id) if system_id else None))


def load_for_contract(system_id: UUID) -> list[dict]:
    """계약서에 싣는 승인된 용어 (시스템 + 전사 공통)"""
    return [{"term": t.term, "synonyms": t.synonyms, "meaning": t.meaning, "maps_to": t.maps_to}
            for t in list_terms(system_id) if t.status == "approved"]
