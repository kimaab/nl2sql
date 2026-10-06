import logging
from uuid import UUID

import db
from models import ApiException
from compiler import build_scope, compile_formula, resolve_ref

log = logging.getLogger("nl2sql.contract")


def load_tables(datasource_id: UUID) -> dict:
    """저장된 스키마를 {테이블명: {name, description, columns}} 로. 컬럼에는 동의어·코드 사전이 붙는다."""
    rows = db.query("""
        SELECT t.name AS table_name, t.description AS table_note,
               c.name AS column_name, c.data_type, c.description AS column_note
          FROM datasource_table t
          LEFT JOIN datasource_column c ON c.table_id = t.id
         WHERE t.datasource_id = %s
         ORDER BY t.name, c.ordinal
    """, str(datasource_id))

    annotations = {
        (a["table_name"], a["column_name"]): a
        for a in db.query(
            "SELECT table_name, column_name, synonyms, codes FROM column_annotation WHERE datasource_id = %s",
            str(datasource_id),
        )
    }

    tables = {}
    for r in rows:
        table = tables.setdefault(r["table_name"], {
            "name": r["table_name"],
            "description": r["table_note"] or "",
            "columns": []
        })
        if r["column_name"] is not None:
            column = {
                "name": r["column_name"],
                "type": r["data_type"] or "",
                "description": r["column_note"] or ""
            }
            note = annotations.get((r["table_name"], r["column_name"]))
            if note:
                if note["synonyms"]:
                    column["synonyms"] = note["synonyms"]
                if note["codes"]:
                    column["codes"] = note["codes"]
            table["columns"].append(column)
    return tables


def load_relations(datasource_id: UUID, tables: dict) -> list[dict]:
    """두 테이블이 모두 스키마에 남아 있는 관계만 돌려준다."""
    out = []
    for r in db.query("""
        SELECT constraint_name, left_table, left_column, right_table, right_column
          FROM datasource_relation WHERE datasource_id = %s
         ORDER BY constraint_name, left_table, left_column
    """, str(datasource_id)):
        left = tables.get(r["left_table"])
        right = tables.get(r["right_table"])
        if not left or not right:
            continue
        if r["left_column"] not in {c["name"] for c in left["columns"]}:
            continue
        if r["right_column"] not in {c["name"] for c in right["columns"]}:
            continue
        out.append({
            "constraint": r["constraint_name"] or f"{r['left_table']}->{r['right_table']}",
            "left_table": r["left_table"], "left_column": r["left_column"],
            "right_table": r["right_table"], "right_column": r["right_column"],
        })
    return out


def load_contract(datasource_id: UUID) -> dict:
    """메타데이터 DB에서 계약서(스키마) 로드"""
    row = db.one(
        "SELECT id, name, driver FROM datasource WHERE id = %s",
        str(datasource_id)
    )
    if row is None:
        raise ApiException(404, "데이터소스를 찾을 수 없습니다")

    tables = load_tables(datasource_id)
    if not tables:
        raise ApiException(
            400,
            f"'{row['name']}'의 스키마가 비어 있습니다. "
            "데이터소스 화면에서 스키마 읽기를 먼저 실행하십시오"
        )

    metric_rows = db.query(
        "SELECT * FROM datasource_metric WHERE datasource_id = %s ORDER BY name",
        str(datasource_id)
    )
    broken = broken_reasons(metric_rows, tables)
    metrics = []
    for m in metric_rows:
        # 모델에게 보여주면 컴파일 단계에서 반드시 실패하는 AST를 유도한다.
        if m["name"] in broken:
            log.warning("깨진 지표를 제외합니다: %s — %s", m["name"], broken[m["name"]])
            continue
        metrics.append(metric_entry(m))

    return {
        "name": row["name"],
        "driver": row["driver"],
        "tables": list(tables.values()),
        "relations": load_relations(datasource_id, tables),
        "metrics": metrics
    }


def metric_entry(m: dict) -> dict:
    """메타데이터 DB의 지표 행을 계약서 항목으로"""
    entry = {
        "name": m["name"],
        "description": m["description"],
        "table": m["table_name"],
        "kind": m["kind"],
        "fixed_filters": m["fixed_filters"] or [],
    }
    if m.get("joins"):
        entry["joins"] = m["joins"]
    if m["kind"] == "projection":
        entry["columns"] = m["select_columns"] or []
    elif m["kind"] == "derived":
        entry["expression"] = m["expression"]
    else:
        entry["aggregation"] = {
            "field": m["agg_field"],
            "function": m["agg_function"],
        }
        if m.get("series"):
            entry["series"] = m["series"]
    if m.get("examples"):
        entry["examples"] = m["examples"]
    return entry


def broken_reasons(metric_rows: list[dict], tables: dict) -> dict:
    """{지표명: 깨진 이유}. 파생 지표는 구성 지표가 깨지면 함께 깨진다."""
    columns = {name: [c["name"] for c in t["columns"]] for name, t in tables.items()}
    by_name = {m["name"]: m for m in metric_rows}
    out = {}
    for m in metric_rows:
        if m["kind"] != "derived":
            reason = _broken_reason(m, columns)
            if reason:
                out[m["name"]] = reason
    for m in metric_rows:
        if m["kind"] == "derived":
            reason = _broken_reason(m, columns) or _broken_components(m, by_name, out, columns)
            if reason:
                out[m["name"]] = reason
    return out


def _broken_components(m: dict, by_name: dict, broken: dict, columns: dict) -> str | None:
    problems = []

    def check(name):
        comp = by_name.get(name)
        if comp is None:
            problems.append(f"구성 지표 {name}이(가) 없습니다")
        elif comp["kind"] != "aggregate":
            problems.append(f"구성 지표 {name}은(는) 집계형이 아닙니다")
        elif name in broken:
            problems.append(f"구성 지표 {name}이(가) 깨졌습니다 ({broken[name]})")
        else:
            try:
                scope, _ = build_scope(m["table_name"], m.get("joins") or [], columns)
            except ValueError:
                return "x"
            if comp["table_name"] not in scope:
                problems.append(f"구성 지표 {name}의 테이블 {comp['table_name']}이(가) 이 지표의 범위에 없습니다")
        return "x"

    try:
        compile_formula(m["expression"], check)
    except ValueError as e:
        return str(e)
    return "; ".join(problems) or None


def _broken_reason(m: dict, columns: dict) -> str | None:
    """동기화로 테이블·컬럼이 사라진 지표를 가려낸다.

    집계형은 agg_field 하나만 보면 되지만, 조회형은 컬럼 목록 전부가
    살아 있어야 한다. 고정 필터 컬럼은 어느 쪽이든 확인한다 — 필터가
    깨지면 지표가 보장하려던 조건이 조용히 빠진다. 조인 지표는 조인한
    테이블과 ON 컬럼까지 살아 있어야 한다.
    """
    if m["table_name"] not in columns:
        return f"테이블 {m['table_name']}이(가) 없습니다"
    try:
        scope, _ = build_scope(m["table_name"], m.get("joins") or [], columns)
    except ValueError as e:
        return str(e)
    base = next(iter(scope))

    def missing(refs) -> list[str]:
        out = []
        for ref in refs:
            try:
                resolve_ref(str(ref), scope, base)
            except ValueError:
                out.append(str(ref))
        return out

    if m["kind"] == "projection":
        if not (m["select_columns"] or []):
            return "조회 컬럼이 비어 있습니다"
        missing_columns = missing(m["select_columns"])
        if missing_columns:
            return f"조회 컬럼이 없습니다: {', '.join(missing_columns)}"
    elif m["kind"] == "aggregate" and m["agg_field"] != "*" and missing([m["agg_field"]]):
        return f"집계 컬럼 {m['agg_field']}이(가) 없습니다"
    if m["kind"] == "aggregate" and m.get("series"):
        series = m["series"]
        gone = missing([series.get("partition_by"), series.get("order_by")])
        if gone:
            return f"구분·순서 컬럼이 없습니다: {', '.join(gone)}"

    missing_filters = missing(f.get("field") for f in (m["fixed_filters"] or []))
    if missing_filters:
        return f"고정 필터 컬럼이 없습니다: {', '.join(missing_filters)}"
    return None


def broken_metric_list(datasource_id: UUID) -> list[dict]:
    """동기화 직후 알림용: [{name, reason}]"""
    tables = load_tables(datasource_id)
    rows = db.query("SELECT * FROM datasource_metric WHERE datasource_id = %s", str(datasource_id))
    return [{"name": k, "reason": v} for k, v in sorted(broken_reasons(rows, tables).items())]
