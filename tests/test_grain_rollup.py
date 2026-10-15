"""지표의 기본 기간 단위와 더 큰 단위로의 합산 (계산 단위 · rollup · 단위 겹침 방지)"""

import pytest

from ask import _prompt_metric
from test_utc_offset import make

DAY = {"field": "collect_datetime", "grain": "day"}
MONTH = {"field": "collect_datetime", "grain": "month"}


def fn(name, *args, **extra):
    return {"fn": name, "args": list(args), **extra}


def span(col, **extra):
    return fn("ABS", fn("SUB", fn("MAX", {"col": col}), fn("MIN", {"col": col})), **extra)


# 장비별 하루 무인 가동시간 — 결과를 날짜(일)·장비로 나눈다 (daily_stats)
PER_VEHICLE = {
    "name": "장비별 무인 가동시간",
    "kind": "aggregate",
    "table": "bms_log",
    "fixed_filters": [],
    "measures": [{"name": "무인 가동시간", "expr": span("uman_time", by=[DAY, "vehicle_id"])}],
}

# 날짜별 전체 합계 — 안쪽은 장비별, 바깥은 날짜(일)별 SUM
DAILY_TOTAL = {
    "name": "일별 무인 가동시간",
    "kind": "aggregate",
    "table": "bms_log",
    "fixed_filters": [],
    "measures": [{"name": "무인 가동시간", "expr": fn("SUM", span("uman_time", by=["vehicle_id"]), by=[DAY])}],
}

INNER_DAY = "CAST((\"collect_datetime\" + INTERVAL '9 hours') AS DATE)"
INNER_MONTH = "CAST(DATE_TRUNC('month', (\"collect_datetime\" + INTERVAL '9 hours')) AS DATE)"


def compile_(metric, **ast):
    return make(metrics=[metric]).compile({"metric": metric["name"], **ast})


def test_no_grain_in_question_uses_metric_default_day():
    sql = compile_(PER_VEHICLE)
    assert sql == (
        f'SELECT {INNER_DAY} AS "collect_datetime_day", "vehicle_id", '
        'ABS(MAX("uman_time") - MIN("uman_time")) AS "무인 가동시간" FROM "bms_log" '
        f'GROUP BY {INNER_DAY}, "vehicle_id";'
    )


def test_same_grain_is_not_repeated():
    sql = compile_(PER_VEHICLE, group_by=[DAY, "vehicle_id"])
    assert sql == compile_(PER_VEHICLE)


def test_larger_grain_computes_per_day_then_sums_per_month():
    """월별 장비별 — 일·장비 단위로 계산한 뒤 월·장비로 합친다 (일 단위가 결과에 섞이지 않는다)"""
    sql = compile_(PER_VEHICLE, group_by=[MONTH, "vehicle_id"])
    assert sql == (
        'SELECT "collect_datetime_month", "vehicle_id", SUM("_m1") AS "무인 가동시간" '
        f'FROM (SELECT {INNER_MONTH} AS "collect_datetime_month", "vehicle_id" AS "vehicle_id", '
        'ABS(MAX("uman_time") - MIN("uman_time")) AS "_m1" FROM "bms_log" '
        f'GROUP BY {INNER_MONTH}, "vehicle_id", {INNER_DAY}) "nested" '
        'GROUP BY "collect_datetime_month", "vehicle_id";'
    )


@pytest.mark.parametrize("rollup", ["AVG", "MAX"])
def test_rollup_function_is_chosen_per_measure(rollup):
    metric = {**PER_VEHICLE, "measures": [{**PER_VEHICLE["measures"][0], "rollup": rollup}]}
    sql = compile_(metric, group_by=[MONTH])
    assert f'{rollup}("_m1") AS "무인 가동시간"' in sql


def test_rollup_none_recomputes_at_question_grain():
    """다시 계산 — 월 전체의 MAX - MIN (일별 합이 아니라)"""
    metric = {**PER_VEHICLE, "measures": [{**PER_VEHICLE["measures"][0], "rollup": "NONE"}]}
    sql = compile_(metric, group_by=[MONTH])
    assert sql == (
        f'SELECT {INNER_MONTH} AS "collect_datetime_month", "vehicle_id", '
        'ABS(MAX("uman_time") - MIN("uman_time")) AS "무인 가동시간" FROM "bms_log" '
        f'GROUP BY {INNER_MONTH}, "vehicle_id";'
    )


def test_nested_metric_adds_day_to_inner_by():
    """이미 SUM(… BY 장비) 인 지표는 안쪽 묶음에 일을 더하고 바깥 SUM 이 월로 합친다"""
    sql = compile_(DAILY_TOTAL, group_by=[MONTH])
    assert f'GROUP BY {INNER_MONTH}, "vehicle_id", {INNER_DAY}) "nested" GROUP BY "collect_datetime_month"' in sql


def test_smaller_grain_recomputes_at_question_grain():
    """최근 5시간 시간별 장비별 — 기본 단위(일)를 쓰지 않고 시간·장비로 다시 계산한다"""
    hour = "DATE_TRUNC('hour', (\"collect_datetime\" + INTERVAL '9 hours'))"
    sql = compile_(
        PER_VEHICLE,
        group_by=[{"field": "collect_datetime", "grain": "hour"}, "vehicle_id"],
        filters=[{"field": "collect_datetime", "operator": "greater_or_equal", "value": "2026-10-14 09:16"}],
    )
    assert sql == (
        f'SELECT {hour} AS "collect_datetime_hour", "vehicle_id", '
        'ABS(MAX("uman_time") - MIN("uman_time")) AS "무인 가동시간" FROM "bms_log" '
        "WHERE \"collect_datetime\" >= TIMESTAMP '2026-10-14 09:16:00' - INTERVAL '9 hours' "
        f'GROUP BY {hour}, "vehicle_id";'
    )


def test_nested_metric_at_smaller_grain_drops_default_day():
    """SUM(… BY 장비) BY 일 을 시간별로 물으면 안쪽은 시간·장비, 바깥은 시간"""
    sql = compile_(DAILY_TOTAL, group_by=[{"field": "collect_datetime", "grain": "hour"}])
    assert "CAST((\"collect_datetime\"" not in sql  # 일 단위가 끼지 않는다
    assert sql.endswith('GROUP BY "collect_datetime_hour";')


def test_raw_timestamp_grouping_asks_for_a_grain():
    with pytest.raises(ValueError, match="단위"):
        compile_(PER_VEHICLE, group_by=["collect_datetime"])


@pytest.mark.parametrize(
    "driver,time_type,expected",
    [
        ("mysql", "datetime", "CAST(DATE_FORMAT((`collect_datetime` + INTERVAL 9 HOUR), '%Y-%m-%d %H:00:00') AS DATETIME)"),
        ("oracle", "DATE", "TRUNC((\"collect_datetime\" + INTERVAL '9' HOUR), 'HH24')"),
    ],
)
def test_hour_grain_per_driver(driver, time_type, expected):
    sql = make(driver=driver, time_type=time_type).compile(
        {
            "target_table": "bms_log",
            "aggregations": [{"field": "*", "function": "COUNT", "alias": "건수"}],
            "group_by": [{"field": "collect_datetime", "grain": "hour"}],
        }
    )
    assert expected in sql


def test_hour_grain_needs_time_of_day():
    with pytest.raises(ValueError, match="시각이 없는 날짜"):
        make(time_type="date").compile(
            {
                "target_table": "bms_log",
                "aggregations": [{"field": "*", "function": "COUNT", "alias": "건수"}],
                "group_by": [{"field": "collect_datetime", "grain": "hour"}],
            }
        )


def test_same_column_with_two_grains_is_rejected():
    """질문이 월과 일로 함께 묶으면 결과가 일 단위로 쪼개진다 — 되돌려 보낸다"""
    with pytest.raises(ValueError, match="두 단위로 묶었습니다"):
        make().compile(
            {
                "target_table": "bms_log",
                "aggregations": [{"field": "*", "function": "COUNT", "alias": "건수"}],
                "group_by": [MONTH, DAY],
            }
        )


def test_prompt_tells_default_and_allowed_grains():
    metric = {"name": "장비 가동", "table": "bms_log", "dimensions": [DAY, "vehicle_id"], "formulas": []}
    note = _prompt_metric(metric)["묶음"]
    assert "collect_datetime(day), vehicle_id 단위로 계산됩니다" in note
    assert "hour·day·week·month·quarter·year" in note
