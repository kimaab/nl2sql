import json
import logging
from uuid import UUID

import db
from contract import load_tables
from models import Annotation, AnnotationInput, ApiException

log = logging.getLogger("nl2sql.annotations")


def list_annotations(datasource_id: UUID) -> list[Annotation]:
    rows = db.query("""
        SELECT table_name, column_name, synonyms, codes FROM column_annotation
         WHERE datasource_id = %s ORDER BY table_name, column_name
    """, str(datasource_id))
    return [Annotation(**r) for r in rows]


def put_annotation(datasource_id: UUID, table: str, column: str, data: AnnotationInput) -> Annotation:
    """컬럼의 동의어·코드 사전을 통째로 바꾼다. 둘 다 비우면 지운다."""
    tables = load_tables(datasource_id)
    found = next((t for t in tables if t.lower() == table.lower()), None)
    if found is None:
        raise ApiException(400, f"테이블을 찾을 수 없습니다: {table}")
    col = next((c["name"] for c in tables[found]["columns"] if c["name"].lower() == column.lower()), None)
    if col is None:
        raise ApiException(400, f"컬럼을 찾을 수 없습니다: {found}.{column}")

    with db.connection() as conn:
        with conn.cursor() as cur:
            if not data.synonyms and not data.codes:
                cur.execute(
                    "DELETE FROM column_annotation WHERE datasource_id = %s AND table_name = %s AND column_name = %s",
                    (str(datasource_id), found, col),
                )
            else:
                cur.execute("""
                    INSERT INTO column_annotation (datasource_id, table_name, column_name, synonyms, codes)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (datasource_id, table_name, column_name)
                    DO UPDATE SET synonyms = EXCLUDED.synonyms, codes = EXCLUDED.codes, updated_at = now()
                """, (
                    str(datasource_id), found, col,
                    json.dumps(data.synonyms, ensure_ascii=False),
                    json.dumps([c.model_dump() for c in data.codes], ensure_ascii=False),
                ))
    log.info("annotation saved: %s.%s (synonyms=%d, codes=%d)", found, col, len(data.synonyms), len(data.codes))
    return Annotation(table_name=found, column_name=col, synonyms=data.synonyms, codes=data.codes)
