import logging
import json
from uuid import UUID, uuid4
import db
from models import Metric, MetricInput, ApiException

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
    """지표 생성"""
    # 테이블 존재 확인
    table = db.one(
        "SELECT id FROM datasource_table WHERE datasource_id = %s AND name = %s",
        str(datasource_id),
        input_data.table_name
    )
    if table is None:
        raise ApiException(400, f"테이블을 찾을 수 없습니다: {input_data.table_name}")

    # 컬럼 존재 확인 (agg_field가 * 아닐 때)
    if input_data.agg_field != "*":
        col = db.one(
            "SELECT id FROM datasource_column WHERE table_id = %s AND name = %s",
            table["id"],
            input_data.agg_field
        )
        if col is None:
            raise ApiException(400, f"컬럼을 찾을 수 없습니다: {input_data.table_name}.{input_data.agg_field}")

    # 집계 함수 검증
    valid_functions = ("SUM", "COUNT", "AVG", "MIN", "MAX")
    if input_data.agg_function not in valid_functions:
        raise ApiException(400, f"허용되지 않은 함수: {input_data.agg_function}")

    # 고정 필터의 컬럼 존재 확인
    for filter_spec in input_data.fixed_filters:
        col = db.one(
            "SELECT id FROM datasource_column WHERE table_id = %s AND name = %s",
            table["id"],
            filter_spec.get("field")
        )
        if col is None:
            raise ApiException(400, f"필터 컬럼을 찾을 수 없습니다: {input_data.table_name}.{filter_spec.get('field')}")

    # 중복된 이름 검사
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
                (id, datasource_id, name, description, table_name, agg_field, agg_function, fixed_filters)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                str(metric_id),
                str(datasource_id),
                input_data.name,
                input_data.description,
                input_data.table_name,
                input_data.agg_field,
                input_data.agg_function,
                json.dumps(input_data.fixed_filters),
            ))
        conn.commit()

    log.info("metric created: %s (%s)", metric_id, input_data.name)
    return get_metric(datasource_id, metric_id)


def delete_metric(datasource_id: UUID, metric_id: UUID) -> None:
    """지표 삭제"""
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM datasource_metric WHERE id = %s AND datasource_id = %s",
                (str(metric_id), str(datasource_id))
            )
        conn.commit()

    log.info("metric deleted: %s", metric_id)


def _row_to_metric(row: dict) -> Metric:
    """DB 행을 Metric 모델로 변환"""
    return Metric(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        table_name=row["table_name"],
        agg_field=row["agg_field"],
        agg_function=row["agg_function"],
        fixed_filters=row["fixed_filters"] or [],
    )
