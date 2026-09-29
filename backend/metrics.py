import logging
import json
from uuid import UUID, uuid4
import db
from models import Metric, MetricInput, MetricKind, ApiException
from compiler import (
    FILTER_SOURCES, build_scope, parse_temporal, resolve_ref, resolve_relative, temporal_kind,
)

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
    row = db.one(
        "SELECT * FROM datasource_metric WHERE id = %s AND datasource_id = %s",
        str(metric_id),
        str(datasource_id)
    )
    if row is None:
        raise ApiException(404, "지표를 찾을 수 없습니다")
    return _row_to_metric(row)


def create_metric(datasource_id: UUID, input_data: MetricInput) -> Metric:
    """지표 생성.

    가리키는 테이블·컬럼이 저장된 스키마에 실제로 있는지 여기서 확인한다.
    질문 시점에 발견하면 사용자는 왜 실패하는지 알 수 없다.
    """
    types: dict[tuple[str, str], str] = {}
    tables: dict[str, list[str]] = {}
    for r in db.query("""
        SELECT t.name AS table_name, c.name AS column_name, c.data_type
          FROM datasource_table t
          LEFT JOIN datasource_column c ON c.table_id = t.id
         WHERE t.datasource_id = %s
         ORDER BY t.name, c.ordinal
    """, str(datasource_id)):
        columns = tables.setdefault(r["table_name"], [])
        if r["column_name"] is not None:
            columns.append(r["column_name"])
            types[(r["table_name"], r["column_name"])] = r["data_type"] or ""
    driver = db.one("SELECT driver FROM datasource WHERE id = %s", str(datasource_id))["driver"]

    joins = [j.model_dump() for j in input_data.joins]
    try:
        scope, _ = build_scope(input_data.table_name, joins, tables)
    except ValueError as error:
        raise ApiException(400, str(error)) from None
    base = next(iter(scope))

    def _require_column(column, what: str) -> tuple[str, str]:
        try:
            return resolve_ref(column, scope, base)
        except ValueError as error:
            raise ApiException(400, f"{what} 컬럼: {error}") from None

    if input_data.kind is MetricKind.AGGREGATE:
        if input_data.agg_field != "*":
            _require_column(input_data.agg_field, "집계")
    else:
        for column in input_data.select_columns:
            _require_column(column, "조회")

    for filter_spec in input_data.fixed_filters:
        field = filter_spec.get("field")
        ref = _require_column(field, "필터")
        where = ".".join(ref)
        source = str(filter_spec.get("source") or "literal").lower()
        if source not in FILTER_SOURCES:
            raise ApiException(
                400, f"알 수 없는 필터 출처입니다: {source} (사용 가능: {', '.join(FILTER_SOURCES)})")

        kind = temporal_kind(types.get(ref, ""), driver)

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
        operator = filter_spec.get("operator")
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

    existing = db.one(
        "SELECT id FROM datasource_metric WHERE datasource_id = %s AND lower(name) = lower(%s)",
        str(datasource_id),
        input_data.name
    )
    if existing:
        raise ApiException(409, f"'{input_data.name}' 이름의 지표가 이미 있습니다")

    metric_id = uuid4()
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO datasource_metric
                (id, datasource_id, name, description, kind, table_name, joins,
                 agg_field, agg_function, select_columns, fixed_filters)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                str(metric_id),
                str(datasource_id),
                input_data.name,
                input_data.description,
                input_data.kind.value,
                base,
                json.dumps(joins),
                input_data.agg_field,
                input_data.agg_function,
                json.dumps(input_data.select_columns),
                json.dumps(input_data.fixed_filters),
            ))

    log.info("metric created: %s (%s, kind=%s, joins=%d)",
             metric_id, input_data.name, input_data.kind.value, len(joins))
    return get_metric(datasource_id, metric_id)


def delete_metric(datasource_id: UUID, metric_id: UUID) -> None:
    """지표 삭제"""
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM datasource_metric WHERE id = %s AND datasource_id = %s",
                (str(metric_id), str(datasource_id))
            )

    log.info("metric deleted: %s", metric_id)


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
        fixed_filters=row["fixed_filters"] or [],
    )
