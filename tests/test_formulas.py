"""시스템 계산식 + 새 형식 지표 (테이블 · 디멘젼 · 허용 계산식)"""

import pytest

from compiler import Compiler, expand_formula, mentioned_formulas


def fn(name, *args, **extra):
    return {"fn": name, "args": list(args), **extra}


def span(col):
    return fn("ABS", fn("SUB", fn("MAX", {"col": col}), fn("MIN", {"col": col})))


DAY = {"field": "collect_datetime", "grain": "day"}
MONTH = {"field": "collect_datetime", "grain": "month"}

FORMULAS = [
    {"name": "무인 가동시간", "column": "uman_time", "mode": "forced", "expr": span("uman_time")},
    {"name": "유인 가동시간", "column": "man_time", "mode": "forced", "expr": span("man_time")},
    {
        "name": "무인 비율",
        "mode": "organic",
        "expr": fn("DIV", {"formula": "무인 가동시간"}, fn("ADD", {"formula": "무인 가동시간"}, {"formula": "유인 가동시간"})),
    },
    {"name": "전압 편차", "mode": "organic", "expr": fn("SUB", {"col": "volt_max"}, {"col": "volt_min"})},
]

METRIC = {
    "name": "장비 가동",
    "description": "장비별 하루 가동 기록",
    "table": "bms_log",
    "dimensions": [DAY, "vehicle_id"],
    "fixed_filters": [],
    "formulas": ["무인 가동시간", "유인 가동시간", "무인 비율", "전압 편차"],
}

INNER_DAY = "CAST((\"collect_datetime\" + INTERVAL '9 hours') AS DATE)"


def make(metric=METRIC, formulas=FORMULAS):
    return Compiler(
        {
            "driver": "postgresql",
            "relations": [],
            "metrics": [metric],
            "formulas": formulas,
            "tables": [
                {
                    "name": "bms_log",
                    "columns": [
                        {"name": "collect_datetime", "type": "timestamp", "utc_offset": 9},
                        {"name": "vehicle_id", "type": "varchar(20)"},
                        {"name": "uman_time", "type": "numeric"},
                        {"name": "man_time", "type": "numeric"},
                        {"name": "volt_max", "type": "numeric"},
                        {"name": "volt_min", "type": "numeric"},
                    ],
                }
            ],
        }
    )


def ask(ast, question=None, **kw):
    return make(**kw).compile({"metric": "장비 가동", **ast}, question)


def test_formula_alone_splits_by_metric_dimensions():
    """Q2: 최근 5일 장비별 계산된 무인 가동시간 — 디멘젼(일 × 장비)별"""
    sql = ask({"aggregations": [{"formula": "무인 가동시간"}]}, "장비별 계산된 무인 가동시간")
    assert sql == (
        f'SELECT {INNER_DAY} AS "collect_datetime_day", "vehicle_id", '
        'ABS(MAX("uman_time") - MIN("uman_time")) AS "무인 가동시간" FROM "bms_log" '
        f'GROUP BY {INNER_DAY}, "vehicle_id";'
    )


def test_function_over_formula_aggregates_dimension_values():
    """Q1: 일별 계산된 무인·유인 가동시간 — 장비별 값을 날짜별로 합친다"""
    sql = ask(
        {
            "aggregations": [
                {"formula": "무인 가동시간", "function": "SUM"},
                {"formula": "유인 가동시간", "function": "SUM"},
            ],
            "group_by": [DAY],
        },
        "일별 계산된 무인 가동시간, 계산된 유인 가동시간",
    )
    assert sql == (
        'SELECT "collect_datetime_day", SUM("_m1") AS "무인 가동시간", SUM("_m2") AS "유인 가동시간" '
        f'FROM (SELECT {INNER_DAY} AS "collect_datetime_day", '
        'ABS(MAX("uman_time") - MIN("uman_time")) AS "_m1", ABS(MAX("man_time") - MIN("man_time")) AS "_m2" '
        f'FROM "bms_log" GROUP BY {INNER_DAY}, "vehicle_id") "nested" GROUP BY "collect_datetime_day";'
    )


def test_larger_grain_computes_at_dimension_then_sums():
    sql = ask({"aggregations": [{"formula": "무인 가동시간"}], "group_by": [MONTH, "vehicle_id"]})
    assert 'SUM("_m1") AS "무인 가동시간"' in sql
    assert f'"vehicle_id", {INNER_DAY}) "nested"' in sql


def test_formula_referencing_formulas_is_unfolded_at_dimension():
    sql = ask({"aggregations": [{"formula": "무인 비율"}]})
    assert (
        'ABS(MAX("uman_time") - MIN("uman_time")) * 1.0 / NULLIF((ABS(MAX("uman_time") - MIN("uman_time")) '
        '+ ABS(MAX("man_time") - MIN("man_time"))), 0) AS "무인 비율"' in sql
    )
    assert sql.endswith(f'GROUP BY {INNER_DAY}, "vehicle_id";')


def test_row_formula_in_columns_and_aggregated():
    rows = ask({"columns": ["vehicle_id", {"formula": "전압 편차"}]})
    assert rows == 'SELECT "vehicle_id" AS "vehicle_id", "volt_max" - "volt_min" AS "전압 편차" FROM "bms_log";'
    agg = ask({"aggregations": [{"formula": "전압 편차", "function": "AVG"}], "group_by": ["vehicle_id"]})
    assert 'AVG("volt_max" - "volt_min") AS "전압 편차"' in agg
    with pytest.raises(ValueError, match="집계 함수가 필요"):
        ask({"aggregations": [{"formula": "전압 편차"}]})


def test_plain_columns_need_no_formula():
    sql = ask(
        {"aggregations": [{"field": "uman_time", "function": "MAX"}], "group_by": ["vehicle_id"]},
        "장비별 무인 가동시간 최댓값",
    )
    assert sql == 'SELECT "vehicle_id", MAX("uman_time") AS "max_uman_time" FROM "bms_log" GROUP BY "vehicle_id";'


def test_mentioned_formula_must_be_used():
    with pytest.raises(ValueError, match="계산된 무인 가동시간"):
        ask(
            {"aggregations": [{"field": "uman_time", "function": "SUM"}]},
            "계산된 무인 가동 시간 알려줘",
        )


def test_forced_formula_only_when_mentioned():
    with pytest.raises(ValueError, match="있을 때만"):
        ask({"aggregations": [{"formula": "무인 가동시간"}]}, "무인 가동시간 알려줘")
    # 유기적 계산식은 이름이 없어도 쓸 수 있다
    ask({"aggregations": [{"formula": "전압 편차", "function": "AVG"}]}, "평균 전압 차이")


def test_formula_not_allowed_by_metric():
    metric = {**METRIC, "formulas": ["유인 가동시간"]}
    with pytest.raises(ValueError, match="쓸 수 있는 계산식이 아닙니다"):
        ask({"aggregations": [{"formula": "무인 가동시간"}]}, metric=metric)
    with pytest.raises(ValueError, match="이 계산식을 쓰지 않습니다"):
        ask({"aggregations": [{"formula": "유인 가동시간"}]}, "계산된 무인 가동시간", metric=metric)


def test_mentions_ignore_spaces_and_prefer_longest():
    formulas = FORMULAS + [{"name": "무인", "expr": {"col": "uman_time"}}]
    assert mentioned_formulas("계산된 무인 가동 시간 조회", formulas) == ["무인 가동시간"]
    assert mentioned_formulas("무인 가동시간 조회", formulas) == []


def test_formula_cycle_is_rejected():
    loop = {"a": {"name": "a", "expr": {"formula": "b"}}, "b": {"name": "b", "expr": {"formula": "a"}}}
    with pytest.raises(ValueError, match="서로를 참조"):
        expand_formula({"formula": "a"}, loop)


def test_metric_needs_a_selection():
    with pytest.raises(ValueError, match="무엇을 볼지"):
        ask({})


def test_latest_row_per_question_units_with_all_columns():
    """최근 5일 일별 장비별 가장 마지막 모든 데이터 — GROUP BY 가 아니라 순위를 매겨 1건씩"""
    sql = ask(
        {
            "columns": ["*"],
            "latest": {"order_by": "collect_datetime"},
            "group_by": [DAY, "vehicle_id"],
            "filters": [{"field": "collect_datetime", "operator": "greater_or_equal", "value": "2026-10-09"}],
        }
    )
    assert sql == (
        'SELECT "collect_datetime", "vehicle_id", "uman_time", "man_time", "volt_max", "volt_min" FROM ('
        "SELECT (\"collect_datetime\" + INTERVAL '9 hours') AS \"collect_datetime\", \"vehicle_id\" AS \"vehicle_id\", "
        '"uman_time" AS "uman_time", "man_time" AS "man_time", "volt_max" AS "volt_max", "volt_min" AS "volt_min", '
        f'ROW_NUMBER() OVER (PARTITION BY {INNER_DAY}, "vehicle_id" ORDER BY "collect_datetime" DESC NULLS LAST) AS "_rn" '
        "FROM \"bms_log\" WHERE \"collect_datetime\" >= TIMESTAMP '2026-10-09 00:00:00' - INTERVAL '9 hours'"
        ') "latest" WHERE "_rn" <= 1;'
    )


def test_latest_defaults_to_metric_dimensions_and_validates():
    sql = ask({"columns": ["vehicle_id", "uman_time"], "latest": "collect_datetime"})
    assert f'PARTITION BY {INNER_DAY}, "vehicle_id"' in sql
    first = ask({"columns": ["vehicle_id"], "latest": {"order_by": "collect_datetime", "direction": "asc", "count": 3}})
    assert 'ORDER BY "collect_datetime" ASC) AS "_rn"' in first and '"_rn" <= 3' in first
    with pytest.raises(ValueError, match="행 목록"):
        ask({"aggregations": [{"formula": "전압 편차", "function": "AVG"}], "latest": "collect_datetime"})
    with pytest.raises(ValueError, match="단위가 없습니다"):
        ask({"columns": ["vehicle_id"], "latest": "collect_datetime"}, metric={**METRIC, "dimensions": []})
    with pytest.raises(ValueError, match="결과 컬럼 이름으로만"):
        ask({"columns": ["vehicle_id"], "latest": "collect_datetime", "order_by": [{"field": "uman_time"}]})


def test_sql_with_cte_unfolds_innermost_aggregate():
    from formula_sql import parse_formula_sql

    parsed = parse_formula_sql(
        """WITH daily AS (
            SELECT DATE(collect_datetime + (9 || ' hours')::INTERVAL) AS log_date, vehicle_id,
                   ABS(MAX(uman_time) - MIN(uman_time)) AS unmanned_time, ABS(MAX(eg_time) - MIN(eg_time)) AS full_time
            FROM public.bms_log GROUP BY DATE(collect_datetime + (9 || ' hours')::INTERVAL), vehicle_id)
        SELECT log_date, SUM(full_time - unmanned_time) AS manned FROM daily GROUP BY log_date"""
    )
    assert [f["text"] for f in parsed["formulas"]] == [
        "ABS(MAX(uman_time) - MIN(uman_time))",
        "ABS(MAX(eg_time) - MIN(eg_time))",
    ]
    assert parsed["dimensions"] == [DAY, "vehicle_id"] and parsed["table"] == "bms_log"
    assert parsed["outer"][0] == "manned: SUM([full_time] - [unmanned_time])"


def test_sql_with_two_statements_is_rejected_plainly():
    from formula_sql import parse_formula_sql

    with pytest.raises(ValueError, match="SQL 문이 2개"):
        parse_formula_sql("SELECT MAX(a) FROM t;\nSELECT MIN(a) FROM t;")


def test_formula_alone_sums_dimensions_the_question_drops():
    """최근 5일간 일별 계산된 무인·유인 가동시간 — function 없이 일별만 물어도 장비별로 계산한 뒤 날짜별로 합친다"""
    sql = ask(
        {"aggregations": [{"formula": "무인 가동시간"}, {"formula": "유인 가동시간"}], "group_by": [DAY]},
        "일별 계산된 무인 가동시간, 계산된 유인 가동시간",
    )
    assert sql == ask(
        {
            "aggregations": [
                {"formula": "무인 가동시간", "function": "SUM"},
                {"formula": "유인 가동시간", "function": "SUM"},
            ],
            "group_by": [DAY],
        }
    )
    # 디멘젼을 다 묶으면 그대로, rollup NONE 이면 질문의 묶음으로 다시 계산
    assert '"nested"' not in ask({"aggregations": [{"formula": "무인 가동시간"}], "group_by": [DAY, "vehicle_id"]})
    again = [{**f, "rollup": "NONE"} if f["name"] == "무인 가동시간" else f for f in FORMULAS]
    assert ask({"aggregations": [{"formula": "무인 가동시간"}], "group_by": ["vehicle_id"]}, formulas=again) == (
        'SELECT "vehicle_id", ABS(MAX("uman_time") - MIN("uman_time")) AS "무인 가동시간" '
        'FROM "bms_log" GROUP BY "vehicle_id";'
    )
