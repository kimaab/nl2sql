import json
import logging
from uuid import UUID, uuid4

import db
from compiler import (
    FILTER_SOURCES,
    Compiler,
    build_scope,
    by_column,
    expand_formula,
    is_numeric_type,
    measure_has_aggregate,
    parse_temporal,
    resolve_ref,
    resolve_relative,
    temporal_kind,
)
from contract import (
    broken_reasons,
    formula_entry,
    load_contract,
    load_formula_rows,
    load_metric_rows,
    load_tables,
    metric_entry,
    metric_tables,
)
from models import ApiException, Metric, MetricHistoryEntry, MetricInput

log = logging.getLogger("nl2sql.metrics")

_SELECT = """
    SELECT m.*, t.name AS table_name,
           COALESCE((SELECT jsonb_agg(f.name ORDER BY f.name) FROM metric_formula mf
                       JOIN formula f ON f.id = mf.formula_id WHERE mf.metric_id = m.id), '[]') AS formulas
      FROM metric m JOIN meta_table t ON t.id = m.base_table_id
"""


def list_metrics(system_id: UUID) -> list[Metric]:
    rows = db.query(
        _SELECT + " WHERE m.system_id = %s ORDER BY m.created_at DESC", str(system_id)
    )
    return [_row_to_metric(r) for r in rows]


def get_metric(system_id: UUID, metric_id: UUID) -> Metric:
    return _row_to_metric(_get_row(system_id, metric_id))


def _get_row(system_id: UUID, metric_id: UUID) -> dict:
    row = db.one(
        _SELECT + " WHERE m.id = %s AND m.system_id = %s",
        str(metric_id),
        str(system_id),
    )
    if row is None:
        raise ApiException(404, "지표를 찾을 수 없습니다")
    return row


def create_metric(
    system_id: UUID, data: MetricInput, actor: str | None = None
) -> Metric:
    """지표 생성. 가리키는 테이블·컬럼·계산식이 실제로 있는지 여기서 확인한다."""
    _guard_duplicate_name(system_id, data.name)
    table_name, formula_ids = _validate(system_id, data)
    tables = load_tables(system_id)

    metric_id = uuid4()
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO metric (id, system_id, name, description, base_table_id, joins, dimensions,
                                    fixed_filters, synonyms)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
                (
                    str(metric_id),
                    str(system_id),
                    *_values(data, tables[table_name]["id"]),
                ),
            )
            _save_formulas(cur, metric_id, formula_ids)
            _record_history(cur, system_id, metric_id, 1, "create", actor)
    _after_save(system_id, metric_id)
    log.info("metric created: %s (%s)", metric_id, data.name)
    return get_metric(system_id, metric_id)


def update_metric(
    system_id: UUID, metric_id: UUID, data: MetricInput, actor: str | None = None
) -> Metric:
    """지표 수정. 버전을 올리고 이전 정의는 이력에 남는다."""
    current = _get_row(system_id, metric_id)
    _guard_duplicate_name(system_id, data.name, except_id=metric_id)
    table_name, formula_ids = _validate(system_id, data)
    tables = load_tables(system_id)

    version = current["version"] + 1
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE metric SET name = %s, description = %s, base_table_id = %s, joins = %s, dimensions = %s,
                       fixed_filters = %s, synonyms = %s, version = %s, updated_at = now()
                 WHERE id = %s AND system_id = %s
            """,
                (
                    *_values(data, tables[table_name]["id"]),
                    version,
                    str(metric_id),
                    str(system_id),
                ),
            )
            _save_formulas(cur, metric_id, formula_ids)
            _record_history(cur, system_id, metric_id, version, "update", actor)
    _after_save(system_id, metric_id)
    log.info("metric updated: %s (%s, v%d)", metric_id, data.name, version)
    return get_metric(system_id, metric_id)


def delete_metric(system_id: UUID, metric_id: UUID, actor: str | None = None) -> None:
    """지표 삭제. 마지막 정의는 이력에 남는다."""
    row = db.one(
        "SELECT name, version FROM metric WHERE id = %s AND system_id = %s",
        str(metric_id),
        str(system_id),
    )
    if row is None:
        return
    with db.connection() as conn:
        with conn.cursor() as cur:
            _record_history(cur, system_id, metric_id, row["version"], "delete", actor)
            cur.execute(
                "DELETE FROM metric WHERE id = %s AND system_id = %s",
                (str(metric_id), str(system_id)),
            )
    log.info("metric deleted: %s", metric_id)


def list_metric_history(system_id: UUID, metric_id: UUID) -> list[MetricHistoryEntry]:
    rows = db.query(
        """
        SELECT version, action, snapshot, created_at FROM metric_history
         WHERE system_id = %s AND metric_id = %s ORDER BY id DESC
    """,
        str(system_id),
        str(metric_id),
    )
    if not rows:
        raise ApiException(404, "지표 이력을 찾을 수 없습니다")
    return [
        MetricHistoryEntry(
            version=r["version"],
            action=r["action"],
            snapshot=r["snapshot"],
            created_at=r["created_at"].isoformat(),
        )
        for r in rows
    ]


# ── 저장 뒤처리: 테이블 연결 · 상태 ─────────────────────────────────────────


def _after_save(system_id: UUID, metric_id: UUID) -> None:
    rebuild_metric_tables(system_id, metric_id)
    refresh_statuses(system_id)


def rebuild_metric_tables(system_id: UUID, metric_id: UUID) -> None:
    """정의에서 metric_table(origin=definition) 을 다시 만든다. 사람이 더한 manual 행은 남긴다."""
    target = next(
        (r for r in load_metric_rows(system_id) if str(r["id"]) == str(metric_id)), None
    )
    if target is None:
        return
    tables = load_tables(system_id)
    links = [
        (tables[n]["id"], role) for n, role in metric_tables(target) if n in tables
    ]
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM metric_table WHERE metric_id = %s AND origin = 'definition'",
                (str(metric_id),),
            )
            cur.executemany(
                """
                INSERT INTO metric_table (metric_id, table_id, role, origin) VALUES (%s, %s, %s, 'definition')
                ON CONFLICT (metric_id, table_id) DO NOTHING
            """,
                [(str(metric_id), tid, role) for tid, role in links],
            )


def refresh_statuses(system_id: UUID) -> list[dict]:
    """지표 상태를 다시 매긴다: 깨졌으면 broken, 아니면 active.

    retired 는 사람이 정한 것이라 건드리지 않는다. 깨진 지표 [{name, reason}] 를 돌려준다.
    """
    rows = [r for r in load_metric_rows(system_id) if r["status"] != "retired"]
    formulas = [formula_entry(f) for f in load_formula_rows(system_id)]
    broken = broken_reasons(rows, load_tables(system_id), formulas)
    updates = []
    for r in rows:
        if r["name"] in broken:
            status, reason = "broken", broken[r["name"]]
        else:
            status, reason = "active", None
        if status != r["status"] or reason != r["broken_reason"]:
            updates.append((status, reason, str(r["id"])))
    if updates:
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "UPDATE metric SET status = %s, broken_reason = %s WHERE id = %s",
                    updates,
                )
    return [{"name": k, "reason": v} for k, v in sorted(broken.items())]


def _record_history(
    cur, system_id: UUID, metric_id: UUID, version: int, action: str, actor: str | None
) -> None:
    cur.execute(
        """
        INSERT INTO metric_history (system_id, metric_id, version, action, actor, snapshot)
        SELECT m.system_id, m.id, %s, %s, %s,
               jsonb_build_object('name', m.name, 'description', m.description, 'table_name', t.name,
                                  'joins', m.joins, 'dimensions', m.dimensions, 'fixed_filters', m.fixed_filters,
                                  'synonyms', m.synonyms,
                                  'formulas', COALESCE((SELECT jsonb_agg(f.name ORDER BY f.name) FROM metric_formula mf
                                                         JOIN formula f ON f.id = mf.formula_id
                                                        WHERE mf.metric_id = m.id), '[]'))
          FROM metric m JOIN meta_table t ON t.id = m.base_table_id
         WHERE m.id = %s AND m.system_id = %s
    """,
        (version, action, actor, str(metric_id), str(system_id)),
    )


def _values(data: MetricInput, base_table_id: str) -> tuple:
    return (
        data.name,
        data.description,
        base_table_id,
        json.dumps([j.model_dump() for j in data.joins]),
        json.dumps(data.dimensions, ensure_ascii=False),
        json.dumps(data.fixed_filters, ensure_ascii=False),
        json.dumps(data.synonyms, ensure_ascii=False),
    )


def _save_formulas(cur, metric_id: UUID, formula_ids: list[str]) -> None:
    cur.execute("DELETE FROM metric_formula WHERE metric_id = %s", (str(metric_id),))
    cur.executemany(
        "INSERT INTO metric_formula (metric_id, formula_id) VALUES (%s, %s)",
        [(str(metric_id), fid) for fid in formula_ids],
    )


def _guard_duplicate_name(
    system_id: UUID, name: str, except_id: UUID | None = None
) -> None:
    existing = db.one(
        "SELECT id FROM metric WHERE system_id = %s AND lower(name) = lower(%s) AND id <> %s",
        str(system_id),
        name,
        str(except_id or uuid4()),
    )
    if existing:
        raise ApiException(409, f"'{name}' 이름의 지표가 이미 있습니다")


def _validate(system_id: UUID, data: MetricInput) -> tuple[str, list[str]]:
    """정의를 저장된 스키마와 대조한다 → (정규화된 기본 테이블 이름, 허용 계산식 id 목록)."""
    tables = load_tables(system_id)
    if not tables:
        raise ApiException(400, "스키마가 비어 있습니다. 동기화를 먼저 실행하십시오")
    columns = {name: [c["name"] for c in t["columns"]] for name, t in tables.items()}
    types = {
        (t["name"], c["name"]): c["type"] for t in tables.values() for c in t["columns"]
    }
    driver = db.one("SELECT driver FROM meta_system WHERE id = %s", str(system_id))[
        "driver"
    ]

    try:
        scope, _ = build_scope(
            data.table_name, [j.model_dump() for j in data.joins], columns
        )
    except ValueError as error:
        raise ApiException(
            400,
            str(error).replace(
                "조회할 수 없는 테이블입니다", "테이블을 찾을 수 없습니다"
            ),
        ) from None
    base = next(iter(scope))

    def require(ref, what: str) -> tuple[str, str]:
        try:
            return resolve_ref(
                str(ref or ""), scope, base, f"{what} 컬럼을 찾을 수 없습니다"
            )
        except ValueError as error:
            raise ApiException(400, str(error)) from None

    for d in data.dimensions:
        table, column = require(by_column(d), "디멘젼")
        if isinstance(d, dict):
            kind = temporal_kind(types.get((table, column), ""), driver)
            if not kind:
                raise ApiException(
                    400, f"{table}.{column} 는 날짜 컬럼이 아니라 기간 단위로 묶을 수 없습니다"
                )
            if d["grain"] == "hour" and kind != "datetime":
                raise ApiException(
                    400, f"{table}.{column} 는 시각이 없는 날짜라 시간 단위로 묶을 수 없습니다"
                )
    grains = [d for d in data.dimensions if isinstance(d, dict)]
    if len({by_column(d).lower() for d in grains}) != len(grains):
        raise ApiException(400, "같은 날짜 컬럼을 두 단위로 묶을 수 없습니다")

    rows = {f["name"].lower(): f for f in load_formula_rows(system_id)}
    formulas = {f["name"]: formula_entry(f) for f in rows.values()}
    formula_ids = []
    for name in data.formulas:
        row = rows.get(name.lower())
        if row is None:
            raise ApiException(400, f"등록되지 않은 계산식입니다: {name}")
        try:
            expand_formula({"formula": row["name"]}, formulas)
        except ValueError as error:
            raise ApiException(400, str(error)) from None
        formula_ids.append(str(row["id"]))

    for filter_spec in data.fixed_filters:
        table, column = require(filter_spec.get("field"), "필터")
        where = f"{table}.{column}"
        source = str(filter_spec.get("source") or "literal").lower()
        if source not in FILTER_SOURCES:
            raise ApiException(
                400,
                f"알 수 없는 필터 출처입니다: {source} (사용 가능: {', '.join(FILTER_SOURCES)})",
            )
        kind = temporal_kind(types.get((table, column), ""), driver)
        operator = filter_spec.get("operator")
        if operator in ("is_null", "is_not_null"):
            continue
        if operator in ("contains", "starts_with", "ends_with") and (
            kind or is_numeric_type(types.get((table, column), ""))
        ):
            raise ApiException(
                400, f"{where} 는 문자 컬럼이 아니라 {operator!r} 를 쓸 수 없습니다"
            )

        # 값이 질문에서 오므로 여기서 검사할 값이 없다.
        if source == "question":
            continue

        if source == "relative":
            if not kind:
                raise ApiException(
                    400, f"{where} 는 날짜 컬럼이 아니라 상대 기간을 쓸 수 없습니다"
                )
            try:
                resolve_relative(filter_spec.get("value"), where)
            except ValueError as error:
                raise ApiException(400, str(error)) from None
            continue

        # source == "literal" — 날짜 값은 여기서 막는다.
        # 질문 시점에 터지면 사용자는 왜 실패하는지 모른다.
        if not kind:
            continue
        try:
            value_kind, _ = parse_temporal(filter_spec.get("value"), where)
        except ValueError as error:
            raise ApiException(400, str(error)) from None
        if (
            kind == "datetime"
            and value_kind == "date"
            and operator in ("equals", "not_equals")
        ):
            raise ApiException(
                400,
                f"{where} 는 시각을 포함하는 컬럼이라 {operator!r} 로는 하루를 고를 수 없습니다 "
                "(자정인 행만 걸립니다). greater_or_equal 과 less_or_equal 로 기간을 지정하십시오",
            )

    # 허용 계산식마다 실제로 컴파일해 본다 — 계산식의 컬럼이 이 지표의 테이블에 있는지는 여기서 드러난다
    preview_sqls(system_id, data, base)
    return base, formula_ids


def _draft_compiler(system_id: UUID, data: MetricInput, base: str) -> Compiler:
    """저장 전의 정의를 계약서에 끼운 컴파일러. 질문 없이 정의만 컴파일하므로 질문에서 값을 받는 필터는 뺀다."""
    contract = load_contract(system_id)
    draft = metric_entry(
        {
            "name": data.name,
            "description": data.description,
            "table_name": base,
            "joins": [j.model_dump() for j in data.joins],
            "dimensions": data.dimensions,
            "formulas": data.formulas,
            "fixed_filters": [
                f
                for f in data.fixed_filters
                if str(f.get("source") or "literal").lower() != "question"
            ],
        }
    )
    contract["metrics"] = [
        m for m in contract["metrics"] if m["name"].lower() != data.name.lower()
    ] + [draft]
    return Compiler(contract)


def preview_sqls(system_id: UUID, data: MetricInput, base: str) -> list[tuple[str, str]]:
    """[(제목, SQL)] — 허용 계산식마다 하나. 계산식이 없으면 건수 하나."""
    compiler = _draft_compiler(system_id, data, base)
    formulas = {f["name"].lower(): f for f in compiler.formulas.values()}
    asts = []
    for name in data.formulas:
        f = formulas.get(name.lower())
        if f is None:
            continue
        expr = expand_formula({"formula": f["name"]}, compiler.formulas)
        if measure_has_aggregate(expr):
            asts.append((f["name"], {"aggregations": [{"formula": f["name"]}]}))
        else:
            asts.append((f["name"], {"columns": [{"formula": f["name"]}]}))
    if not asts:
        asts.append(("건수", {"aggregations": [{"field": "*", "function": "COUNT", "alias": "건수"}]}))
    out = []
    for title, ast in asts:
        try:
            out.append((title, compiler.compile({"metric": data.name, **ast})))
        except (ValueError, KeyError, TypeError) as error:
            raise ApiException(400, f"계산식 {title}: {error}") from None
    return out


def preview_metric(system_id: UUID, data: MetricInput) -> str:
    """저장하지 않고 정의를 SQL 로. 질문에서 받는 필터는 값이 없어 빠진다."""
    base, _ = _validate(system_id, data)
    return "\n\n".join(
        f"-- {title}\n{sql}" for title, sql in preview_sqls(system_id, data, base)
    )


def _row_to_metric(row: dict) -> Metric:
    return Metric(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        table_name=row["table_name"],
        joins=row.get("joins") or [],
        dimensions=row.get("dimensions") or [],
        formulas=row.get("formulas") or [],
        fixed_filters=row["fixed_filters"] or [],
        synonyms=row.get("synonyms") or [],
        status=row["status"],
        broken_reason=row.get("broken_reason"),
        version=row.get("version") or 1,
        updated_at=row["updated_at"].isoformat() if row.get("updated_at") else None,
    )

