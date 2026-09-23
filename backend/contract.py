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
        broken = _broken_reason(m, tables)
        # 모델에게 보여주면 컴파일 단계에서 반드시 실패하는 AST를 유도한다.
        if broken:
            log.warning("깨진 지표를 제외합니다: %s — %s", m["name"], broken)
            continue

        entry = {
            "name": m["name"],
            "description": m["description"],
            "table": m["table_name"],
            "kind": m["kind"],
            "fixed_filters": m["fixed_filters"] or [],
        }
        if m["kind"] == "projection":
            entry["columns"] = m["select_columns"] or []
        else:
            entry["aggregation"] = {
                "field": m["agg_field"],
                "function": m["agg_function"],
            }
        metrics.append(entry)

    return {
        "name": row["name"],
        "driver": row["driver"],
        "tables": list(tables.values()),
        "metrics": metrics
    }


def _broken_reason(m: dict, tables: dict) -> str | None:
    """동기화로 테이블·컬럼이 사라진 지표를 가려낸다.

    집계형은 agg_field 하나만 보면 되지만, 조회형은 컬럼 목록 전부가
    살아 있어야 한다. 고정 필터 컬럼은 어느 쪽이든 확인한다 — 필터가
    깨지면 지표가 보장하려던 조건이 조용히 빠진다.
    """
    table = tables.get(m["table_name"])
    if table is None:
        return f"테이블 {m['table_name']}이(가) 없습니다"
    known = {c["name"] for c in table["columns"]}

    if m["kind"] == "projection":
        missing = [c for c in (m["select_columns"] or []) if c not in known]
        if not (m["select_columns"] or []):
            return "조회 컬럼이 비어 있습니다"
        if missing:
            return f"조회 컬럼이 없습니다: {', '.join(missing)}"
    elif m["agg_field"] != "*" and m["agg_field"] not in known:
        return f"집계 컬럼 {m['table_name']}.{m['agg_field']}이(가) 없습니다"

    missing_filters = [
        f.get("field") for f in (m["fixed_filters"] or []) if f.get("field") not in known
    ]
    if missing_filters:
        return f"고정 필터 컬럼이 없습니다: {', '.join(map(str, missing_filters))}"
    return None
