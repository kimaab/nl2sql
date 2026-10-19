import logging
from uuid import UUID

import db
from models import ApiException

log = logging.getLogger("nl2sql.contract")


def load_tables(system_id: UUID) -> dict:
    """살아 있는 스키마를 {테이블명: {id, name, description, purpose, columns}} 로.

    삭제 표시된 테이블·컬럼은 빠진다.
    """
    rows = db.query(
        """
        SELECT t.id AS table_id, t.name AS table_name, t.comment AS table_note, t.purpose,
               c.name AS column_name, c.data_type, c.comment AS column_note, c.is_pk,
               c.utc_offset_hours, c.needs_calc
          FROM meta_table t
          LEFT JOIN meta_column c ON c.table_id = t.id AND c.deleted_at IS NULL
         WHERE t.system_id = %s AND t.deleted_at IS NULL
         ORDER BY t.name, c.ordinal
    """,
        str(system_id),
    )

    tables = {}
    for r in rows:
        table = tables.setdefault(
            r["table_name"],
            {
                "id": str(r["table_id"]),
                "name": r["table_name"],
                "description": r["table_note"] or "",
                "purpose": r["purpose"] or "",
                "columns": [],
            },
        )
        if r["column_name"] is not None:
            column = {
                "name": r["column_name"],
                "type": r["data_type"] or "",
                "description": r["column_note"] or "",
            }
            if r["is_pk"]:
                column["is_pk"] = True
            if r["utc_offset_hours"]:
                column["utc_offset"] = r["utc_offset_hours"]
            if r["needs_calc"]:
                column["needs_calc"] = True
            table["columns"].append(column)
    return tables


def load_relations(system_id: UUID, tables: dict) -> list[dict]:
    """두 테이블과 컬럼이 모두 살아 있는 관계만 돌려준다."""
    out = []
    for r in db.query(
        """
        SELECT r.constraint_name, lt.name AS left_table, r.left_column, rt.name AS right_table, r.right_column
          FROM meta_relation r
          JOIN meta_table lt ON lt.id = r.left_table_id
          JOIN meta_table rt ON rt.id = r.right_table_id
         WHERE r.system_id = %s
         ORDER BY r.constraint_name, lt.name, r.left_column
    """,
        str(system_id),
    ):
        left = tables.get(r["left_table"])
        right = tables.get(r["right_table"])
        if not left or not right:
            continue
        if not _has_column(left, r["left_column"]) or not _has_column(right, r["right_column"]):
            continue
        out.append(
            {
                "constraint": r["constraint_name"]
                or f"{r['left_table']}->{r['right_table']}",
                "left_table": r["left_table"],
                "left_column": r["left_column"],
                "right_table": r["right_table"],
                "right_column": r["right_column"],
            }
        )
    return out


def _has_column(table: dict, name: str) -> bool:
    for c in table["columns"]:
        if c["name"] == name:
            return True
    return False


def load_metric_rows(system_id: UUID) -> list[dict]:
    """지표 행 + 쓰는 테이블 이름 (tables, 기본 테이블이 먼저)"""
    return db.query(
        """
        SELECT m.*,
               COALESCE((SELECT jsonb_agg(t.name ORDER BY mt.role, t.name)
                           FROM metric_table mt JOIN meta_table t ON t.id = mt.table_id
                          WHERE mt.metric_id = m.id), '[]') AS tables
          FROM metric m
         WHERE m.system_id = %s ORDER BY m.name
    """,
        str(system_id),
    )


def metric_entry(m: dict) -> dict:
    """메타데이터 DB의 지표 행을 계약서 항목으로"""
    entry = {
        "name": m["name"],
        "description": m["description"],
        "tables": m.get("tables") or [],
        "sql": m["source_sql"],
        "ast": m["ast"],
    }
    if m.get("synonyms"):
        entry["synonyms"] = m["synonyms"]
    return entry


def load_contract(system_id: UUID, active_only: bool = False) -> dict:
    """메타데이터 DB에서 계약서 로드.

    active_only=True 는 질문 처리용 — 깨지지 않은 지표만 싣는다. False 는 깨진 지표도 싣는다.
    """
    row = db.one(
        "SELECT id, code, name, domain_desc, driver FROM meta_system WHERE id = %s",
        str(system_id),
    )
    if row is None:
        raise ApiException(404, "시스템을 찾을 수 없습니다")

    tables = load_tables(system_id)
    if not tables:
        raise ApiException(
            400,
            f"'{row['name']}'의 스키마가 비어 있습니다. 시스템 화면에서 동기화를 먼저 실행하십시오",
        )
    metrics = []
    for m in load_metric_rows(system_id):
        if not active_only or m["status"] == "active":
            metrics.append(metric_entry(m))
    return {
        "name": row["name"],
        "code": row["code"],
        "domain": row["domain_desc"],
        "driver": row["driver"],
        "tables": list(tables.values()),
        "relations": load_relations(system_id, tables),
        "metrics": metrics,
    }
