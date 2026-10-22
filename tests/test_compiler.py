import pytest

from compiler import CalcColumnError, Compiler, ast_tables

CONTRACT = {
    "driver": "postgresql",
    "tables": [
        {
            "name": "bms_log",
            "columns": [
                {"name": "vehicle_id", "type": "varchar"},
                {"name": "collect_datetime", "type": "timestamp", "utc_offset": 9},
                {"name": "uman_time", "type": "numeric"},
                {"name": "eg_time", "type": "numeric"},
            ],
        },
        {
            "name": "vehicle",
            "columns": [
                {"name": "vehicle_id", "type": "varchar"},
                {"name": "vehicle_name", "type": "varchar"},
            ],
        },
    ],
}


@pytest.fixture
def c():
    return Compiler(CONTRACT)


def test_simple_aggregate(c):
    sql = c.compile(
        {
            "from": "bms_log",
            "select": [
                {"expr": {"col": "vehicle_id"}, "as": "vehicle_id"},
                {"expr": {"fn": "COUNT", "args": []}, "as": "n"},
            ],
            "group_by": [{"col": "vehicle_id"}],
            "order_by": [{"expr": {"col": "n"}, "dir": "desc"}],
            "limit": 5,
        }
    )
    assert sql == (
        'SELECT "vehicle_id" AS "vehicle_id", COUNT(*) AS "n" FROM "bms_log" '
        'GROUP BY "vehicle_id" ORDER BY "n" DESC LIMIT 5'
    )


def test_utc_column_is_local_in_select_and_filter_value_is_utc(c):
    sql = c.compile(
        {
            "from": "bms_log",
            "select": [{"expr": {"fn": "DATE", "args": [{"col": "collect_datetime"}]}, "as": "day"}],
            "where": [{"expr": {"col": "collect_datetime"}, "op": ">=", "value": "2026-10-01"}],
            "group_by": [{"fn": "DATE", "args": [{"col": "collect_datetime"}]}],
        }
    )
    assert "CAST((\"collect_datetime\" + INTERVAL '9 hours') AS DATE) AS \"day\"" in sql
    # 컬럼을 감싸지 않고 상수를 UTC 로 — 인덱스를 탄다
    assert "WHERE \"collect_datetime\" >= (TIMESTAMP '2026-10-01 00:00:00' - INTERVAL '9 hours')" in sql


def test_utc_filter_value_written_as_str_expression_is_still_shifted(c):
    sql = c.compile(
        {
            "from": "bms_log",
            "select": [{"expr": {"col": "vehicle_id"}, "as": "v"}],
            "where": [{"expr": {"col": "collect_datetime"}, "op": "<", "value": {"str": "2026-10-15 00:00:00"}}],
        }
    )
    assert sql.endswith("WHERE \"collect_datetime\" < (TIMESTAMP '2026-10-15 00:00:00' - INTERVAL '9 hours')")


def test_add_hours_minus_offset_gives_stored_value(c):
    sql = c.compile(
        {
            "from": "bms_log",
            "select": [
                {"expr": {"fn": "ADD_HOURS", "args": [{"col": "collect_datetime"}, {"num": -9}]}, "as": "utc"}
            ],
        }
    )
    assert sql == 'SELECT "collect_datetime" AS "utc" FROM "bms_log"'


def test_latest_row_per_vehicle_day(c):
    inner = {
        "from": "bms_log",
        "select": [
            {"expr": {"col": "vehicle_id"}, "as": "vehicle_id"},
            {"expr": {"col": "collect_datetime"}, "as": "collect_datetime"},
            {
                "expr": {
                    "fn": "ROW_NUMBER",
                    "partition_by": [{"col": "vehicle_id"}, {"fn": "DATE", "args": [{"col": "collect_datetime"}]}],
                    "order_by": [{"expr": {"col": "collect_datetime"}, "dir": "desc"}],
                },
                "as": "rn",
            },
        ],
    }
    sql = c.compile(
        {
            "from": {"query": inner, "as": "x"},
            "select": [{"expr": {"col": "*"}}],
            "where": [{"expr": {"col": "rn"}, "op": "=", "value": 1}],
        }
    )
    assert sql.startswith('SELECT "vehicle_id" AS "vehicle_id", "collect_datetime" AS "collect_datetime", "rn" AS "rn" FROM (')
    assert "ROW_NUMBER() OVER (PARTITION BY" in sql
    assert sql.endswith(') "x" WHERE "rn" = 1')


def test_two_level_aggregation_needs_subquery(c):
    with pytest.raises(ValueError, match="집계 안에 집계"):
        c.compile(
            {
                "from": "bms_log",
                "select": [
                    {"expr": {"fn": "SUM", "args": [{"fn": "MAX", "args": [{"col": "uman_time"}]}]}, "as": "x"}
                ],
            }
        )


def test_or_with_and_group(c):
    sql = c.compile(
        {
            "from": "bms_log",
            "select": [{"expr": {"col": "vehicle_id"}, "as": "v"}],
            "where": [
                {
                    "or": [
                        {"expr": {"col": "vehicle_id"}, "op": "=", "value": "A"},
                        {
                            "and": [
                                {"expr": {"col": "uman_time"}, "op": ">", "value": 1},
                                {"expr": {"col": "eg_time"}, "op": "<", "value": 2},
                            ]
                        },
                    ]
                }
            ],
        }
    )
    assert sql.endswith("""WHERE ("vehicle_id" = 'A' OR ("uman_time" > 1 AND "eg_time" < 2))""")


def test_errors_name_the_problem(c):
    with pytest.raises(ValueError, match="없는 컬럼"):
        c.compile({"from": "bms_log", "select": [{"expr": {"col": "nope"}, "as": "x"}]})
    with pytest.raises(ValueError, match="group_by"):
        c.compile(
            {
                "from": "bms_log",
                "select": [
                    {"expr": {"col": "vehicle_id"}, "as": "v"},
                    {"expr": {"fn": "SUM", "args": [{"col": "uman_time"}]}, "as": "s"},
                ],
            }
        )
    with pytest.raises(ValueError, match="where 에는 집계"):
        c.compile(
            {
                "from": "bms_log",
                "select": [{"expr": {"col": "vehicle_id"}, "as": "v"}],
                "where": [{"expr": {"fn": "SUM", "args": [{"col": "uman_time"}]}, "op": ">", "value": 1}],
            }
        )


def test_join_qualifies_columns(c):
    ast = {
        "from": "bms_log",
        "joins": [{"table": "vehicle", "type": "left", "on": [["bms_log.vehicle_id", "vehicle.vehicle_id"]]}],
        "select": [{"expr": {"col": "vehicle_name"}, "as": "name"}],
    }
    sql = c.compile(ast)
    assert sql == (
        'SELECT "vehicle"."vehicle_name" AS "name" FROM "bms_log" '
        'LEFT JOIN "vehicle" ON "bms_log"."vehicle_id" = "vehicle"."vehicle_id"'
    )
    assert ast_tables(ast) == ["bms_log", "vehicle"]


DAILY = {
    "from": "bms_log",
    "select": [
        {"expr": {"fn": "DATE", "args": [{"col": "collect_datetime"}]}, "as": "day"},
        {"expr": {"col": "vehicle_id"}, "as": "vehicle_id"},
        {"expr": {"fn": "SUB", "args": [{"fn": "MAX", "args": [{"col": "uman_time"}]}, {"fn": "MIN", "args": [{"col": "uman_time"}]}]}, "as": "uman_hours"},
    ],
    "where": [{"expr": {"col": "collect_datetime"}, "op": ">=", "value": "2026-09-02"}],
    "group_by": [{"fn": "DATE", "args": [{"col": "collect_datetime"}]}, {"col": "vehicle_id"}],
}


def test_join_subquery_keeps_details_of_top_groups(c):
    """합계 상위 5대 차량의 일별 값: 상위 5대를 구하는 하위 쿼리와 조인한다"""
    top = {
        "from": {"query": DAILY, "as": "d"},
        "select": [
            {"expr": {"col": "vehicle_id"}, "as": "vehicle_id"},
            {"expr": {"fn": "SUM", "args": [{"col": "uman_hours"}]}, "as": "total"},
        ],
        "group_by": [{"col": "vehicle_id"}],
        "order_by": [{"expr": {"col": "total"}, "dir": "desc"}],
        "limit": 5,
    }
    ast = {
        "from": {"query": DAILY, "as": "daily"},
        "joins": [{"query": top, "as": "top", "type": "inner", "on": [["daily.vehicle_id", "top.vehicle_id"]]}],
        "select": [
            {"expr": {"col": "daily.day"}, "as": "day"},
            {"expr": {"col": "daily.vehicle_id"}, "as": "vehicle_id"},
            {"expr": {"col": "uman_hours"}, "as": "uman_hours"},
        ],
        "order_by": [{"expr": {"col": "top.total"}, "dir": "desc"}, {"expr": {"col": "day"}, "dir": "asc"}],
    }
    sql = c.compile(ast)
    assert sql.startswith('SELECT "daily"."day" AS "day", "daily"."vehicle_id" AS "vehicle_id", "daily"."uman_hours" AS "uman_hours" FROM (SELECT ')
    assert ') "daily" JOIN (SELECT "vehicle_id" AS "vehicle_id", SUM("uman_hours") AS "total" FROM (SELECT ' in sql
    assert ') "top" ON "daily"."vehicle_id" = "top"."vehicle_id" ORDER BY "top"."total" DESC, "day" ASC' in sql
    assert 'GROUP BY "vehicle_id" ORDER BY "total" DESC LIMIT 5) "top"' in sql
    assert ast_tables(ast) == ["bms_log"]
    # {"table": {"query", "as"}} 로 적어도 같다
    other = {**ast, "joins": [{"table": {"query": top, "as": "top"}, "type": "inner", "on": [["daily.vehicle_id", "top.vehicle_id"]]}]}
    assert c.compile(other) == sql


def test_join_subquery_needs_alias(c):
    with pytest.raises(ValueError, match="별칭"):
        c.compile(
            {
                "from": "bms_log",
                "joins": [{"query": DAILY, "on": [["bms_log.vehicle_id", "vehicle_id"]]}],
                "select": [{"expr": {"col": "bms_log.vehicle_id"}, "as": "v"}],
            }
        )


def test_dialects():
    ast = {
        "from": "bms_log",
        "select": [{"expr": {"col": "collect_datetime"}, "as": "t"}],
        "limit": 3,
    }
    mysql = Compiler({**CONTRACT, "driver": "mysql"}).compile(ast)
    assert mysql == "SELECT (`collect_datetime` + INTERVAL 9 HOUR) AS `t` FROM `bms_log` LIMIT 3"
    oracle = Compiler({**CONTRACT, "driver": "oracle"}).compile(ast)
    assert oracle.endswith("FETCH FIRST 3 ROWS ONLY")
    assert "INTERVAL '9' HOUR" in oracle


def test_case_with_utc_column_against_now_minutes(c):
    sql = c.compile(
        {
            "from": "bms_log",
            "select": [
                {"expr": {"col": "vehicle_id"}, "as": "v"},
                {
                    "expr": {
                        "fn": "CASE",
                        "when": [
                            {
                                "if": [
                                    {
                                        "expr": {"col": "collect_datetime"},
                                        "op": ">=",
                                        "value": {"fn": "ADD_MINUTES", "args": [{"fn": "NOW"}, {"num": -1}]},
                                    }
                                ],
                                "then": {"str": "online"},
                            }
                        ],
                        "else": {"str": "offline"},
                    },
                    "as": "status",
                },
            ],
        }
    )
    # NOW 와 비교하면 상수가 아니니 컬럼을 현지 시각으로 보정한다
    assert (
        "(CASE WHEN (\"collect_datetime\" + INTERVAL '9 hours') >= (CURRENT_TIMESTAMP - INTERVAL '1 minutes') "
        "THEN 'online' ELSE 'offline' END) AS \"status\""
    ) in sql


def test_case_inside_aggregate_and_errors(c):
    flag = {
        "fn": "CASE",
        "when": [{"if": [{"expr": {"col": "uman_time"}, "op": ">", "value": 0}], "then": {"num": 1}}],
        "else": {"num": 0},
    }
    sql = c.compile({"from": "bms_log", "select": [{"expr": {"fn": "SUM", "args": [flag]}, "as": "n"}]})
    assert sql == 'SELECT SUM((CASE WHEN "uman_time" > 0 THEN 1 ELSE 0 END)) AS "n" FROM "bms_log"'
    nested = {
        "fn": "CASE",
        "when": [{"if": [{"expr": {"fn": "MAX", "args": [{"col": "uman_time"}]}, "op": ">", "value": 0}], "then": {"num": 1}}],
    }
    with pytest.raises(ValueError, match="집계 안에 집계"):
        c.compile({"from": "bms_log", "select": [{"expr": {"fn": "SUM", "args": [nested]}, "as": "n"}]})
    with pytest.raises(ValueError, match="CASE"):
        c.compile({"from": "bms_log", "select": [{"expr": {"fn": "CASE", "when": []}, "as": "n"}]})
    # 집계하지 않은 CASE 는 group_by 에 있어야 한다
    with pytest.raises(ValueError, match="group_by"):
        c.compile(
            {
                "from": "bms_log",
                "select": [{"expr": flag, "as": "f"}, {"expr": {"fn": "COUNT", "args": []}, "as": "n"}],
            }
        )


def calc_contract():
    tables = []
    for t in CONTRACT["tables"]:
        columns = []
        for col in t["columns"]:
            columns.append({**col, "needs_calc": True} if col["name"] == "uman_time" else col)
        tables.append({**t, "columns": columns})
    return {**CONTRACT, "tables": tables}


USAGE = {"fn": "SUB", "args": [{"fn": "MAX", "args": [{"col": "uman_time"}]}, {"fn": "MIN", "args": [{"col": "uman_time"}]}]}


def by_vehicle(expr):
    return {
        "from": "bms_log",
        "select": [{"expr": {"col": "vehicle_id"}, "as": "v"}, {"expr": expr, "as": "x"}],
        "group_by": [{"col": "vehicle_id"}],
    }


def test_calc_column_needs_metric_expression():
    guarded = Compiler(calc_contract(), metrics=[])
    for expr in ({"fn": "SUM", "args": [{"col": "uman_time"}]}, {"fn": "AVG", "args": [{"col": "uman_time"}]}, USAGE):
        with pytest.raises(CalcColumnError) as caught:
            guarded.compile(by_vehicle(expr))
        assert (caught.value.table, caught.value.column) == ("bms_log", "uman_time")
    # 값을 보여 주기만 하는 MAX 하나, 계산 필요가 아닌 컬럼의 SUM 은 된다
    guarded.compile(by_vehicle({"fn": "MAX", "args": [{"col": "uman_time"}]}))
    guarded.compile(by_vehicle({"fn": "SUM", "args": [{"col": "eg_time"}]}))
    # 지표 등록(검사 없음)에서는 그대로 컴파일한다
    Compiler(calc_contract()).compile(by_vehicle(USAGE))


def test_metric_expression_is_allowed_with_or_without_table_prefix():
    metric = {"ast": DAILY}
    guarded = Compiler(calc_contract(), metrics=[metric])
    guarded.compile(by_vehicle(USAGE))
    qualified = {"fn": "sub", "args": [{"fn": "MAX", "args": [{"col": "bms_log.uman_time"}]}, {"fn": "MIN", "args": [{"col": "bms_log.uman_time"}]}]}
    guarded.compile(by_vehicle(qualified))
    # 지표 결과(하위 쿼리 컬럼)를 다시 합치는 것은 계산 필요 컬럼이 아니다
    guarded.compile(
        {
            "from": {"query": DAILY, "as": "d"},
            "select": [{"expr": {"col": "vehicle_id"}, "as": "v"}, {"expr": {"fn": "SUM", "args": [{"col": "uman_hours"}]}, "as": "total"}],
            "group_by": [{"col": "vehicle_id"}],
        }
    )
    with pytest.raises(CalcColumnError):
        guarded.compile(by_vehicle({"fn": "SUM", "args": [{"col": "uman_time"}]}))


def test_same_calculation_written_differently_is_allowed():
    """지표는 a - b - c 로, LLM 은 (a - b) - c 로 적어도 같은 계산이다 (유인 가동시간 = 엔진 가동시간 - 무인 가동시간)"""
    contract = calc_contract()
    for col in contract["tables"][0]["columns"]:
        if col["name"] == "eg_time":
            col["needs_calc"] = True
    flat = {"fn": "SUB", "args": [
        {"fn": "MAX", "args": [{"col": "eg_time"}]}, {"fn": "MIN", "args": [{"col": "eg_time"}]}, USAGE,
    ]}
    guarded = Compiler(contract, metrics=[{"ast": by_vehicle(flat)}])
    nested = {"fn": "SUB", "args": [
        {"fn": "SUB", "args": [{"fn": "MAX", "args": [{"col": "eg_time"}]}, {"fn": "MIN", "args": [{"col": "eg_time"}]}]}, USAGE,
    ]}
    guarded.compile(by_vehicle(nested))
    # 지표가 MAX, MIN 으로 계산하는 컬럼을 SUM 하면 안 된다
    with pytest.raises(CalcColumnError, match="MAX, MIN"):
        guarded.compile(by_vehicle({"fn": "SUM", "args": [{"col": "eg_time"}]}))
