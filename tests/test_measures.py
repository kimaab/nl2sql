"""측정값 식 트리 — 함수를 겹쳐 만든 지표 출력 컬럼"""

import pytest

from compiler import Compiler, measure_columns, measure_text

COLUMNS = [
    ("lawd_cd", "varchar(5)"),
    ("legal_dong", "text"),
    ("apartment_name", "text"),
    ("exclusive_area_m2", "numeric"),
    ("deal_date", "date"),
    ("deal_ym", "varchar(6)"),
    ("row_no", "integer"),
    ("deal_amount_manwon", "bigint"),
    ("is_cancelled", "boolean"),
]
SERIES = {
    "partition_by": ["lawd_cd", "legal_dong", "apartment_name", "exclusive_area_m2"],
    "order_by": ["deal_date", "row_no"],
}
AMT = {"col": "deal_amount_manwon"}


def fn(name, *args, **extra):
    return {"fn": name, "args": list(args), **extra}


def metric(name, kind, measures, **extra):
    return {
        "name": name,
        "kind": kind,
        "table": "apartment_trade",
        "fixed_filters": [],
        "measures": [{"name": n, "expr": e} for n, e in measures],
        **extra,
    }


def make(*metrics, driver="postgresql"):
    return Compiler(
        {
            "driver": driver,
            "relations": [],
            "metrics": list(metrics),
            "tables": [
                {
                    "name": "apartment_trade",
                    "columns": [{"name": n, "type": t} for n, t in COLUMNS],
                }
            ],
        }
    )


def error(compiler, ast) -> str:
    with pytest.raises(ValueError) as info:
        compiler.compile(ast)
    return str(info.value)


CHANGE = metric(
    "거래 변동",
    "projection",
    [
        ("아파트명", {"col": "apartment_name"}),
        ("이전거래금액", fn("PREV", AMT)),
        ("차이금액", fn("DELTA", AMT)),
        ("변동폭", fn("ABS", fn("DELTA", AMT))),
        ("평균거래금액", fn("ROUND", fn("AVG", AMT), {"num": 0})),
    ],
    series=SERIES,
    fixed_filters=[
        {"field": "is_cancelled", "operator": "equals", "value": False},
        {"field": "legal_dong", "operator": "equals", "value": "상동"},
    ],
)


def test_projection_with_previous_row_and_window_average():
    out = make(CHANGE).compile(
        {
            "metric": "거래 변동",
            "filters": [
                {
                    "field": "deal_date",
                    "operator": "greater_or_equal",
                    "value": "2026-01-01",
                }
            ],
            "order_by": [{"field": "변동폭", "direction": "desc"}],
            "limit": 1,
        }
    )
    assert out == (
        'SELECT "apartment_name" AS "아파트명", "lag_1_deal_amount_manwon" AS "이전거래금액", '
        '"deal_amount_manwon" - "lag_1_deal_amount_manwon" AS "차이금액", '
        'ABS("deal_amount_manwon" - "lag_1_deal_amount_manwon") AS "변동폭", '
        'ROUND(AVG("deal_amount_manwon") OVER (), 0) AS "평균거래금액" '
        'FROM (SELECT "apartment_trade".*, LAG("deal_amount_manwon") OVER (PARTITION BY "lawd_cd", "legal_dong", '
        '"apartment_name", "exclusive_area_m2" ORDER BY "deal_date", "row_no", "deal_amount_manwon") '
        'AS "lag_1_deal_amount_manwon" FROM "apartment_trade" WHERE "is_cancelled" = FALSE AND "legal_dong" = \'상동\') '
        '"apartment_trade" WHERE "deal_date" >= DATE \'2026-01-01\' ORDER BY "변동폭" DESC NULLS LAST LIMIT 1;'
    )


def test_question_period_stays_outside_lag_subquery():
    """기간의 첫 거래도 기간 밖의 직전 거래와 비교해야 한다."""
    out = make(CHANGE).compile(
        {
            "metric": "거래 변동",
            "filters": [
                {
                    "field": "deal_date",
                    "operator": "greater_or_equal",
                    "value": "2026-01-01",
                }
            ],
        }
    )
    inner, outer = out.split(') "apartment_trade" ')
    assert 'deal_date" >=' not in inner and 'deal_date" >=' in outer


def test_projection_window_by_partitions():
    m = metric(
        "단지 평균 대비",
        "projection",
        [
            ("금액", AMT),
            ("단지평균대비", fn("SUB", AMT, fn("AVG", AMT, by=["apartment_name"]))),
        ],
    )
    out = make(m).compile({"metric": "단지 평균 대비"})
    assert (
        '"deal_amount_manwon" - AVG("deal_amount_manwon") OVER (PARTITION BY "apartment_name") AS "단지평균대비"'
        in out
    )


def test_aggregate_with_several_measures_and_combined_functions():
    m = metric(
        "가격 통계",
        "aggregate",
        [
            ("가격폭", fn("SUB", fn("MAX", AMT), fn("MIN", AMT))),
            ("평균", fn("ROUND", fn("AVG", AMT), {"num": 0})),
            (
                "건당면적가",
                fn("DIV", fn("SUM", AMT), fn("SUM", {"col": "exclusive_area_m2"})),
            ),
            ("거래건수", fn("COALESCE", fn("COUNT"), {"num": 0})),
        ],
    )
    out = make(m).compile(
        {
            "metric": "가격 통계",
            "group_by": ["legal_dong"],
            "order_by": [{"field": "가격폭", "direction": "desc"}],
        }
    )
    assert out == (
        'SELECT "legal_dong", MAX("deal_amount_manwon") - MIN("deal_amount_manwon") AS "가격폭", '
        'ROUND(AVG("deal_amount_manwon"), 0) AS "평균", '
        'SUM("deal_amount_manwon") * 1.0 / NULLIF(SUM("exclusive_area_m2"), 0) AS "건당면적가", '
        'COALESCE(COUNT(*), 0) AS "거래건수" '
        'FROM "apartment_trade" GROUP BY "legal_dong" ORDER BY "가격폭" DESC NULLS LAST;'
    )


def test_arithmetic_keeps_grouping():
    m = metric(
        "식",
        "aggregate",
        [("x", fn("MUL", fn("SUB", fn("MAX", AMT), fn("MIN", AMT)), {"num": 2}))],
    )
    assert (
        'SELECT (MAX("deal_amount_manwon") - MIN("deal_amount_manwon")) * 2 AS "x"'
        in make(m).compile({"metric": "식"})
    )


def test_nested_aggregate_groups_twice():
    m = metric(
        "월평균 거래건수",
        "aggregate",
        [("월평균 거래건수", fn("AVG", fn("COUNT", by=["deal_ym"])))],
        fixed_filters=[{"field": "is_cancelled", "operator": "equals", "value": False}],
    )
    out = make(m).compile(
        {
            "metric": "월평균 거래건수",
            "group_by": ["legal_dong"],
            "order_by": [{"field": "월평균 거래건수", "direction": "desc"}],
            "limit": 5,
        }
    )
    assert out == (
        'SELECT "legal_dong", AVG("_m1") AS "월평균 거래건수" FROM (SELECT "legal_dong" AS "legal_dong", COUNT(*) AS "_m1" '
        'FROM "apartment_trade" WHERE "is_cancelled" = FALSE GROUP BY "legal_dong", "deal_ym") "nested" '
        'GROUP BY "legal_dong" ORDER BY "월평균 거래건수" DESC NULLS LAST LIMIT 5;'
    )


def test_nested_aggregate_with_grain_and_no_group():
    m = metric(
        "최대 월거래액",
        "aggregate",
        [("최대 월거래액", fn("MAX", fn("SUM", AMT, by=["deal_ym"])))],
    )
    assert make(m).compile({"metric": "최대 월거래액"}) == (
        'SELECT MAX("_m1") AS "최대 월거래액" FROM (SELECT SUM("deal_amount_manwon") AS "_m1" '
        'FROM "apartment_trade" GROUP BY "deal_ym") "nested";'
    )
    out = make(m).compile(
        {
            "metric": "최대 월거래액",
            "group_by": [{"field": "deal_date", "grain": "year"}],
        }
    )
    assert 'SELECT "deal_date_year", MAX("_m1")' in out
    assert 'CAST(DATE_TRUNC(\'year\', "deal_date") AS DATE) AS "deal_date_year"' in out


def test_compare_wraps_compound_measure():
    m = metric(
        "가격폭", "aggregate", [("가격폭", fn("SUB", fn("MAX", AMT), fn("MIN", AMT)))]
    )
    out = make(m).compile(
        {
            "metric": "가격폭",
            "compare": {
                "field": "deal_date",
                "periods": [
                    {"label": "올해", "from": "2026-01-01", "to": "2026-12-31"},
                    {"label": "작년", "from": "2025-01-01", "to": "2025-12-31"},
                ],
            },
        }
    )
    rate = out.split('AS "가격폭_작년", ')[1]
    assert rate.startswith("((MAX(") and ") - (MAX(" in rate


def test_several_measure_metrics_together_keep_their_filters():
    a = metric(
        "상동 평균",
        "aggregate",
        [("상동 평균", fn("AVG", AMT))],
        fixed_filters=[{"field": "legal_dong", "operator": "equals", "value": "상동"}],
    )
    b = metric(
        "가격범위", "aggregate", [("최고", fn("MAX", AMT)), ("최저", fn("MIN", AMT))]
    )
    out = make(a, b).compile({"metric": ["상동 평균", "가격범위"]})
    assert out == (
        'SELECT AVG(CASE WHEN "legal_dong" = \'상동\' THEN "deal_amount_manwon" END) AS "상동 평균", '
        'MAX("deal_amount_manwon") AS "최고", MIN("deal_amount_manwon") AS "최저" FROM "apartment_trade";'
    )


def test_derived_metric_uses_single_measure_component():
    total = metric(
        "거래액", "aggregate", [("거래액", fn("ROUND", fn("SUM", AMT), {"num": 0}))]
    )
    derived = {
        "name": "건당",
        "kind": "derived",
        "table": "apartment_trade",
        "fixed_filters": [],
        "expression": "[거래액] / 10",
    }
    assert make(total, derived).compile({"metric": "건당"}) == (
        'SELECT ROUND(SUM("deal_amount_manwon"), 0) * 1.0 / NULLIF(10, 0) AS "건당" FROM "apartment_trade";'
    )


def test_previous_row_inside_aggregate():
    m = metric(
        "최대 상승",
        "aggregate",
        [("최대 상승", fn("MAX", fn("DELTA", AMT)))],
        series=SERIES,
    )
    out = make(m).compile({"metric": "최대 상승"})
    assert out.startswith(
        'SELECT MAX("deal_amount_manwon" - "lag_1_deal_amount_manwon") AS "최대 상승" FROM (SELECT'
    )


@pytest.mark.parametrize(
    "kind,expr,msg",
    [
        ("aggregate", AMT, "집계되지 않은 값"),
        ("aggregate", fn("SUB", fn("MAX", AMT), AMT), "집계되지 않은 값"),
        ("aggregate", fn("AVG", fn("SUM", AMT)), "BY(묶음 기준)"),
        (
            "aggregate",
            fn("SUB", fn("AVG", fn("COUNT", by=["deal_ym"])), fn("MAX", AMT)),
            "BY 집계 안에서만",
        ),
        ("aggregate", fn("SUM", {"num": 1}), "컬럼이 없습니다"),
        ("aggregate", fn("ROUND", fn("AVG", AMT), AMT), "자릿수"),
        ("aggregate", fn("SUB", fn("MAX", AMT)), "2~8개"),
        ("aggregate", fn("MEDIAN", AMT), "쓸 수 없는 함수"),
        ("aggregate", fn("MAX", fn("PREV", AMT)), "구분 컬럼"),
        ("projection", fn("AVG", fn("SUM", AMT)), "겹칠 수 없습니다"),
        ("projection", fn("DELTA_SUM", AMT), "집계형 지표에서만"),
        ("projection", fn("PREV", fn("ABS", AMT)), "컬럼 하나"),
        ("projection", {"col": "nope"}, "측정값의 컬럼을 찾을 수 없습니다"),
    ],
)
def test_invalid_combinations(kind, expr, msg):
    assert msg in error(make(metric("m", kind, [("m", expr)])), {"metric": "m"})


def test_nested_needs_one_by():
    m = metric(
        "m",
        "aggregate",
        [
            ("a", fn("AVG", fn("COUNT", by=["deal_ym"]))),
            ("b", fn("MAX", fn("COUNT", by=["legal_dong"]))),
        ],
    )
    assert "하나로 같아야" in error(make(m), {"metric": "m"})


def test_nested_rejects_compare_and_multi_metric():
    m = metric("m", "aggregate", [("m", fn("AVG", fn("COUNT", by=["deal_ym"])))])
    other = metric("o", "aggregate", [("o", fn("MAX", AMT))])
    assert "compare" in error(
        make(m), {"metric": "m", "compare": {"field": "deal_date", "periods": []}}
    )
    assert "함께 쓸 수 없습니다" in error(make(m, other), {"metric": ["o", "m"]})


def test_oracle_fetch_first_on_nested():
    m = metric("m", "aggregate", [("m", fn("AVG", fn("COUNT", by=["deal_ym"])))])
    assert (
        make(m, driver="oracle")
        .compile({"metric": "m", "limit": 3})
        .endswith('"nested" FETCH FIRST 3 ROWS ONLY;')
    )


def test_measure_text_and_columns():
    expr = fn("ROUND", fn("AVG", fn("COUNT", by=["deal_ym"])), {"num": 1})
    assert measure_text(expr) == "ROUND(AVG(COUNT(* BY deal_ym)), 1)"
    assert (
        measure_text(fn("SUB", fn("MAX", AMT), fn("ADD", AMT, {"num": 1})))
        == "MAX(deal_amount_manwon) - (deal_amount_manwon + 1)"
    )
    assert measure_columns(fn("SUM", AMT, by=["deal_ym"])) == [
        "deal_ym",
        "deal_amount_manwon",
    ]


def test_input_names_measures_and_drops_unused_series():
    from models import MetricInput

    data = MetricInput(
        name="거래액",
        table_name="t",
        kind="aggregate",
        measures=[{"expr": fn("SUM", AMT)}],
        series={"partition_by": "a", "order_by": "b"},
    )
    assert (
        data.measures[0].name == "거래액"
        and data.series is None
        and data.agg_function is None
    )
    rows = MetricInput(
        name="목록",
        table_name="t",
        kind="projection",
        measures=[
            {"expr": {"col": "t.apartment_name"}},
            {"name": "이전", "expr": fn("PREV", AMT)},
        ],
        series={"partition_by": ["a", "b"], "order_by": ["c"]},
    )
    assert [m.name for m in rows.measures] == ["apartment_name", "이전"]
    assert rows.series.partition_by == ["a", "b"] and rows.series.order_by == "c"
    with pytest.raises(ValueError, match="구분 컬럼과 순서 컬럼"):
        MetricInput(
            name="x",
            table_name="t",
            kind="projection",
            measures=[{"expr": fn("PREV", AMT)}],
        )
    with pytest.raises(ValueError, match="각각 이름"):
        MetricInput(
            name="x",
            table_name="t",
            measures=[{"expr": fn("SUM", AMT)}, {"expr": fn("MAX", AMT)}],
        )


def test_by_on_expression_computes_whole_expression_inside():
    """SUM(ABS(MAX(x) - MIN(x)) BY 묶음) — 식 전체가 안쪽 질의의 컬럼 하나가 된다"""
    span = fn("ABS", fn("SUB", fn("MAX", AMT), fn("MIN", AMT)), by=["apartment_name"])
    out = make(metric("가격폭 합", "aggregate", [("가격폭 합", fn("SUM", span))])).compile(
        {"metric": "가격폭 합", "group_by": ["legal_dong"]}
    )
    assert out == (
        'SELECT "legal_dong", SUM("_m1") AS "가격폭 합" FROM (SELECT "legal_dong" AS "legal_dong", '
        'ABS(MAX("deal_amount_manwon") - MIN("deal_amount_manwon")) AS "_m1" FROM "apartment_trade" '
        'GROUP BY "legal_dong", "apartment_name") "nested" GROUP BY "legal_dong";'
    )
    assert measure_text(fn("SUM", span)) == (
        "SUM(ABS(MAX(deal_amount_manwon) - MIN(deal_amount_manwon) BY apartment_name))"
    )


@pytest.mark.parametrize(
    "expr,message",
    [
        (fn("SUM", fn("ABS", AMT, by=["apartment_name"])), "집계 함수나 집계를 품은 식"),
        (
            fn("SUM", fn("SUB", fn("MAX", AMT), AMT, by=["apartment_name"])),
            "모두 집계 함수 안",
        ),
    ],
)
def test_by_expression_errors(expr, message):
    compiler = make(metric("m", "aggregate", [("m", expr)]))
    assert message in error(compiler, {"metric": "m"})


def test_arithmetic_chains_more_than_two_values():
    """빼기·더하기는 값을 이어 붙인다 — a - b - c (왼쪽부터)"""
    span = fn("SUB", fn("MAX", AMT), fn("MIN", AMT), fn("AVG", AMT))
    out = make(metric("m", "aggregate", [("m", span)])).compile({"metric": "m"})
    assert out == (
        'SELECT MAX("deal_amount_manwon") - MIN("deal_amount_manwon") - AVG("deal_amount_manwon") AS "m" '
        'FROM "apartment_trade";'
    )
    assert measure_text(span) == (
        "MAX(deal_amount_manwon) - MIN(deal_amount_manwon) - AVG(deal_amount_manwon)"
    )
    too_many = fn("DIV", AMT, AMT, AMT)
    assert "두 개" in error(make(metric("d", "projection", [("d", too_many)])), {"metric": "d"})


def test_by_outside_aggregate_splits_result():
    """바깥 집계 없는 BY 는 결과를 그 기준별로 나눈다 — 질문의 묶음에 더해진다 (daily_stats 모양)"""
    span = fn(
        "ABS",
        fn("SUB", fn("MAX", AMT), fn("MIN", AMT)),
        by=[{"field": "deal_date", "grain": "day"}, "apartment_name"],
    )
    compiler = make(metric("가격폭", "aggregate", [("가격폭", span)]))
    assert compiler.compile({"metric": "가격폭"}) == (
        'SELECT CAST("deal_date" AS DATE) AS "deal_date_day", "apartment_name", '
        'ABS(MAX("deal_amount_manwon") - MIN("deal_amount_manwon")) AS "가격폭" '
        'FROM "apartment_trade" GROUP BY CAST("deal_date" AS DATE), "apartment_name";'
    )
    # 질문이 같은 기준으로 묶어도 한 번만
    same = compiler.compile({"metric": "가격폭", "group_by": ["apartment_name"]})
    assert same.count('"apartment_name"') == 2  # SELECT 와 GROUP BY 에 한 번씩


def test_inner_by_accepts_date_grain():
    """안쪽 BY 기준에도 날짜 단위를 쓴다 — SUM(COUNT(*) BY 월) 처럼"""
    expr = fn("AVG", fn("COUNT", by=[{"field": "deal_date", "grain": "month"}]))
    out = make(metric("월평균 건수", "aggregate", [("월평균 건수", expr)])).compile({"metric": "월평균 건수"})
    assert "GROUP BY CAST(DATE_TRUNC('month', \"deal_date\") AS DATE)" in out
    assert measure_text(expr) == "AVG(COUNT(* BY deal_date(month)))"
