import json
import logging
from uuid import UUID, uuid4

import db
from compiler import (
    FILTER_SOURCES, Compiler, build_scope, compile_formula, is_numeric_type, parse_temporal, resolve_ref,
    resolve_relative, temporal_kind,
)
from contract import load_contract, load_tables, metric_entry
from models import ApiException, Metric, MetricHistoryEntry, MetricInput, MetricKind

log = logging.getLogger("nl2sql.metrics")


def list_metrics(datasource_id: UUID) -> list[Metric]:
    """지표 목록"""
    rows = db.query(
        "SELECT * FROM datasource_metric WHERE datasource_id = %s ORDER BY created_at DESC",
        str(datasource_id)
    )
    return [_row_to_metric(r) for r in rows]


def get_metric(datasource_id: UUID, metric_id: UUID) -> Metric:
    """지표 조회"""
    return _row_to_metric(_get_row(datasource_id, metric_id))


def _get_row(datasource_id: UUID, metric_id: UUID) -> dict:
    row = db.one(
        "SELECT * FROM datasource_metric WHERE id = %s AND datasource_id = %s",
        str(metric_id),
        str(datasource_id)
    )
    if row is None:
        raise ApiException(404, "지표를 찾을 수 없습니다")
    return row


def create_metric(datasource_id: UUID, input_data: MetricInput) -> Metric:
    """지표 생성.

    가리키는 테이블·컬럼이 저장된 스키마에 실제로 있는지 여기서 확인한다.
    질문 시점에 발견하면 사용자는 왜 실패하는지 알 수 없다.
    """
    _guard_duplicate_name(datasource_id, input_data.name)
    table_name = _validate(datasource_id, input_data)

    metric_id = uuid4()
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO datasource_metric
                (id, datasource_id, name, description, kind, table_name,
                 joins, agg_field, agg_function, select_columns, expression, fixed_filters, examples)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (str(metric_id), str(datasource_id), *_values(input_data, table_name)))
            _record_history(cur, datasource_id, metric_id, 1, "create")

    log.info("metric created: %s (%s, kind=%s)", metric_id, input_data.name, input_data.kind.value)
    return get_metric(datasource_id, metric_id)


def update_metric(datasource_id: UUID, metric_id: UUID, input_data: MetricInput) -> Metric:
    """지표 수정. 버전을 올리고 이전 정의는 이력에 남는다."""
    current = _get_row(datasource_id, metric_id)
    _guard_duplicate_name(datasource_id, input_data.name, except_id=metric_id)
    if current["name"].lower() != input_data.name.lower() or input_data.kind is not MetricKind.AGGREGATE:
        _guard_not_referenced(datasource_id, current["name"], "이름을 바꾸거나 집계형이 아니게 바꿀")
    table_name = _validate(datasource_id, input_data)

    version = current["version"] + 1
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE datasource_metric SET
                    name = %s, description = %s, kind = %s, table_name = %s, joins = %s,
                    agg_field = %s, agg_function = %s, select_columns = %s, expression = %s,
                    fixed_filters = %s, examples = %s, version = %s, updated_at = now()
                WHERE id = %s AND datasource_id = %s
            """, (*_values(input_data, table_name), version, str(metric_id), str(datasource_id)))
            _record_history(cur, datasource_id, metric_id, version, "update")

    log.info("metric updated: %s (%s, v%d)", metric_id, input_data.name, version)
    return get_metric(datasource_id, metric_id)


def delete_metric(datasource_id: UUID, metric_id: UUID) -> None:
    """지표 삭제. 마지막 정의는 이력에 남는다."""
    row = db.one(
        "SELECT name, version FROM datasource_metric WHERE id = %s AND datasource_id = %s",
        str(metric_id), str(datasource_id),
    )
    if row is None:
        return
    _guard_not_referenced(datasource_id, row["name"], "지울")
    with db.connection() as conn:
        with conn.cursor() as cur:
            _record_history(cur, datasource_id, metric_id, row["version"], "delete")
            cur.execute(
                "DELETE FROM datasource_metric WHERE id = %s AND datasource_id = %s",
                (str(metric_id), str(datasource_id))
            )

    log.info("metric deleted: %s", metric_id)


def list_metric_history(datasource_id: UUID, metric_id: UUID) -> list[MetricHistoryEntry]:
    rows = db.query("""
        SELECT version, action, snapshot, created_at FROM datasource_metric_history
         WHERE datasource_id = %s AND metric_id = %s ORDER BY id DESC
    """, str(datasource_id), str(metric_id))
    if not rows:
        raise ApiException(404, "지표 이력을 찾을 수 없습니다")
    return [
        MetricHistoryEntry(version=r["version"], action=r["action"], snapshot=r["snapshot"],
                           created_at=r["created_at"].isoformat())
        for r in rows
    ]


def _record_history(cur, datasource_id: UUID, metric_id: UUID, version: int, action: str) -> None:
    cur.execute("""
        INSERT INTO datasource_metric_history (datasource_id, metric_id, version, action, snapshot)
        SELECT datasource_id, id, %s, %s,
               jsonb_build_object('name', name, 'description', description, 'kind', kind,
                                  'table_name', table_name, 'joins', joins, 'agg_field', agg_field,
                                  'agg_function', agg_function, 'select_columns', select_columns,
                                  'expression', expression, 'fixed_filters', fixed_filters,
                                  'examples', examples)
          FROM datasource_metric WHERE id = %s AND datasource_id = %s
    """, (version, action, str(metric_id), str(datasource_id)))


def _values(input_data: MetricInput, table_name: str) -> tuple:
    return (
        input_data.name,
        input_data.description,
        input_data.kind.value,
        table_name,
        json.dumps([j.model_dump() for j in input_data.joins]),
        input_data.agg_field,
        input_data.agg_function,
        json.dumps(input_data.select_columns),
        input_data.expression,
        json.dumps(input_data.fixed_filters),
        json.dumps([e.model_dump() for e in input_data.examples], ensure_ascii=False),
    )


def _guard_duplicate_name(datasource_id: UUID, name: str, except_id: UUID | None = None) -> None:
    existing = db.one(
        "SELECT id FROM datasource_metric WHERE datasource_id = %s AND lower(name) = lower(%s) AND id <> %s",
        str(datasource_id), name, str(except_id or uuid4()),
    )
    if existing:
        raise ApiException(409, f"'{name}' 이름의 지표가 이미 있습니다")


def _guard_not_referenced(datasource_id: UUID, name: str, what: str) -> None:
    """파생 지표가 쓰는 지표를 지우거나 바꾸면 그 파생 지표가 조용히 깨진다."""
    users = []
    for r in db.query(
        "SELECT name, expression FROM datasource_metric WHERE datasource_id = %s AND kind = 'derived'",
        str(datasource_id),
    ):
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


def _validate(datasource_id: UUID, data: MetricInput) -> str:
    """정의를 저장된 스키마와 대조한다. 정규화된 기본 테이블 이름을 돌려준다."""
    tables = load_tables(datasource_id)
    if not tables:
        raise ApiException(400, "스키마가 비어 있습니다. 동기화를 먼저 실행하십시오")
    columns = {name: [c["name"] for c in t["columns"]] for name, t in tables.items()}
    types = {(t["name"], c["name"]): c["type"] for t in tables.values() for c in t["columns"]}
    driver = db.one("SELECT driver FROM datasource WHERE id = %s", str(datasource_id))["driver"]

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
        if data.agg_field != "*":
            require(data.agg_field, "집계")
        elif data.agg_function != "COUNT":
            raise ApiException(400, "* 는 COUNT 에만 쓸 수 있습니다")
    elif data.kind is MetricKind.PROJECTION:
        for column in data.select_columns:
            require(column, "조회")
    else:
        _validate_formula(datasource_id, data, scope)

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

    _validate_examples(datasource_id, data, base)
    return base


def _validate_formula(datasource_id: UUID, data: MetricInput, scope: dict) -> None:
    rows = {
        r["name"].lower(): r
        for r in db.query(
            "SELECT name, kind, table_name, fixed_filters FROM datasource_metric WHERE datasource_id = %s",
            str(datasource_id),
        )
    }

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


def _validate_examples(datasource_id: UUID, data: MetricInput, base: str) -> None:
    """예시에 AST 가 있으면 실제로 컴파일되는지 확인한다 — 틀린 모범 답안은 모델을 틀리게 가르친다."""
    with_ast = [e for e in data.examples if e.ast is not None]
    if not with_ast:
        return
    contract = load_contract(datasource_id)
    draft = metric_entry({
        "name": data.name, "description": data.description, "kind": data.kind.value, "table_name": base,
        "joins": [j.model_dump() for j in data.joins], "select_columns": data.select_columns,
        "expression": data.expression, "agg_field": data.agg_field, "agg_function": data.agg_function,
        "fixed_filters": data.fixed_filters,
    })
    contract["metrics"] = [m for m in contract["metrics"] if m["name"].lower() != data.name.lower()] + [draft]
    compiler = Compiler(contract)
    for example in with_ast:
        try:
            compiler.compile(example.ast)
        except (ValueError, KeyError, TypeError) as error:
            raise ApiException(400, f"예시 '{example.question}' 의 AST 가 컴파일되지 않습니다: {error}") from None


def _row_to_metric(row: dict) -> Metric:
    """DB 행을 Metric 모델로 변환"""
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
        fixed_filters=row["fixed_filters"] or [],
        examples=row.get("examples") or [],
        version=row.get("version") or 1,
        updated_at=row["updated_at"].isoformat() if row.get("updated_at") else None,
    )
