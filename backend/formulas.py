"""시스템 계산식 등록. 계산식은 테이블에 매이지 않는다 — 컬럼 이름으로 쓰이고, 지표가 허용해야 질문에 쓰인다."""

import json
import logging
from uuid import UUID, uuid4

import db
from compiler import (
    MEASURE_AGGREGATES,
    MEASURE_ARITHMETIC,
    MEASURE_ROW_SERIES,
    MEASURE_SCALARS,
    SERIES_FUNCTIONS,
    expand_formula,
    formula_refs,
    measure_has_aggregate,
    measure_text,
)
from contract import formula_entry, load_formula_rows
from formula_sql import parse_formula_sql
from metrics import refresh_statuses
from models import ApiException, Formula, FormulaInput, FormulaParseResponse

log = logging.getLogger("nl2sql.formulas")

_FUNCTIONS = (
    set(MEASURE_AGGREGATES)
    | set(MEASURE_SCALARS)
    | set(MEASURE_ARITHMETIC)
    | set(MEASURE_ROW_SERIES)
    | set(SERIES_FUNCTIONS)
)


def list_formulas(system_id: UUID) -> list[Formula]:
    rows = load_formula_rows(system_id)
    users = _users(system_id)
    return [_to_formula(r, users.get(r["id"], [])) for r in rows]


def get_formula(system_id: UUID, formula_id: UUID) -> Formula:
    row = _get_row(system_id, formula_id)
    return _to_formula(row, _users(system_id).get(row["id"], []))


def create_formula(system_id: UUID, data: FormulaInput) -> Formula:
    _guard_duplicate_name(system_id, data.name)
    expr = _validate(system_id, data)
    formula_id = uuid4()
    _execute(
        """
        INSERT INTO formula (id, system_id, name, description, column_name, mode, expr, source_sql, rollup)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """,
        str(formula_id),
        str(system_id),
        *_values(data, expr),
    )
    refresh_statuses(system_id)
    log.info("formula created: %s (%s)", formula_id, data.name)
    return get_formula(system_id, formula_id)


def update_formula(system_id: UUID, formula_id: UUID, data: FormulaInput) -> Formula:
    current = _get_row(system_id, formula_id)
    _guard_duplicate_name(system_id, data.name, except_id=formula_id)
    if current["name"].lower() != data.name.lower():
        _guard_not_referenced(system_id, current["name"], "이름을 바꿀")
    expr = _validate(system_id, data, replacing=current["name"])
    _execute(
        """
        UPDATE formula SET name = %s, description = %s, column_name = %s, mode = %s, expr = %s,
               source_sql = %s, rollup = %s, updated_at = now()
         WHERE id = %s AND system_id = %s
    """,
        *_values(data, expr),
        str(formula_id),
        str(system_id),
    )
    refresh_statuses(system_id)
    log.info("formula updated: %s (%s)", formula_id, data.name)
    return get_formula(system_id, formula_id)


def delete_formula(system_id: UUID, formula_id: UUID) -> None:
    row = _get_row(system_id, formula_id)
    _guard_not_referenced(system_id, row["name"], "지울")
    users = _users(system_id).get(row["id"], [])
    if users:
        raise ApiException(
            400,
            f"지표 {', '.join(users)}이(가) 계산식 '{row['name']}'을(를) 허용하고 있어 지울 수 없습니다. "
            "지표에서 먼저 빼십시오",
        )
    _execute(
        "DELETE FROM formula WHERE id = %s AND system_id = %s",
        str(formula_id),
        str(system_id),
    )
    log.info("formula deleted: %s", formula_id)


def parse_sql(system_id: UUID, sql: str) -> FormulaParseResponse:
    driver = _driver(system_id)
    try:
        return FormulaParseResponse(**parse_formula_sql(sql, driver))
    except ValueError as error:
        raise ApiException(400, str(error)) from None


# ── 검사 ─────────────────────────────────────────────────────────────────


def _validate(system_id: UUID, data: FormulaInput, replacing: str | None = None) -> dict:
    """식 트리를 정한다 (expr 이 없으면 SQL 을 푼다). 함수·모양·참조를 확인한다."""
    expr = data.expr
    if expr is None:
        try:
            parsed = parse_formula_sql(data.source_sql, _driver(system_id))
        except ValueError as error:
            raise ApiException(400, str(error)) from None
        if len(parsed["formulas"]) != 1:
            names = ", ".join(f["name"] or f["text"] for f in parsed["formulas"])
            raise ApiException(
                400, f"SQL 에 계산식 후보가 여럿입니다 ({names}) — 하나를 골라 식으로 등록하십시오"
            )
        expr = parsed["formulas"][0]["expr"]
    try:
        _check_tree(expr)
    except ValueError as error:
        raise ApiException(400, str(error)) from None

    formulas = {
        f["name"]: formula_entry(f)
        for f in load_formula_rows(system_id)
        if f["name"].lower() != (replacing or "").lower()
    }
    formulas[data.name] = {"name": data.name, "mode": data.mode.value, "expr": expr}
    try:
        expanded = expand_formula({"formula": data.name}, formulas)
    except ValueError as error:
        raise ApiException(400, str(error)) from None
    if data.rollup and not measure_has_aggregate(expanded):
        raise ApiException(400, "합치는 방법(rollup)은 집계 계산식에만 씁니다")
    return expr


def _check_tree(node, depth: int = 0) -> None:
    if depth > 12:
        raise ValueError("식이 너무 깊습니다")
    if not isinstance(node, dict):
        raise ValueError(f"식의 항목은 객체여야 합니다 -> {node!r}")
    if "formula" in node:
        if not str(node["formula"] or "").strip():
            raise ValueError("계산식 참조의 이름이 비어 있습니다")
        return
    if "col" in node:
        if not str(node["col"] or "").strip():
            raise ValueError("컬럼 이름이 비어 있습니다")
        return
    if "num" in node:
        if not isinstance(node["num"], (int, float)):
            raise ValueError(f"숫자가 아닙니다 -> {node['num']!r}")
        return
    fn = str(node.get("fn") or "").upper()
    if fn not in _FUNCTIONS:
        raise ValueError(f"계산식에 쓸 수 없는 함수입니다 -> {fn or node!r}")
    for child in node.get("args") or []:
        _check_tree(child, depth + 1)


def _guard_duplicate_name(system_id: UUID, name: str, except_id: UUID | None = None) -> None:
    if db.one(
        "SELECT id FROM formula WHERE system_id = %s AND lower(name) = lower(%s) AND id <> %s",
        str(system_id),
        name,
        str(except_id or uuid4()),
    ):
        raise ApiException(409, f"'{name}' 이름의 계산식이 이미 있습니다")


def _guard_not_referenced(system_id: UUID, name: str, what: str) -> None:
    """다른 계산식이 이름으로 가리키는 계산식을 지우거나 이름을 바꾸면 그 계산식이 깨진다."""
    users = [
        f["name"]
        for f in load_formula_rows(system_id)
        if f["name"].lower() != name.lower()
        and any(r.lower() == name.lower() for r in formula_refs(f["expr"]))
    ]
    if users:
        raise ApiException(
            400,
            f"계산식 {', '.join(users)}이(가) '{name}'을(를) 쓰고 있어 {what} 수 없습니다",
        )


def _users(system_id: UUID) -> dict:
    out: dict = {}
    for r in db.query(
        """
        SELECT mf.formula_id, m.name FROM metric_formula mf JOIN metric m ON m.id = mf.metric_id
         WHERE m.system_id = %s ORDER BY m.name
    """,
        str(system_id),
    ):
        out.setdefault(r["formula_id"], []).append(r["name"])
    return out


def _execute(sql: str, *args) -> None:
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)


def _driver(system_id: UUID) -> str:
    row = db.one("SELECT driver FROM meta_system WHERE id = %s", str(system_id))
    if row is None:
        raise ApiException(404, "시스템을 찾을 수 없습니다")
    return row["driver"]


def _get_row(system_id: UUID, formula_id: UUID) -> dict:
    row = db.one(
        "SELECT * FROM formula WHERE id = %s AND system_id = %s",
        str(formula_id),
        str(system_id),
    )
    if row is None:
        raise ApiException(404, "계산식을 찾을 수 없습니다")
    return row


def _values(data: FormulaInput, expr: dict) -> tuple:
    return (
        data.name,
        data.description,
        data.column_name,
        data.mode.value,
        json.dumps(expr, ensure_ascii=False),
        data.source_sql,
        data.rollup,
    )


def _to_formula(row: dict, used_by: list[str]) -> Formula:
    return Formula(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        column_name=row.get("column_name"),
        mode=row["mode"],
        expr=row["expr"],
        text=measure_text(row["expr"]),
        level="aggregate" if measure_has_aggregate(row["expr"]) else "row",
        source_sql=row.get("source_sql"),
        rollup=row.get("rollup"),
        refs=formula_refs(row["expr"]),
        used_by=used_by,
        updated_at=row["updated_at"].isoformat() if row.get("updated_at") else None,
    )
