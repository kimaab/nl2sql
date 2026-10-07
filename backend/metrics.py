import json
import logging
from uuid import UUID, uuid4

import db
from compiler import (
    FILTER_SOURCES, Compiler, build_scope, compile_formula, is_numeric_type, parse_temporal, resolve_ref,
    resolve_relative, temporal_kind,
)
from contract import broken_reasons, load_contract, load_metric_rows, load_tables, metric_entry, metric_tables
from models import ApiException, Metric, MetricExample, MetricHistoryEntry, MetricInput, MetricKind

log = logging.getLogger("nl2sql.metrics")


def list_metrics(system_id: UUID) -> list[Metric]:
    rows = db.query("""
        SELECT m.*, t.name AS table_name FROM metric m JOIN meta_table t ON t.id = m.base_table_id
         WHERE m.system_id = %s ORDER BY m.created_at DESC
    """, str(system_id))
    examples = _examples([r["id"] for r in rows])
    return [_row_to_metric(r, examples.get(r["id"], {})) for r in rows]


def get_metric(system_id: UUID, metric_id: UUID) -> Metric:
    row = _get_row(system_id, metric_id)
    return _row_to_metric(row, _examples([row["id"]]).get(row["id"], {}))


def _get_row(system_id: UUID, metric_id: UUID) -> dict:
    row = db.one("""
        SELECT m.*, t.name AS table_name FROM metric m JOIN meta_table t ON t.id = m.base_table_id
         WHERE m.id = %s AND m.system_id = %s
    """, str(metric_id), str(system_id))
    if row is None:
        raise ApiException(404, "지표를 찾을 수 없습니다")
    return row


def _examples(metric_ids: list) -> dict:
    """{metric_id: {"approved": [..], "drafts": n}}"""
    out: dict = {}
    if not metric_ids:
        return out
    for r in db.query("""
        SELECT metric_id, question, ast, origin, status FROM metric_example
         WHERE metric_id = ANY(%s) AND status IN ('approved', 'draft') ORDER BY id
    """, [str(i) for i in metric_ids]):
        slot = out.setdefault(r["metric_id"], {"approved": [], "drafts": 0})
        if r["status"] == "approved":
            slot["approved"].append(MetricExample(question=r["question"], ast=r["ast"], origin=r["origin"]))
        else:
            slot["drafts"] += 1
    return out


def create_metric(system_id: UUID, data: MetricInput, actor: str | None = None) -> Metric:
    """지표 생성. 가리키는 테이블·컬럼이 저장된 스키마에 실제로 있는지 여기서 확인한다."""
    _guard_duplicate_name(system_id, data.name)
    table_name = _validate(system_id, data)
    tables = load_tables(system_id)

    metric_id = uuid4()
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO metric (id, system_id, name, description, kind, base_table_id, joins, agg_field,
                                    agg_function, select_columns, expression, series, fixed_filters, synonyms)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (str(metric_id), str(system_id), *_values(data, tables[table_name]["id"])))
            _save_examples(cur, metric_id, data.examples)
            _record_history(cur, system_id, metric_id, 1, "create", actor)
    _after_save(system_id, metric_id)
    log.info("metric created: %s (%s, kind=%s)", metric_id, data.name, data.kind.value)
    return get_metric(system_id, metric_id)


def update_metric(system_id: UUID, metric_id: UUID, data: MetricInput, actor: str | None = None) -> Metric:
    """지표 수정. 버전을 올리고 이전 정의는 이력에 남는다."""
    current = _get_row(system_id, metric_id)
    _guard_duplicate_name(system_id, data.name, except_id=metric_id)
    if current["name"].lower() != data.name.lower() or data.kind is not MetricKind.AGGREGATE:
        _guard_not_referenced(system_id, current["name"], "이름을 바꾸거나 집계형이 아니게 바꿀")
    table_name = _validate(system_id, data)
    tables = load_tables(system_id)

    version = current["version"] + 1
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE metric SET name = %s, description = %s, kind = %s, base_table_id = %s, joins = %s,
                       agg_field = %s, agg_function = %s, select_columns = %s, expression = %s, series = %s,
                       fixed_filters = %s, synonyms = %s, version = %s, updated_at = now()
                 WHERE id = %s AND system_id = %s
            """, (*_values(data, tables[table_name]["id"]), version, str(metric_id), str(system_id)))
            _save_examples(cur, metric_id, data.examples)
            _record_history(cur, system_id, metric_id, version, "update", actor)
    _after_save(system_id, metric_id)
    log.info("metric updated: %s (%s, v%d)", metric_id, data.name, version)
    return get_metric(system_id, metric_id)


def delete_metric(system_id: UUID, metric_id: UUID, actor: str | None = None) -> None:
    """지표 삭제. 마지막 정의는 이력에 남는다."""
    row = db.one("SELECT name, version FROM metric WHERE id = %s AND system_id = %s", str(metric_id), str(system_id))
    if row is None:
        return
    _guard_not_referenced(system_id, row["name"], "지울")
    with db.connection() as conn:
        with conn.cursor() as cur:
            _record_history(cur, system_id, metric_id, row["version"], "delete", actor)
            cur.execute("DELETE FROM metric WHERE id = %s AND system_id = %s", (str(metric_id), str(system_id)))
    log.info("metric deleted: %s", metric_id)


def list_metric_history(system_id: UUID, metric_id: UUID) -> list[MetricHistoryEntry]:
    rows = db.query("""
        SELECT version, action, snapshot, created_at FROM metric_history
         WHERE system_id = %s AND metric_id = %s ORDER BY id DESC
    """, str(system_id), str(metric_id))
    if not rows:
        raise ApiException(404, "지표 이력을 찾을 수 없습니다")
    return [MetricHistoryEntry(version=r["version"], action=r["action"], snapshot=r["snapshot"],
                               created_at=r["created_at"].isoformat()) for r in rows]


# ── 저장 뒤처리: 테이블 연결 · 상태 · 보강 대기열 ───────────────────────────

def _after_save(system_id: UUID, metric_id: UUID) -> None:
    rebuild_metric_tables(system_id, metric_id)
    refresh_statuses(system_id)
    with db.connection() as conn:
        conn.execute("""
            INSERT INTO enrich_job (target_type, target_id, reason) VALUES ('metric', %s, 'changed')
            ON CONFLICT (target_type, target_id) WHERE status = 'queued' DO NOTHING
        """, (str(metric_id),))


def rebuild_metric_tables(system_id: UUID, metric_id: UUID) -> None:
    """정의에서 metric_table(origin=definition) 을 다시 만든다. 사람이 더한 manual 행은 남긴다."""
    rows = load_metric_rows(system_id)
    by_name = {r["name"].lower(): r for r in rows}
    target = next((r for r in rows if str(r["id"]) == str(metric_id)), None)
    if target is None:
        return
    tables = load_tables(system_id)
    links = [(tables[n]["id"], role) for n, role in metric_tables(target, by_name) if n in tables]
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM metric_table WHERE metric_id = %s AND origin = 'definition'", (str(metric_id),))
            cur.executemany("""
                INSERT INTO metric_table (metric_id, table_id, role, origin) VALUES (%s, %s, %s, 'definition')
                ON CONFLICT (metric_id, table_id) DO NOTHING
            """, [(str(metric_id), tid, role) for tid, role in links])


def refresh_statuses(system_id: UUID) -> list[dict]:
    """지표 상태를 다시 매긴다: 깨졌으면 broken, 승인된 예시 질문이 있으면 active, 아니면 draft.

    retired 는 사람이 정한 것이라 건드리지 않는다. 깨진 지표 [{name, reason}] 를 돌려준다.
    """
    rows = [r for r in load_metric_rows(system_id) if r["status"] != "retired"]
    broken = broken_reasons(rows, load_tables(system_id))
    with_examples = {r["metric_id"] for r in db.query("""
        SELECT DISTINCT e.metric_id FROM metric_example e JOIN metric m ON m.id = e.metric_id
         WHERE m.system_id = %s AND e.status = 'approved'
    """, str(system_id))}
    updates = []
    for r in rows:
        if r["name"] in broken:
            status, reason = "broken", broken[r["name"]]
        else:
            status, reason = ("active" if r["id"] in with_examples else "draft"), None
        if status != r["status"] or reason != r["broken_reason"]:
            updates.append((status, reason, str(r["id"])))
    if updates:
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany("UPDATE metric SET status = %s, broken_reason = %s WHERE id = %s", updates)
    return [{"name": k, "reason": v} for k, v in sorted(broken.items())]


def _save_examples(cur, metric_id: UUID, examples: list[MetricExample]) -> None:
    """화면에서 온 예시 목록으로 승인된 예시를 맞춘다. 검수 대기(draft) 초안은 건드리지 않는다.

    같은 질문이 이미 있으면 출처(origin)를 지킨다 — LLM 초안을 승인한 것이 사람이 쓴 것으로 바뀌지 않게.
    """
    cur.execute("SELECT id, lower(question), origin FROM metric_example WHERE metric_id = %s AND status = 'approved'",
                (str(metric_id),))
    existing = {q: (i, origin) for i, q, origin in cur.fetchall()}
    keep = set()
    for e in examples:
        key = e.question.lower()
        if key in existing:
            keep.add(existing[key][0])
            cur.execute("UPDATE metric_example SET question = %s, ast = %s WHERE id = %s",
                        (e.question, json.dumps(e.ast, ensure_ascii=False) if e.ast else None, existing[key][0]))
        else:
            cur.execute("""
                INSERT INTO metric_example (metric_id, question, ast, origin, status, reviewed_at)
                VALUES (%s, %s, %s, %s, 'approved', now()) RETURNING id
            """, (str(metric_id), e.question, json.dumps(e.ast, ensure_ascii=False) if e.ast else None,
                  e.origin if e.origin != "llm" else "human"))
            keep.add(cur.fetchone()[0])
    gone = [i for i, _ in existing.values() if i not in keep]
    if gone:
        cur.execute("DELETE FROM metric_example WHERE id = ANY(%s)", (gone,))


def _record_history(cur, system_id: UUID, metric_id: UUID, version: int, action: str, actor: str | None) -> None:
    cur.execute("""
        INSERT INTO metric_history (system_id, metric_id, version, action, actor, snapshot)
        SELECT m.system_id, m.id, %s, %s, %s,
               jsonb_build_object('name', m.name, 'description', m.description, 'kind', m.kind,
                                  'table_name', t.name, 'joins', m.joins, 'agg_field', m.agg_field,
                                  'agg_function', m.agg_function, 'select_columns', m.select_columns,
                                  'expression', m.expression, 'fixed_filters', m.fixed_filters,
                                  'series', m.series, 'synonyms', m.synonyms,
                                  'examples', COALESCE((SELECT jsonb_agg(jsonb_build_object('question', e.question,
                                                         'ast', e.ast, 'origin', e.origin) ORDER BY e.id)
                                                        FROM metric_example e
                                                       WHERE e.metric_id = m.id AND e.status = 'approved'), '[]'))
          FROM metric m JOIN meta_table t ON t.id = m.base_table_id
         WHERE m.id = %s AND m.system_id = %s
    """, (version, action, actor, str(metric_id), str(system_id)))


def _values(data: MetricInput, base_table_id: str) -> tuple:
    return (
        data.name,
        data.description,
        data.kind.value,
        base_table_id,
        json.dumps([j.model_dump() for j in data.joins]),
        data.agg_field,
        data.agg_function,
        json.dumps(data.select_columns),
        data.expression,
        json.dumps(data.series.model_dump()) if data.series else None,
        json.dumps(data.fixed_filters),
        json.dumps(data.synonyms, ensure_ascii=False),
    )


def _guard_duplicate_name(system_id: UUID, name: str, except_id: UUID | None = None) -> None:
    existing = db.one(
        "SELECT id FROM metric WHERE system_id = %s AND lower(name) = lower(%s) AND id <> %s",
        str(system_id), name, str(except_id or uuid4()),
    )
    if existing:
        raise ApiException(409, f"'{name}' 이름의 지표가 이미 있습니다")


def _guard_not_referenced(system_id: UUID, name: str, what: str) -> None:
    """파생 지표가 쓰는 지표를 지우거나 바꾸면 그 파생 지표가 조용히 깨진다."""
    users = []
    for r in db.query("SELECT name, expression FROM metric WHERE system_id = %s AND kind = 'derived'", str(system_id)):
        try:
            _, refs = compile_formula(r["expression"], lambda n: "x")
        except ValueError:
            continue
        if any(ref.lower() == name.lower() for ref in refs):
            users.append(r["name"])
    if users:
        raise ApiException(
            400, f"파생 지표 {', '.join(users)}이(가) '{name}'을(를) 쓰고 있어 {what} 수 없습니다. 파생 지표를 먼저 고치십시오"
        )


def _validate(system_id: UUID, data: MetricInput) -> str:
    """정의를 저장된 스키마와 대조한다. 정규화된 기본 테이블 이름을 돌려준다."""
    tables = load_tables(system_id)
    if not tables:
        raise ApiException(400, "스키마가 비어 있습니다. 동기화를 먼저 실행하십시오")
    columns = {name: [c["name"] for c in t["columns"]] for name, t in tables.items()}
    types = {(t["name"], c["name"]): c["type"] for t in tables.values() for c in t["columns"]}
    driver = db.one("SELECT driver FROM meta_system WHERE id = %s", str(system_id))["driver"]

    try:
        scope, _ = build_scope(data.table_name, [j.model_dump() for j in data.joins], columns)
    except ValueError as error:
        raise ApiException(400, str(error).replace("조회할 수 없는 테이블입니다", "테이블을 찾을 수 없습니다")) from None
    base = next(iter(scope))

    def require(ref, what: str) -> tuple[str, str]:
        try:
            return resolve_ref(str(ref or ""), scope, base, f"{what} 컬럼을 찾을 수 없습니다")
        except ValueError as error:
            raise ApiException(400, str(error)) from None

    if data.kind is MetricKind.AGGREGATE:
        if data.series is not None:
            _validate_series(data, scope, base, types, driver)
        elif data.agg_field != "*":
            require(data.agg_field, "집계")
        elif data.agg_function != "COUNT":
            raise ApiException(400, "* 는 COUNT 에만 쓸 수 있습니다")
    elif data.kind is MetricKind.PROJECTION:
        for column in data.select_columns:
            require(column, "조회")
    else:
        _validate_formula(system_id, data, scope)

    for filter_spec in data.fixed_filters:
        table, column = require(filter_spec.get("field"), "필터")
        where = f"{table}.{column}"
        source = str(filter_spec.get("source") or "literal").lower()
        if source not in FILTER_SOURCES:
            raise ApiException(
                400, f"알 수 없는 필터 출처입니다: {source} (사용 가능: {', '.join(FILTER_SOURCES)})")
        kind = temporal_kind(types.get((table, column), ""), driver)
        operator = filter_spec.get("operator")
        if operator in ("is_null", "is_not_null"):
            continue
        if operator in ("contains", "starts_with", "ends_with") and (kind or is_numeric_type(types.get((table, column), ""))):
            raise ApiException(400, f"{where} 는 문자 컬럼이 아니라 {operator!r} 를 쓸 수 없습니다")

        # 값이 질문에서 오므로 여기서 검사할 값이 없다.
        if source == "question":
            continue

        if source == "relative":
            if not kind:
                raise ApiException(
                    400, f"{where} 는 날짜 컬럼이 아니라 상대 기간을 쓸 수 없습니다")
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
        if kind == "datetime" and value_kind == "date" and operator in ("equals", "not_equals"):
            raise ApiException(
                400,
                f"{where} 는 시각을 포함하는 컬럼이라 {operator!r} 로는 하루를 고를 수 없습니다 "
                "(자정인 행만 걸립니다). greater_or_equal 과 less_or_equal 로 기간을 지정하십시오",
            )

    _validate_examples(system_id, data, base)
    return base


def _validate_series(data: MetricInput, scope: dict, base: str, types: dict, driver: str) -> None:
    """시계열 집계: 값·구분·순서 컬럼은 모두 기본 테이블에 있어야 한다 (LAG 서브쿼리가 기본 테이블만 감싼다)."""
    only_base = {base: scope[base]}
    labels = (("집계", data.agg_field), ("구분", data.series.partition_by), ("순서", data.series.order_by))
    resolved = {}
    for what, ref in labels:
        if ref == "*":
            raise ApiException(400, f"{data.agg_function} 는 * 대신 컬럼이 필요합니다")
        try:
            resolved[what] = resolve_ref(str(ref or ""), only_base, base, f"{what} 컬럼을 기본 테이블 {base} 에서 찾을 수 없습니다")
        except ValueError as error:
            raise ApiException(400, str(error)) from None
    value_type = types.get(resolved["집계"], "")
    if data.agg_function == "DELTA_SUM" and not is_numeric_type(value_type):
        raise ApiException(400, f"DELTA_SUM 은 숫자 컬럼에만 씁니다 -> {base}.{resolved['집계'][1]} ({value_type})")
    if data.agg_function == "CHANGE_COUNT" and is_numeric_type(value_type):
        try:
            float(data.series.baseline)
        except ValueError:
            raise ApiException(400, f"정상값은 숫자여야 합니다 -> {data.series.baseline!r}") from None
    if not temporal_kind(types.get(resolved["순서"], ""), driver) and not is_numeric_type(types.get(resolved["순서"], "")):
        raise ApiException(400, f"순서 컬럼은 날짜·시각이나 숫자 컬럼이어야 합니다 -> {base}.{resolved['순서'][1]}")


def _validate_formula(system_id: UUID, data: MetricInput, scope: dict) -> None:
    rows = {r["name"].lower(): r for r in load_metric_rows(system_id)}

    def check(name):
        comp = rows.get(name.lower())
        if comp is None:
            raise ValueError(f"수식의 지표 [{name}] 이(가) 없습니다")
        if comp["kind"] != "aggregate":
            raise ValueError(f"수식에는 집계형 지표만 쓸 수 있습니다 -> [{name}] 은(는) {comp['kind']}")
        if comp["table_name"] not in scope:
            raise ValueError(f"[{name}] 의 테이블 {comp['table_name']} 이(가) 이 지표의 범위({', '.join(scope)})에 없습니다")
        if any(str(f.get("source") or "literal") == "question" for f in comp["fixed_filters"] or []):
            raise ValueError(f"[{name}] 은(는) 질문에서 값을 받는 필터가 있어 수식에 쓸 수 없습니다")
        return "x"

    try:
        compile_formula(data.expression, check)
    except ValueError as error:
        raise ApiException(400, str(error)) from None


def _validate_examples(system_id: UUID, data: MetricInput, base: str) -> None:
    """예시에 AST 가 있으면 실제로 컴파일되는지 확인한다 — 틀린 모범 답안은 모델을 틀리게 가르친다."""
    with_ast = [e for e in data.examples if e.ast is not None]
    if not with_ast:
        return
    contract = load_contract(system_id)
    draft = metric_entry({
        "name": data.name, "description": data.description, "kind": data.kind.value, "table_name": base,
        "joins": [j.model_dump() for j in data.joins], "select_columns": data.select_columns,
        "expression": data.expression, "agg_field": data.agg_field, "agg_function": data.agg_function,
        "fixed_filters": data.fixed_filters,
        "series": data.series.model_dump() if data.series else None,
    })
    contract["metrics"] = [m for m in contract["metrics"] if m["name"].lower() != data.name.lower()] + [draft]
    compiler = Compiler(contract)
    for example in with_ast:
        try:
            compiler.compile(example.ast)
        except (ValueError, KeyError, TypeError) as error:
            raise ApiException(400, f"예시 '{example.question}' 의 AST 가 컴파일되지 않습니다: {error}") from None


def _row_to_metric(row: dict, examples: dict) -> Metric:
    return Metric(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        kind=MetricKind(row["kind"]),
        table_name=row["table_name"],
        joins=row.get("joins") or [],
        agg_field=row["agg_field"],
        agg_function=row["agg_function"],
        select_columns=row["select_columns"] or [],
        expression=row.get("expression"),
        series=row.get("series"),
        fixed_filters=row["fixed_filters"] or [],
        examples=examples.get("approved", []),
        draft_example_count=examples.get("drafts", 0),
        synonyms=row.get("synonyms") or [],
        status=row["status"],
        broken_reason=row.get("broken_reason"),
        version=row.get("version") or 1,
        updated_at=row["updated_at"].isoformat() if row.get("updated_at") else None,
    )
