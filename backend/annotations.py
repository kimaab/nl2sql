import json
import logging
from uuid import UUID

import db
from models import Annotation, AnnotationInput, ApiException

log = logging.getLogger("nl2sql.annotations")


def list_annotations(system_id: UUID) -> list[Annotation]:
    """동의어나 코드 사전이 있는 컬럼만"""
    rows = db.query("""
        SELECT t.name AS table_name, c.name AS column_name, c.synonyms, c.codes
          FROM meta_column c JOIN meta_table t ON t.id = c.table_id
         WHERE t.system_id = %s AND t.deleted_at IS NULL AND c.deleted_at IS NULL
           AND (c.synonyms <> '[]'::jsonb OR c.codes <> '[]'::jsonb)
         ORDER BY t.name, c.name
    """, str(system_id))
    return [Annotation(**r) for r in rows]


def put_annotation(system_id: UUID, table: str, column: str, data: AnnotationInput) -> Annotation:
    """컬럼의 동의어·코드 사전을 통째로 바꾼다. 둘 다 비우면 사전이 없어진다."""
    row = db.one("""
        SELECT c.id, t.name AS table_name, c.name AS column_name
          FROM meta_column c JOIN meta_table t ON t.id = c.table_id
         WHERE t.system_id = %s AND lower(t.name) = lower(%s) AND lower(c.name) = lower(%s)
           AND t.deleted_at IS NULL AND c.deleted_at IS NULL
    """, str(system_id), table, column)
    if row is None:
        exists = db.one("SELECT name FROM meta_table WHERE system_id = %s AND lower(name) = lower(%s) AND deleted_at IS NULL",
                        str(system_id), table)
        if exists is None:
            raise ApiException(400, f"테이블을 찾을 수 없습니다: {table}")
        raise ApiException(400, f"컬럼을 찾을 수 없습니다: {exists['name']}.{column}")

    with db.connection() as conn:
        conn.execute("UPDATE meta_column SET synonyms = %s, codes = %s, updated_at = now() WHERE id = %s", (
            json.dumps(data.synonyms, ensure_ascii=False),
            json.dumps([c.model_dump() for c in data.codes], ensure_ascii=False),
            row["id"],
        ))
    log.info("annotation saved: %s.%s (synonyms=%d, codes=%d)",
             row["table_name"], row["column_name"], len(data.synonyms), len(data.codes))
    return Annotation(table_name=row["table_name"], column_name=row["column_name"],
                      synonyms=data.synonyms, codes=data.codes)
