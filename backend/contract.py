import logging
from uuid import UUID
import db
from models import ApiException

log = logging.getLogger("nl2sql.contract")


def load_contract(datasource_id: UUID) -> dict:
    """메타데이터 DB에서 계약서(스키마) 로드"""
    row = db.one(
        "SELECT id, name, driver FROM datasource WHERE id = %s",
        str(datasource_id)
    )
    if row is None:
        raise ApiException(404, "데이터소스를 찾을 수 없습니다")

    rows = db.query("""
        SELECT t.name AS table_name, t.description AS table_note,
               c.name AS column_name, c.data_type, c.description AS column_note
          FROM datasource_table t
          LEFT JOIN datasource_column c ON c.table_id = t.id
         WHERE t.datasource_id = %s
         ORDER BY t.name, c.ordinal
    """, str(datasource_id))

    tables = {}
    for r in rows:
        table = tables.setdefault(r["table_name"], {
            "name": r["table_name"],
            "description": r["table_note"] or "",
            "columns": []
        })
        if r["column_name"] is not None:
            table["columns"].append({
                "name": r["column_name"],
                "type": r["data_type"] or "",
                "description": r["column_note"] or ""
            })

    if not tables:
        raise ApiException(
            400,
            f"'{row['name']}'의 스키마가 비어 있습니다. "
            "데이터소스 화면에서 스키마 읽기를 먼저 실행하십시오"
        )

    metrics = []
    for m in db.query(
        "SELECT * FROM datasource_metric WHERE datasource_id = %s",
        str(datasource_id)
    ):
        table = tables.get(m["table_name"])
        if table is None or (
            m["agg_field"] != "*" and
            m["agg_field"] not in [c["name"] for c in table["columns"]]
        ):
            log.warning(
                "깨진 지표를 제외합니다: %s (%s.%s)",
                m["name"],
                m["table_name"],
                m["agg_field"]
            )
            continue
        metrics.append({
            "name": m["name"],
            "description": m["description"],
            "table": m["table_name"],
            "aggregation": {
                "field": m["agg_field"],
                "function": m["agg_function"]
            },
            "fixed_filters": m["fixed_filters"] or [],
        })

    return {
        "name": row["name"],
        "driver": row["driver"],
        "tables": list(tables.values()),
        "metrics": metrics
    }
