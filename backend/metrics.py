import logging
import json
from uuid import UUID, uuid4
import db
from models import Metric, MetricInput, MetricKind, ApiException
from compiler import temporal_kind, parse_temporal

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
    table = db.one(
        "SELECT id FROM datasource_table WHERE datasource_id = %s AND name = %s",
        str(datasource_id),
        input_data.table_name
    )
    if table is None:
        raise ApiException(400, f"테이블을 찾을 수 없습니다: {input_data.table_name}")

    types = {
        r["name"].lower(): r["data_type"]
        for r in db.query(
            "SELECT name, data_type FROM datasource_column WHERE table_id = %s", table["id"]
        )
    }
    known = set(types)
    driver = db.one("SELECT driver FROM datasource WHERE id = %s", str(datasource_id))["driver"]

    def _require_column(column: str, what: str) -> None:
        if not column or column.lower() not in known:
            raise ApiException(
                400, f"{what} 컬럼을 찾을 수 없습니다: {input_data.table_name}.{column}"
            )

    if input_data.kind is MetricKind.AGGREGATE:
        if input_data.agg_field != "*":
            _require_column(input_data.agg_field, "집계")
    else:
        for column in input_data.select_columns:
            _require_column(column, "조회")

    for filter_spec in input_data.fixed_filters:
        field = filter_spec.get("field")
        _require_column(field, "고정 필터")
        # 날짜 값은 여기서 막는다. 질문 시점에 터지면 사용자는 왜 실패하는지 모른다.
        kind = temporal_kind(types.get(str(field).lower(), ""), driver)
        if not kind:
            continue
        where = f"{input_data.table_name}.{field}"
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
                (id, datasource_id, name, description, kind, table_name,
                 agg_field, agg_function, select_columns, fixed_filters)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                str(metric_id),
                str(datasource_id),
                input_data.name,
                input_data.description,
                input_data.kind.value,
                input_data.table_name,
                input_data.agg_field,
                input_data.agg_function,
                json.dumps(input_data.select_columns),
                json.dumps(input_data.fixed_filters),
            ))

    log.info("metric created: %s (%s, kind=%s)", metric_id, input_data.name, input_data.kind.value)
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
        agg_field=row["agg_field"],
        agg_function=row["agg_function"],
        select_columns=row["select_columns"] or [],
        fixed_filters=row["fixed_filters"] or [],
    )
