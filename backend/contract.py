import logging
from uuid import UUID

import db
from models import ApiException
from compiler import (
    build_scope,
    by_column,
    expand_formula,
    locate_columns,
    measure_columns,
    resolve_ref,
)

log = logging.getLogger("nl2sql.contract")


def load_tables(system_id: UUID) -> dict:
    """살아 있는 스키마를 {테이블명: {id, name, description, purpose, columns}} 로.

    삭제 표시된 테이블·컬럼은 빠진다.
    """
    rows = db.query(
        """
        SELECT t.id AS table_id, t.name AS table_name, t.comment AS table_note, t.purpose,
               c.name AS column_name, c.data_type, c.comment AS column_note, c.is_pk,
               c.utc_offset_hours
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
        if r["left_column"] not in {c["name"] for c in left["columns"]}:
            continue
        if r["right_column"] not in {c["name"] for c in right["columns"]}:
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


def load_metric_rows(system_id: UUID) -> list[dict]:
    """지표 행 + 기본 테이블 이름 + 허용 계산식 이름 (formulas)."""
    rows = db.query(
        """
        SELECT m.*, t.name AS table_name
          FROM metric m JOIN meta_table t ON t.id = m.base_table_id
         WHERE m.system_id = %s ORDER BY m.name
    """,
        str(system_id),
    )
    links: dict = {}
    for r in db.query(
        """
        SELECT mf.metric_id, f.name FROM metric_formula mf
          JOIN formula f ON f.id = mf.formula_id
         WHERE f.system_id = %s ORDER BY f.name
    """,
        str(system_id),
    ):
        links.setdefault(r["metric_id"], []).append(r["name"])
    for r in rows:
        r["formulas"] = links.get(r["id"], [])
    return rows


def load_formula_rows(system_id: UUID) -> list[dict]:
    return db.query(
        "SELECT * FROM formula WHERE system_id = %s ORDER BY name", str(system_id)
    )


def formula_entry(f: dict) -> dict:
    """계약서의 계산식 항목"""
    entry = {"name": f["name"], "mode": f["mode"], "expr": f["expr"]}
    for key in ("description", "column_name", "rollup"):
        if f.get(key):
            entry[key] = f[key]
    return entry


def load_contract(system_id: UUID, active_only: bool = False) -> dict:
    """메타데이터 DB에서 계약서 로드.

    active_only=True 는 질문 처리용 — active 지표만 싣는다.
    False 는 컴파일·지표 검사용 — draft 도 싣는다. broken·retired 는 어느 쪽에도 없다.
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

    statuses = ("active",) if active_only else ("active", "draft")
    metrics = [
        metric_entry(m)
        for m in load_metric_rows(system_id)
        if m["status"] in statuses and m.get("dimensions") is not None
    ]
    return {
        "name": row["name"],
        "code": row["code"],
        "domain": row["domain_desc"],
        "driver": row["driver"],
        "tables": list(tables.values()),
        "relations": load_relations(system_id, tables),
        "metrics": metrics,
        "formulas": [formula_entry(f) for f in load_formula_rows(system_id)],
    }


def metric_tables(m: dict) -> list[tuple[str, str]]:
    """지표가 쓰는 테이블 [(이름, role)]. 기본 테이블 → base, 조인 → join.

    metric_table 의 정의(origin=definition) 행을 만드는 규칙이다.
    """
    out = [(m["table_name"], "base")]
    for j in m.get("joins") or []:
        if j.get("table"):
            out.append((j["table"], "join"))
    seen, unique = set(), []
    for name, role in out:
        if name.lower() not in seen:
            seen.add(name.lower())
            unique.append((name, role))
    return unique


def metric_entry(m: dict) -> dict:
    """메타데이터 DB의 지표 행을 계약서 항목으로"""
    entry = {
        "name": m["name"],
        "description": m["description"],
        "table": m["table_name"],
        "dimensions": m.get("dimensions") or [],
        "formulas": m.get("formulas") or [],
        "fixed_filters": m["fixed_filters"] or [],
        "tables": [name for name, _ in metric_tables(m)],
    }
    if m.get("joins"):
        entry["joins"] = m["joins"]
    if m.get("synonyms"):
        entry["synonyms"] = m["synonyms"]
    return entry


OLD_FORMAT = "예전 형식 지표입니다 — 테이블·디멘젼·계산식으로 다시 등록하십시오"


def broken_reasons(metric_rows: list[dict], tables: dict, formulas: list[dict]) -> dict:
    """{지표명: 깨진 이유}. 동기화로 테이블·컬럼이 사라졌거나 허용 계산식의 컬럼이 범위에 없으면 깨진다."""
    columns = {name: [c["name"] for c in t["columns"]] for name, t in tables.items()}
    by_name = {f["name"]: f for f in formulas}
    out = {}
    for m in metric_rows:
        reason = (
            OLD_FORMAT
            if m.get("dimensions") is None
            else _broken_reason(m, columns, by_name)
        )
        if reason:
            out[m["name"]] = reason
    return out


def _broken_reason(m: dict, columns: dict, formulas: dict) -> str | None:
    """테이블·조인·디멘젼·고정 필터·허용 계산식의 컬럼이 모두 살아 있어야 한다.

    고정 필터가 깨지면 지표가 보장하려던 조건이 조용히 빠진다.
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
        return list(dict.fromkeys(out))

    gone = missing(by_column(d) for d in m.get("dimensions") or [])
    if gone:
        return f"디멘젼 컬럼이 없습니다: {', '.join(gone)}"
    for name in m.get("formulas") or []:
        try:
            expr = expand_formula({"formula": name}, formulas)
        except ValueError as e:
            return str(e)
        gone = missing(measure_columns(locate_columns(expr, scope, base)))
        if gone:
            return f"계산식 {name} 의 컬럼이 이 지표의 테이블에 없습니다: {', '.join(gone)}"
    gone = missing(f.get("field") for f in (m["fixed_filters"] or []))
    if gone:
        return f"고정 필터 컬럼이 없습니다: {', '.join(gone)}"
    return None
