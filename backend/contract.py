import logging
from uuid import UUID

import db
from models import ApiException
from compiler import build_scope, compile_formula, measure_columns, measure_nested, resolve_ref, series_list

log = logging.getLogger("nl2sql.contract")


def load_tables(system_id: UUID) -> dict:
    """살아 있는 스키마를 {테이블명: {id, name, description, purpose, card, card_line, columns}} 로.

    컬럼에는 동의어·코드 사전이 붙는다. 삭제 표시된 테이블·컬럼은 빠진다.
    카드는 승인된 것만 싣는다 — 초안은 질문 처리에 쓰지 않는다.
    """
    rows = db.query("""
        SELECT t.id AS table_id, t.name AS table_name, t.comment AS table_note, t.purpose,
               t.card, t.card_line, t.card_status,
               c.name AS column_name, c.data_type, c.comment AS column_note, c.is_pk, c.synonyms, c.codes
          FROM meta_table t
          LEFT JOIN meta_column c ON c.table_id = t.id AND c.deleted_at IS NULL
         WHERE t.system_id = %s AND t.deleted_at IS NULL
         ORDER BY t.name, c.ordinal
    """, str(system_id))

    tables = {}
    for r in rows:
        approved = r["card_status"] == "approved"
        table = tables.setdefault(r["table_name"], {
            "id": str(r["table_id"]),
            "name": r["table_name"],
            "description": r["table_note"] or "",
            "purpose": r["purpose"] or "",
            "card": r["card"] if approved else "",
            "card_line": r["card_line"] if approved else "",
            "columns": [],
        })
        if r["column_name"] is not None:
            column = {"name": r["column_name"], "type": r["data_type"] or "", "description": r["column_note"] or ""}
            if r["is_pk"]:
                column["is_pk"] = True
            if r["synonyms"]:
                column["synonyms"] = r["synonyms"]
            if r["codes"]:
                column["codes"] = r["codes"]
            table["columns"].append(column)
    return tables


def load_relations(system_id: UUID, tables: dict) -> list[dict]:
    """두 테이블과 컬럼이 모두 살아 있는 관계만 돌려준다."""
    out = []
    for r in db.query("""
        SELECT r.constraint_name, lt.name AS left_table, r.left_column, rt.name AS right_table, r.right_column
          FROM meta_relation r
          JOIN meta_table lt ON lt.id = r.left_table_id
          JOIN meta_table rt ON rt.id = r.right_table_id
         WHERE r.system_id = %s
         ORDER BY r.constraint_name, lt.name, r.left_column
    """, str(system_id)):
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


def load_metric_rows(system_id: UUID) -> list[dict]:
    """지표 행 + 기본 테이블 이름. table_name 키는 정의 검사·전개 코드가 그대로 쓴다."""
    return db.query("""
        SELECT m.*, t.name AS table_name
          FROM metric m JOIN meta_table t ON t.id = m.base_table_id
         WHERE m.system_id = %s ORDER BY m.name
    """, str(system_id))


def load_contract(system_id: UUID, active_only: bool = False) -> dict:
    """메타데이터 DB에서 계약서 로드.

    active_only=True 는 질문 처리용 — 승인된 예시 질문이 있는 active 지표만 싣는다.
    False 는 컴파일·지표 검사용 — draft 도 싣는다. broken·retired 는 어느 쪽에도 없다.
    """
    row = db.one("SELECT id, code, name, domain_desc, driver FROM meta_system WHERE id = %s", str(system_id))
    if row is None:
        raise ApiException(404, "시스템을 찾을 수 없습니다")

    tables = load_tables(system_id)
    if not tables:
        raise ApiException(400, f"'{row['name']}'의 스키마가 비어 있습니다. 시스템 화면에서 동기화를 먼저 실행하십시오")

    statuses = ("active",) if active_only else ("active", "draft")
    metric_rows = [m for m in load_metric_rows(system_id) if m["status"] in statuses]
    links: dict = {}
    for r in db.query("""
        SELECT mt.metric_id, t.name FROM metric_table mt
          JOIN metric m ON m.id = mt.metric_id JOIN meta_table t ON t.id = mt.table_id
         WHERE m.system_id = %s AND t.deleted_at IS NULL ORDER BY mt.role, t.name
    """, str(system_id)):
        links.setdefault(r["metric_id"], []).append(r["name"])
    examples: dict = {}
    for r in db.query("""
        SELECT e.metric_id, e.question, e.ast FROM metric_example e JOIN metric m ON m.id = e.metric_id
         WHERE m.system_id = %s AND e.status = 'approved' ORDER BY e.id
    """, str(system_id)):
        examples.setdefault(r["metric_id"], []).append({"question": r["question"], "ast": r["ast"]})

    metrics = []
    for m in metric_rows:
        entry = metric_entry({**m, "examples": examples.get(m["id"], [])})
        entry["tables"] = links.get(m["id"], [m["table_name"]])
        if m.get("synonyms"):
            entry["synonyms"] = m["synonyms"]
        metrics.append(entry)

    from glossary import load_for_contract
    return {
        "name": row["name"],
        "code": row["code"],
        "domain": row["domain_desc"],
        "driver": row["driver"],
        "tables": list(tables.values()),
        "relations": load_relations(system_id, tables),
        "metrics": metrics,
        "glossary": load_for_contract(system_id),
    }


def metric_tables(m: dict, by_name: dict) -> list[tuple[str, str]]:
    """지표가 쓰는 테이블 [(이름, role)]. 기본 테이블 → base, 조인 → join, 파생은 구성 지표의 테이블 → join.

    metric_table 의 정의(origin=definition) 행을 만드는 규칙이다.
    """
    out = [(m["table_name"], "base")]
    for j in m.get("joins") or []:
        if j.get("table"):
            out.append((j["table"], "join"))
    if m.get("kind") == "derived" and m.get("expression"):
        names: list = []
        try:
            compile_formula(m["expression"], lambda n: names.append(n) or "x")
        except ValueError:
            pass
        for n in names:
            comp = by_name.get(n.lower())
            if comp:
                out.append((comp["table_name"], "join"))
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
        "kind": m["kind"],
        "fixed_filters": m["fixed_filters"] or [],
    }
    if m.get("joins"):
        entry["joins"] = m["joins"]
    if m.get("measures") and m["kind"] != "derived":
        entry["measures"] = m["measures"]
        if m.get("series"):
            entry["series"] = m["series"]
    elif m["kind"] == "projection":
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
        elif len(comp.get("measures") or []) > 1 or any(measure_nested(ms.get("expr")) for ms in comp.get("measures") or []):
            problems.append(f"구성 지표 {name}은(는) 측정값이 여럿이거나 집계 안의 집계라 수식에 쓸 수 없습니다")
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

    if m.get("measures") and m["kind"] != "derived":
        gone = missing(c for ms in m["measures"] for c in measure_columns(ms.get("expr")))
        if gone:
            return f"측정값 컬럼이 없습니다: {', '.join(dict.fromkeys(gone))}"
    elif m["kind"] == "projection":
        if not (m["select_columns"] or []):
            return "조회 컬럼이 비어 있습니다"
        missing_columns = missing(m["select_columns"])
        if missing_columns:
            return f"조회 컬럼이 없습니다: {', '.join(missing_columns)}"
    elif m["kind"] == "aggregate" and m["agg_field"] != "*" and missing([m["agg_field"]]):
        return f"집계 컬럼 {m['agg_field']}이(가) 없습니다"
    if m["kind"] != "derived" and m.get("series"):
        series = m["series"]
        gone = missing(series_list(series.get("partition_by")) + series_list(series.get("order_by")))
        if gone:
            return f"구분·순서 컬럼이 없습니다: {', '.join(gone)}"

    missing_filters = missing(f.get("field") for f in (m["fixed_filters"] or []))
    if missing_filters:
        return f"고정 필터 컬럼이 없습니다: {', '.join(missing_filters)}"
    return None


