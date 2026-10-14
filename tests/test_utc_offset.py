"""UTC 로 저장된 시각 컬럼의 현지 시각 보정 (meta_column.utc_offset_hours)"""

import pytest

from compiler import Compiler, to_utc


def fn(name, *args, **extra):
    return {"fn": name, "args": list(args), **extra}


V = ["vehicle_id"]


def span(col):
    return fn("SUB", fn("MAX", {"col": col}, by=V), fn("MIN", {"col": col}, by=V))


UPTIME = {
    "name": "장비 가동시간",
    "kind": "aggregate",
    "table": "bms_log",
    "fixed_filters": [],
    "measures": [
        {"name": "무인 가동시간", "expr": fn("SUM", span("uman_time"))},
        {
            "name": "유인 가동시간",
            "expr": fn("SUM", fn("SUB", span("eg_time"), span("uman_time"))),
        },
    ],
}


def make(offset=9, driver="postgresql", time_type="timestamp", metrics=()):
    column = {"name": "collect_datetime", "type": time_type}
    if offset is not None:
        column["utc_offset"] = offset
    return Compiler(
        {
            "driver": driver,
            "relations": [],
            "metrics": list(metrics),
            "tables": [
                {
                    "name": "bms_log",
                    "columns": [
                        column,
                        {"name": "vehicle_id", "type": "varchar(20)"},
                        {"name": "uman_time", "type": "numeric"},
                        {"name": "eg_time", "type": "numeric"},
                    ],
                }
            ],
        }
    )


def test_to_utc_shifts_local_date_and_datetime():
    assert to_utc("2026-10-09", 9) == "2026-10-08 15:00:00"
    assert to_utc("2026-10-09 10:30", 9) == "2026-10-09 01:30:00"
    assert to_utc("2026-10-09", -5) == "2026-10-09 05:00:00"


def test_daily_uptime_groups_by_local_day_and_filters_in_utc():
    """원본: DATE(collect_datetime + 9 hours) 로 묶고 KST 자정부터 조회"""
    sql = make(metrics=[UPTIME]).compile(
        {
            "metric": ["장비 가동시간"],
            "group_by": [{"field": "collect_datetime", "grain": "day"}],
            "filters": [
                {"field": "collect_datetime", "operator": "greater_or_equal", "value": "2026-10-09"}
            ],
            "order_by": [{"field": "collect_datetime_day", "direction": "desc"}],
        }
    )
    assert sql == (
        'SELECT "collect_datetime_day", SUM("_m1" - "_m2") AS "무인 가동시간", '
        'SUM(("_m3" - "_m4") - ("_m1" - "_m2")) AS "유인 가동시간" '
        "FROM (SELECT CAST((\"collect_datetime\" + INTERVAL '9 hours') AS DATE) AS \"collect_datetime_day\", "
        'MAX("uman_time") AS "_m1", MIN("uman_time") AS "_m2", MAX("eg_time") AS "_m3", MIN("eg_time") AS "_m4" '
        "FROM \"bms_log\" WHERE \"collect_datetime\" >= TIMESTAMP '2026-10-09 00:00:00' - INTERVAL '9 hours' "
        "GROUP BY CAST((\"collect_datetime\" + INTERVAL '9 hours') AS DATE), \"vehicle_id\") \"nested\" "
        'GROUP BY "collect_datetime_day" ORDER BY "collect_datetime_day" DESC NULLS LAST;'
    )


def test_end_of_period_covers_whole_local_day():
    """<= 10월 9일 은 KST 10일 0시 전까지 — 현지 날짜에서 시차를 빼 UTC 로 비교한다"""
    sql = make().compile(
        {
            "target_table": "bms_log",
            "aggregations": [{"field": "*", "function": "COUNT", "alias": "건수"}],
            "filters": [
                {"field": "collect_datetime", "operator": "less_or_equal", "value": "2026-10-09"}
            ],
        }
    )
    assert "\"collect_datetime\" < TIMESTAMP '2026-10-10 00:00:00' - INTERVAL '9 hours'" in sql


def test_compare_periods_use_utc_bounds():
    sql = make().compile(
        {
            "target_table": "bms_log",
            "aggregations": [{"field": "uman_time", "function": "SUM", "alias": "무인"}],
            "compare": {
                "field": "collect_datetime",
                "periods": [
                    {"label": "이번달", "from": "2026-10-01", "to": "2026-10-31"},
                    {"label": "지난달", "from": "2026-09-01", "to": "2026-09-30"},
                ],
            },
        }
    )
    assert "TIMESTAMP '2026-10-01 00:00:00' - INTERVAL '9 hours'" in sql
    assert "TIMESTAMP '2026-11-01 00:00:00' - INTERVAL '9 hours'" in sql
    assert "DATE '" not in sql


def test_listing_shows_local_time_and_sorts_by_it():
    sql = make().compile(
        {
            "target_table": "bms_log",
            "columns": ["vehicle_id", "collect_datetime"],
            "distinct": True,
            "order_by": [{"field": "bms_log.collect_datetime", "direction": "desc"}],
        }
    )
    assert sql == (
        'SELECT DISTINCT "vehicle_id", ("collect_datetime" + INTERVAL \'9 hours\') AS "collect_datetime" '
        'FROM "bms_log" ORDER BY "collect_datetime" DESC NULLS LAST;'
    )


@pytest.mark.parametrize(
    "driver,shift",
    [
        ("mysql", "(`collect_datetime` + INTERVAL 9 HOUR)"),
        ("oracle", "(\"collect_datetime\" + INTERVAL '9' HOUR)"),
    ],
)
def test_shift_syntax_per_driver(driver, shift):
    sql = make(driver=driver, time_type="datetime" if driver == "mysql" else "DATE").compile(
        {
            "target_table": "bms_log",
            "aggregations": [{"field": "*", "function": "COUNT", "alias": "건수"}],
            "group_by": [{"field": "collect_datetime", "grain": "day"}],
        }
    )
    assert shift in sql


def test_without_offset_nothing_changes():
    sql = make(offset=None).compile(
        {
            "target_table": "bms_log",
            "aggregations": [{"field": "*", "function": "COUNT", "alias": "건수"}],
            "group_by": [{"field": "collect_datetime", "grain": "day"}],
            "filters": [
                {"field": "collect_datetime", "operator": "greater_or_equal", "value": "2026-10-09"}
            ],
        }
    )
    assert "INTERVAL" not in sql
    assert "DATE '2026-10-09'" in sql


def test_date_only_column_ignores_offset():
    """날짜뿐인 컬럼은 시차가 뜻이 없다"""
    sql = make(time_type="date").compile(
        {
            "target_table": "bms_log",
            "aggregations": [{"field": "*", "function": "COUNT", "alias": "건수"}],
            "group_by": [{"field": "collect_datetime", "grain": "day"}],
        }
    )
    assert "INTERVAL" not in sql


@pytest.mark.parametrize(
    "driver,time_type,expected",
    [
        ("mysql", "datetime", "TIMESTAMP '2026-10-09 00:00:00' - INTERVAL 9 HOUR"),
        ("oracle", "DATE", "TIMESTAMP '2026-10-09 00:00:00' - INTERVAL '9' HOUR"),
    ],
)
def test_filter_offset_syntax_per_driver(driver, time_type, expected):
    sql = make(driver=driver, time_type=time_type).compile(
        {
            "target_table": "bms_log",
            "aggregations": [{"field": "*", "function": "COUNT", "alias": "건수"}],
            "filters": [
                {"field": "collect_datetime", "operator": "greater_or_equal", "value": "2026-10-09"}
            ],
        }
    )
    assert expected in sql


def test_negative_offset_adds_hours():
    """UTC-5 처럼 시차가 음수면 더한다"""
    sql = make(offset=-5).compile(
        {
            "target_table": "bms_log",
            "aggregations": [{"field": "*", "function": "COUNT", "alias": "건수"}],
            "filters": [
                {"field": "collect_datetime", "operator": "greater_or_equal", "value": "2026-10-09"}
            ],
        }
    )
    assert "TIMESTAMP '2026-10-09 00:00:00' + INTERVAL '5 hours'" in sql
