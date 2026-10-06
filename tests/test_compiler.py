import pytest

from compiler import Compiler, compile_formula, normalize_ast, resolve_relative


def sql(compiler, ast):
    return compiler.compile(ast)


def error(compiler, ast) -> str:
    with pytest.raises(ValueError) as info:
        compiler.compile(ast)
    return str(info.value)


# ------------------------------------------------------------------ 기존 동작 (spec 15절)

def test_metric_literal_filter_and_question_filter_both_applied(compiler):
    out = sql(compiler, {"metric": "매출", "filters": [{"field": "PAY_METHOD", "operator": "equals", "value": "CARD"}]})
    assert out == (
        'SELECT SUM("TOTAL_AMT") AS "매출" FROM "TB_ORDER" '
        "WHERE \"ORDER_STATUS\" != '99' AND \"PAY_METHOD\" = 'CARD';"
    )


def test_group_by_goes_first(compiler):
    out = sql(compiler, {"target_table": "TB_ORDER", "aggregations": [{"field": "*", "function": "COUNT"}],
                         "group_by": ["PAY_METHOD"]})
    assert out == 'SELECT "PAY_METHOD", COUNT(*) AS "count_*" FROM "TB_ORDER" GROUP BY "PAY_METHOD";'


def test_reserved_word_column_is_quoted(contract):
    contract["tables"][0]["columns"].append({"name": "order", "type": "integer"})
    out = Compiler(contract).compile({"target_table": "TB_MEMBER", "group_by": ["order"]})
    assert out == 'SELECT "order" FROM "TB_MEMBER" GROUP BY "order";'


def test_backslash_rejected_on_mysql_only(compiler_for):
    ast = {"target_table": "TB_MEMBER", "filters": [{"field": "MEMBER_NM", "operator": "equals", "value": "a\\b"}]}
    with pytest.raises(ValueError, match="백슬래시"):
        compiler_for("mysql").compile(ast)
    assert "'a\\b'" in compiler_for("postgresql").compile(ast)


@pytest.mark.parametrize("alias", ['a"b', "x" * 65])
def test_bad_alias_rejected(compiler, alias):
    msg = error(compiler, {"target_table": "TB_ORDER", "aggregations": [{"field": "*", "function": "COUNT", "alias": alias}]})
    assert "별칭" in msg


def test_unknown_key_lists_allowed_keys(compiler):
    msg = error(compiler, {"target_table": "TB_ORDER", "offset": 10})
    assert "OFFSET" in msg and "쓸 수 있는 키" in msg and "order_by" in msg


def test_projection_metric_rejects_group_by(compiler):
    msg = error(compiler, {"metric": "주문상세", "group_by": ["ORDER_ID"],
                           "filters": [{"field": "MEMBER_ID", "operator": "equals", "value": "m1"}]})
    assert "group_by" in msg and "주문상세" in msg


def test_datetime_column_with_date_end_includes_whole_day(compiler):
    out = sql(compiler, {"target_table": "TB_ORDER", "filters": [
        {"field": "ORDER_DTM", "operator": "less_or_equal", "value": "2026-07-31"}]})
    assert "\"ORDER_DTM\" < DATE '2026-08-01'" in out


def test_datetime_equals_date_rejected(compiler):
    assert "하루를 고를 수 없습니다" in error(compiler, {"target_table": "TB_ORDER", "filters": [
        {"field": "ORDER_DTM", "operator": "equals", "value": "2026-07-31"}]})


def test_relative_metric_filter_steps_back_when_question_touches_column(compiler):
    week_ago = resolve_relative("-7d", "x")
    plain = sql(compiler, {"metric": "최근주문"})
    assert f"\"ORDER_DTM\" >= DATE '{week_ago}'" in plain
    asked = sql(compiler, {"metric": "최근주문", "filters": [
        {"field": "ORDER_DTM", "operator": "greater_or_equal", "value": "2026-07-01"}]})
    assert week_ago not in asked and "DATE '2026-07-01'" in asked


# ------------------------------------------------------------------ 조인 지표 (F-02)

def test_join_metric_qualifies_columns_and_accepts_short_question_field(compiler):
    expected = (
        'SELECT "TB_ORDER"."ORDER_ID", "TB_ORDER"."ORDER_DTM", "TB_ORDER_ITEM"."PRODUCT_ID", "TB_ORDER_ITEM"."QTY" '
        'FROM "TB_ORDER" INNER JOIN "TB_ORDER_ITEM" ON "TB_ORDER"."ORDER_ID" = "TB_ORDER_ITEM"."ORDER_ID" '
        "WHERE \"TB_ORDER\".\"MEMBER_ID\" = 'm1';"
    )
    for field in ("MEMBER_ID", "TB_ORDER.MEMBER_ID", "tb_order.member_id"):
        assert sql(compiler, {"metric": "주문상세", "filters": [
            {"field": field, "operator": "equals", "value": "m1"}]}) == expected


def test_join_metric_missing_question_filter_is_rejected(compiler):
    assert "질문에서 받습니다" in error(compiler, {"metric": "주문상세"})


def test_metric_rejects_extra_selection_keys(compiler):
    assert "지표를 쓸 때는" in error(compiler, {"metric": "매출", "aggregations": [{"field": "*", "function": "COUNT"}]})


# ------------------------------------------------------------------ A-01 별칭 정규화

def test_operator_and_key_aliases_are_normalized(compiler):
    out = sql(compiler, {"table": "TB_ORDER", "where": [
        {"column": "ORDER_DTM", "op": "greater_than_or_equal", "value": "2026-07-01"},
        {"field": "TOTAL_AMT", "operator": ">=", "value": 1000},
    ], "aggregations": [{"column": "*", "func": "count"}]})
    assert "\"ORDER_DTM\" >= DATE '2026-07-01'" in out and '"TOTAL_AMT" >= 1000' in out


def test_normalize_keeps_unknown_keys_for_rejection():
    assert normalize_ast({"foo": 1, "table": "T"}) == {"foo": 1, "target_table": "T"}


def test_normalize_does_not_override_canonical_key():
    assert normalize_ast({"target_table": "A", "table": "B"}) == {"target_table": "A", "table": "B"}


# ------------------------------------------------------------------ Q-01 정렬·개수 제한, 목록 조회

def test_columns_order_by_limit_postgres(compiler):
    out = sql(compiler, {"target_table": "TB_ORDER", "columns": ["ORDER_ID", "ORDER_DTM"],
                         "order_by": [{"field": "ORDER_DTM", "direction": "desc"}], "limit": 10})
    assert out == 'SELECT "ORDER_ID", "ORDER_DTM" FROM "TB_ORDER" ORDER BY "ORDER_DTM" DESC LIMIT 10;'


def test_limit_on_oracle_uses_fetch_first(compiler_for):
    out = compiler_for("oracle").compile({"target_table": "TB_ORDER", "columns": ["ORDER_ID"], "limit": 5})
    assert out.endswith("FETCH FIRST 5 ROWS ONLY;")


def test_order_by_aggregate_alias_top_n(compiler):
    out = sql(compiler, {"target_table": "TB_ORDER", "group_by": ["MEMBER_ID"],
                         "aggregations": [{"field": "TOTAL_AMT", "function": "SUM", "alias": "총액"}],
                         "order_by": [{"field": "총액", "direction": "desc"}], "limit": 5})
    assert out.endswith('GROUP BY "MEMBER_ID" ORDER BY "총액" DESC LIMIT 5;')


def test_order_by_metric_name(compiler):
    out = sql(compiler, {"metric": "매출", "group_by": ["PAY_METHOD"], "order_by": ["매출 desc"]})
    assert out.endswith('ORDER BY "매출" DESC;')


def test_order_by_non_grouped_column_in_aggregate_rejected(compiler):
    msg = error(compiler, {"target_table": "TB_ORDER", "group_by": ["MEMBER_ID"],
                           "aggregations": [{"field": "*", "function": "COUNT"}],
                           "order_by": [{"field": "ORDER_DTM"}]})
    assert "group_by 컬럼이나 집계 별칭" in msg


@pytest.mark.parametrize("limit", [0, 1001, "ten", True])
def test_bad_limit_rejected(compiler, limit):
    assert "limit" in error(compiler, {"target_table": "TB_ORDER", "columns": ["ORDER_ID"], "limit": limit})


def test_columns_and_aggregations_together_rejected(compiler):
    assert "함께 쓸 수 없습니다" in error(compiler, {"target_table": "TB_ORDER", "columns": ["ORDER_ID"],
                                                 "aggregations": [{"field": "*", "function": "COUNT"}]})


# ------------------------------------------------------------------ Q-02 기간 단위 그룹

@pytest.mark.parametrize("driver,expr", [
    ("postgresql", "DATE_TRUNC('month', \"ORDER_DTM\")"),
    ("oracle", "TRUNC(\"ORDER_DTM\", 'MM')"),
    ("mysql", "DATE_FORMAT(`ORDER_DTM`, '%Y-%m-01')"),
])
def test_group_by_month_per_driver(compiler_for, driver, expr):
    out = compiler_for(driver).compile({"metric": "주문수", "group_by": [{"field": "ORDER_DTM", "grain": "month"}],
                                        "order_by": [{"field": "ORDER_DTM_month"}]})
    q = "`" if driver == "mysql" else '"'
    assert out.startswith(f"SELECT {expr} AS {q}ORDER_DTM_month{q}, COUNT(*) AS {q}주문수{q}")
    assert f"GROUP BY {expr} ORDER BY {q}ORDER_DTM_month{q} ASC" in out


def test_grain_on_non_date_column_rejected(compiler):
    assert "날짜 컬럼이 아니라" in error(compiler, {"metric": "주문수", "group_by": [{"field": "PAY_METHOD", "grain": "month"}]})


def test_unknown_grain_rejected(compiler):
    assert "grain" in error(compiler, {"metric": "주문수", "group_by": [{"field": "ORDER_DTM", "grain": "hour"}]})


# ------------------------------------------------------------------ Q-03 / Q-04 조건 확장

def test_null_and_like_operators(compiler):
    out = sql(compiler, {"target_table": "TB_MEMBER", "columns": ["MEMBER_ID"], "filters": [
        {"field": "JOIN_DT", "operator": "is_not_null"},
        {"field": "MEMBER_NM", "operator": "contains", "value": "50%_off!"},
    ]})
    assert "\"JOIN_DT\" IS NOT NULL" in out
    assert "\"MEMBER_NM\" LIKE '%50!%!_off!!%' ESCAPE '!'" in out


def test_like_on_numeric_rejected(compiler):
    assert "문자 컬럼이 아니라" in error(compiler, {"target_table": "TB_ORDER", "filters": [
        {"field": "TOTAL_AMT", "operator": "starts_with", "value": "1"}]})


def test_any_of_renders_parenthesised_or(compiler):
    out = sql(compiler, {"target_table": "TB_ORDER", "columns": ["ORDER_ID"], "filters": [
        {"any_of": [{"field": "PAY_METHOD", "operator": "equals", "value": "CARD"},
                    {"all_of": [{"field": "PAY_METHOD", "operator": "equals", "value": "BANK"},
                                {"field": "TOTAL_AMT", "operator": "greater_than", "value": 100}]}]},
    ]})
    assert out.endswith("WHERE (\"PAY_METHOD\" = 'CARD' OR (\"PAY_METHOD\" = 'BANK' AND \"TOTAL_AMT\" > 100));")


def test_question_filter_satisfied_inside_any_of(compiler):
    out = sql(compiler, {"metric": "주문상세", "filters": [{"any_of": [
        {"field": "MEMBER_ID", "operator": "equals", "value": "a"},
        {"field": "MEMBER_ID", "operator": "equals", "value": "b"}]}]})
    assert "(\"TB_ORDER\".\"MEMBER_ID\" = 'a' OR \"TB_ORDER\".\"MEMBER_ID\" = 'b')" in out


# ------------------------------------------------------------------ Q-05 HAVING, Q-06 DISTINCT

def test_having_by_function_and_alias(compiler):
    out = sql(compiler, {"target_table": "TB_ORDER", "group_by": ["MEMBER_ID"],
                         "aggregations": [{"field": "*", "function": "COUNT", "alias": "주문건수"}],
                         "having": [{"alias": "주문건수", "operator": "greater_or_equal", "value": 3},
                                    {"field": "TOTAL_AMT", "function": "SUM", "operator": "greater_than", "value": "1000"}]})
    assert out.endswith('GROUP BY "MEMBER_ID" HAVING COUNT(*) >= 3 AND SUM("TOTAL_AMT") > 1000;')


def test_having_needs_aggregation(compiler):
    assert "having" in error(compiler, {"target_table": "TB_ORDER", "columns": ["ORDER_ID"],
                                        "having": [{"alias": "x", "operator": "equals", "value": 1}]})


def test_having_value_must_be_number(compiler):
    assert "숫자" in error(compiler, {"metric": "주문수", "group_by": ["MEMBER_ID"],
                                    "having": [{"alias": "주문수", "operator": "greater_than", "value": "many"}]})


def test_distinct_and_count_distinct(compiler):
    assert sql(compiler, {"target_table": "TB_ORDER", "columns": ["MEMBER_ID"], "distinct": True}) == \
        'SELECT DISTINCT "MEMBER_ID" FROM "TB_ORDER";'
    assert sql(compiler, {"target_table": "TB_ORDER", "aggregations": [
        {"field": "MEMBER_ID", "function": "count_distinct", "alias": "구매회원수"}]}) == \
        'SELECT COUNT(DISTINCT "MEMBER_ID") AS "구매회원수" FROM "TB_ORDER";'


def test_distinct_with_aggregation_rejected(compiler):
    assert "COUNT_DISTINCT" in error(compiler, {"metric": "주문수", "distinct": True})


# ------------------------------------------------------------------ S-01 관계 기반 조인

def test_model_join_uses_registered_relation(compiler):
    out = sql(compiler, {"target_table": "TB_ORDER", "joins": ["TB_MEMBER"],
                         "columns": ["ORDER_ID", "TB_MEMBER.MEMBER_NM"],
                         "filters": [{"field": "TB_MEMBER.GRADE_CD", "operator": "equals", "value": "VIP"}]})
    assert out == (
        'SELECT "TB_ORDER"."ORDER_ID", "TB_MEMBER"."MEMBER_NM" FROM "TB_ORDER" '
        'INNER JOIN "TB_MEMBER" ON "TB_ORDER"."MEMBER_ID" = "TB_MEMBER"."MEMBER_ID" '
        "WHERE \"TB_MEMBER\".\"GRADE_CD\" = 'G3';"
    )


def test_model_join_chain_and_left_type(compiler):
    out = sql(compiler, {"target_table": "TB_ORDER_ITEM", "joins": [{"table": "TB_PRODUCT", "type": "left"}, "TB_ORDER"],
                         "group_by": ["TB_PRODUCT.CATEGORY_CD"],
                         "aggregations": [{"field": "QTY", "function": "SUM"}]})
    assert 'LEFT JOIN "TB_PRODUCT" ON "TB_ORDER_ITEM"."PRODUCT_ID" = "TB_PRODUCT"."PRODUCT_ID"' in out
    assert 'INNER JOIN "TB_ORDER" ON "TB_ORDER_ITEM"."ORDER_ID" = "TB_ORDER"."ORDER_ID"' in out


def test_model_join_without_relation_rejected(compiler):
    msg = error(compiler, {"target_table": "TB_MEMBER", "joins": ["TB_PRODUCT"]})
    assert "등록된 관계가 없습니다" in msg and "TB_ORDER" in msg


def test_model_join_cannot_carry_on(compiler):
    assert "자동으로 정해집니다" in error(compiler, {"target_table": "TB_ORDER", "joins": [
        {"table": "TB_MEMBER", "on": [{"left": "a", "right": "b"}]}]})


def test_ambiguous_relation_rejected(contract):
    contract["relations"].append({"constraint": "FK_ORDER_REFERRER", "left_table": "TB_ORDER",
                                  "left_column": "PAY_METHOD", "right_table": "TB_MEMBER", "right_column": "MEMBER_ID"})
    with pytest.raises(ValueError, match="여러 개"):
        Compiler(contract).compile({"target_table": "TB_ORDER", "joins": ["TB_MEMBER"]})


# ------------------------------------------------------------------ S-02 파생 지표, Q-07 비교

def test_derived_metric_uses_conditional_components(compiler):
    out = sql(compiler, {"metric": "취소율"})
    assert out == (
        "SELECT COUNT(CASE WHEN \"ORDER_STATUS\" = '99' THEN 1 END) * 1.0 / NULLIF(COUNT(*), 0) "
        'AS "취소율" FROM "TB_ORDER";'
    )


def test_compare_two_periods_adds_change_rate(compiler):
    out = sql(compiler, {"metric": "주문수", "compare": {"field": "ORDER_DTM", "periods": [
        {"label": "9월", "from": "2026-09-01", "to": "2026-09-30"},
        {"label": "8월", "from": "2026-08-01", "to": "2026-08-31"}]}})
    sep = "CASE WHEN \"ORDER_DTM\" >= DATE '2026-09-01' AND \"ORDER_DTM\" < DATE '2026-10-01' THEN 1 END"
    aug = "CASE WHEN \"ORDER_DTM\" >= DATE '2026-08-01' AND \"ORDER_DTM\" < DATE '2026-09-01' THEN 1 END"
    assert f'COUNT({sep}) AS "주문수_9월"' in out
    assert f'COUNT({aug}) AS "주문수_8월"' in out
    assert f'(COUNT({sep}) - COUNT({aug})) * 1.0 / NULLIF(COUNT({aug}), 0) AS "주문수_증감률"' in out
    assert out.endswith("WHERE \"ORDER_DTM\" >= DATE '2026-08-01' AND \"ORDER_DTM\" < DATE '2026-10-01';")


def test_compare_on_derived_metric(compiler):
    out = sql(compiler, {"metric": "취소율", "compare": {"field": "ORDER_DTM", "periods": [
        {"label": "a", "from": "2026-09-01", "to": "2026-09-30"},
        {"label": "b", "from": "2026-08-01", "to": "2026-08-31"},
        {"label": "c", "from": "2026-07-01", "to": "2026-07-31"}]}})
    assert "\"ORDER_STATUS\" = '99' AND \"ORDER_DTM\" >= DATE '2026-07-01'" in out
    assert "증감률" not in out


@pytest.mark.parametrize("compare,msg", [
    ({"field": "PAY_METHOD", "periods": []}, "날짜 컬럼이 아니라"),
    ({"field": "ORDER_DTM", "periods": [{"label": "a", "from": "2026-01-01", "to": "2026-01-02"}]}, "2~4"),
    ({"field": "ORDER_DTM", "periods": [{"label": "a", "from": "2026-02-01", "to": "2026-01-01"},
                                         {"label": "b", "from": "2026-01-01", "to": "2026-01-02"}]}, "늦습니다"),
])
def test_compare_validation(compiler, compare, msg):
    assert msg in error(compiler, {"metric": "주문수", "compare": compare})


def test_compare_needs_aggregation(compiler):
    assert "집계" in error(compiler, {"target_table": "TB_ORDER", "columns": ["ORDER_ID"], "compare": {
        "field": "ORDER_DTM", "periods": []}})


def test_formula_parser():
    assert compile_formula("([a] + 2) * [b] / [c]", lambda n: n.upper()) == (
        "(A + 2) * B * 1.0 / NULLIF(C, 0)", ["a", "b", "c"])
    for bad in ("", "[a] +", "[a] [b]", "([a]", "select 1", "3 + 4"):
        with pytest.raises(ValueError):
            compile_formula(bad, lambda n: n)


# ------------------------------------------------------------------ A-03 / A-04 값 해석

def test_code_label_is_mapped_to_code(compiler):
    out = sql(compiler, {"target_table": "TB_ORDER", "filters": [
        {"field": "ORDER_STATUS", "operator": "in", "value": ["배송완료", "02"]}]})
    assert "\"ORDER_STATUS\" IN ('03', '02')" in out


def test_unknown_code_value_lists_dictionary(compiler):
    msg = error(compiler, {"target_table": "TB_ORDER", "filters": [
        {"field": "ORDER_STATUS", "operator": "equals", "value": "반품"}]})
    assert "03=배송완료" in msg


def test_non_numeric_value_on_numeric_column_rejected(compiler):
    msg = error(compiler, {"target_table": "TB_ORDER", "filters": [
        {"field": "ORDER_ID", "operator": "equals", "value": "테스트 고객"}]})
    assert "숫자 컬럼" in msg and "이름 컬럼" in msg


def test_metric_literal_value_bypasses_partial_dictionary(contract):
    # 사전에 '03' 하나만 있어도 지표가 정의한 '99' 는 그대로 쓰이고, 모델이 낸 값만 사전을 따른다
    order = next(t for t in contract["tables"] if t["name"] == "TB_ORDER")
    status = next(c for c in order["columns"] if c["name"] == "ORDER_STATUS")
    status["codes"] = [{"code": "03", "label": "배송완료"}]
    compiler = Compiler(contract)
    assert "\"ORDER_STATUS\" = '99'" in compiler.compile({"metric": "취소주문수"})
    assert "COUNT(CASE WHEN \"ORDER_STATUS\" = '99'" in compiler.compile({"metric": "취소율"})
    with pytest.raises(ValueError, match="사전에 없는 값"):
        compiler.compile({"metric": "주문수", "filters": [{"field": "ORDER_STATUS", "operator": "equals", "value": "99"}]})


def test_unjoined_table_reference_suggests_joins(compiler):
    msg = error(compiler, {"target_table": "TB_ORDER", "aggregations": [{"field": "*", "function": "COUNT"}],
                           "filters": [{"field": "TB_MEMBER.GRADE_CD", "operator": "equals", "value": "VIP"}]})
    assert '"joins": ["TB_MEMBER"]' in msg
    msg = error(compiler, {"target_table": "TB_MEMBER", "columns": ["TB_PRODUCT.PRODUCT_NM"]})
    assert "등록된 관계가 없어" in msg
