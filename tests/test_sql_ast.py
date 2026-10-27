import pytest

from compiler import Compiler, ast_tables
from sql_ast import parse_sql
from test_compiler import CONTRACT


def roundtrip(sql: str) -> tuple[dict, str, list]:
    parsed = parse_sql(sql, CONTRACT)
    return parsed["ast"], Compiler(CONTRACT).compile(parsed["ast"]), parsed["warnings"]


def test_latest_row_query_from_user_example():
    ast, sql, warnings = roundtrip(
        """select * from ( select collect_datetime, vehicle_id,
             ROW_NUMBER() OVER (PARTITION BY vehicle_id, DATE(collect_datetime) ORDER BY collect_datetime DESC) AS rn
           from bms_log where collect_datetime > '2026-10-01' ) x where x.rn = 1"""
    )
    inner = ast["from"]["query"]
    # 보정 없이 쓴 UTC 컬럼은 원본처럼 저장된 값으로 옮긴다 → 다시 컴파일해도 같은 SQL 뜻
    assert '"collect_datetime" AS "collect_datetime"' in sql
    assert 'PARTITION BY "vehicle_id", CAST("collect_datetime" AS DATE) ORDER BY "collect_datetime" DESC' in sql
    # UTC 상수는 현지 상수로 옮기고, 컴파일러가 다시 UTC 로 돌린다
    assert inner["where"] == [{"expr": {"col": "collect_datetime"}, "op": ">", "value": "2026-10-01 09:00:00"}]
    assert "\"collect_datetime\" > (TIMESTAMP '2026-10-01 09:00:00' - INTERVAL '9 hours')" in sql
    assert sql.endswith('"x" WHERE "rn" = 1')
    assert any("UTC" in w for w in warnings)


def test_local_time_column_needs_no_warning():
    ast, sql, warnings = roundtrip(
        """select date(collect_datetime + interval '9 hours') as day, vehicle_id,
                  abs(max(uman_time) - min(uman_time)) as uman
             from bms_log b
            where b.collect_datetime >= '2026-10-01'::timestamp - interval '9 hours'
            group by 1, 2 order by day"""
    )
    assert ast["select"][0] == {"expr": {"fn": "DATE", "args": [{"col": "collect_datetime"}]}, "as": "day"}
    assert ast["group_by"][0] == {"fn": "DATE", "args": [{"col": "collect_datetime"}]}
    assert ast["order_by"] == [{"expr": {"col": "day"}, "dir": "asc"}]
    assert "ABS((MAX(\"uman_time\") - MIN(\"uman_time\"))) AS \"uman\"" in sql
    assert not warnings


def test_conditions():
    ast, sql, _ = roundtrip(
        """select vehicle_id from bms_log
            where uman_time between 1 and 5 and vehicle_id not in ('A', 'B')
              and (eg_time is null or uman_time > 3 and eg_time < 2) and vehicle_id like 'K%'"""
    )
    assert ast["where"][0] == {"expr": {"col": "uman_time"}, "op": ">=", "value": 1}
    assert ast["where"][2] == {"expr": {"col": "vehicle_id"}, "op": "not_in", "value": ["A", "B"]}
    assert ast["where"][3]["or"][1] == {
        "and": [
            {"expr": {"col": "uman_time"}, "op": ">", "value": 3},
            {"expr": {"col": "eg_time"}, "op": "<", "value": 2},
        ]
    }
    assert """("eg_time" IS NULL OR ("uman_time" > 3 AND "eg_time" < 2))""" in sql


def test_division_guards_written_in_sql_are_not_doubled():
    ast, sql, _ = roundtrip("select sum(uman_time) * 1.0 / nullif(sum(eg_time), 0) as r from bms_log")
    assert ast["select"][0]["expr"] == {
        "fn": "DIV", "args": [{"fn": "SUM", "args": [{"col": "uman_time"}]}, {"fn": "SUM", "args": [{"col": "eg_time"}]}]
    }
    assert sql == 'SELECT (SUM("uman_time") * 1.0 / NULLIF(SUM("eg_time"), 0)) AS "r" FROM "bms_log"'


def test_join_with_aliases():
    ast, sql, _ = roundtrip(
        """select v.vehicle_name, count(*) as n from bms_log b
             join vehicle v on b.vehicle_id = v.vehicle_id group by v.vehicle_name"""
    )
    assert ast["joins"] == [{"table": "vehicle", "type": "inner", "on": [["bms_log.vehicle_id", "vehicle.vehicle_id"]]}]
    assert ast["select"][0] == {"expr": {"col": "vehicle.vehicle_name"}, "as": "vehicle_name"}
    assert 'JOIN "vehicle" ON "bms_log"."vehicle_id" = "vehicle"."vehicle_id"' in sql


def test_star_from_table_keeps_stored_utc_value():
    ast, sql, _ = roundtrip("select * from vehicle")
    assert [s["as"] for s in ast["select"]] == ["vehicle_id", "vehicle_name"]
    _, sql, _ = roundtrip("select * from bms_log")
    assert '"collect_datetime" AS "collect_datetime"' in sql


@pytest.mark.parametrize(
    "sql, message",
    [
        ("select vehicle_id from bms_log limit 5 offset 10", "OFFSET"),
        ("select vehicle_id from bms_log union select vehicle_id from vehicle", "SELECT 문만"),
        ("select distinct on (vehicle_id) vehicle_id from bms_log", "DISTINCT ON"),
        ("select vehicle_id from bms_log where vehicle_id in (select vehicle_id from vehicle)", "서브쿼리"),
        ("select x from nowhere", "메타데이터에 없는 테이블"),
    ],
)
def test_unsupported_sql_says_why(sql, message):
    with pytest.raises(ValueError, match=message):
        parse_sql(sql, CONTRACT)


def test_case_and_minute_interval():
    ast, sql, warnings = roundtrip(
        """select vehicle_id,
                  case when collect_datetime + interval '9 hours' >= now() - interval '1 minute'
                       then 'online' else 'offline' end as status,
                  sum(case vehicle_id when 'A' then 0 else 1 end) as others
             from bms_log group by vehicle_id,
                  case when collect_datetime + interval '9 hours' >= now() - interval '1 minute'
                       then 'online' else 'offline' end"""
    )
    status = ast["select"][1]["expr"]
    assert status["when"][0]["if"] == [
        {"expr": {"col": "collect_datetime"}, "op": ">=", "value": {"fn": "ADD_MINUTES", "args": [{"fn": "NOW", "args": []}, {"num": -1}]}}
    ]
    assert status["else"] == {"str": "offline"}
    # 단순 CASE 는 = 조건으로
    assert ast["select"][2]["expr"]["args"][0]["when"][0]["if"] == [{"expr": {"col": "vehicle_id"}, "op": "=", "value": "A"}]
    assert "SUM((CASE WHEN \"vehicle_id\" = 'A' THEN 0 ELSE 1 END)) AS \"others\"" in sql
    assert not warnings


# 스키마를 여러 개 읽는 시스템 — 기본 스키마가 아닌 테이블은 '스키마.테이블' 이름이다
MULTI = {
    "driver": "postgresql",
    "tables": CONTRACT["tables"]
    + [
        {
            "name": "sales.orders",
            "schema": "sales",
            "columns": [{"name": "vehicle_id", "type": "varchar"}, {"name": "amount", "type": "numeric"}],
        }
    ],
}


def test_other_schema_table_roundtrip():
    parsed = parse_sql(
        """select v.vehicle_name, sum(orders.amount) as total
             from sales.orders join vehicle v on v.vehicle_id = orders.vehicle_id
            group by v.vehicle_name""",
        MULTI,
    )
    ast = parsed["ast"]
    assert ast["from"] == "sales.orders"
    assert ast["joins"][0]["on"] == [["vehicle.vehicle_id", "sales.orders.vehicle_id"]]
    sql = Compiler(MULTI).compile(ast)
    assert 'FROM "sales"."orders" "sales.orders" JOIN "vehicle"' in sql
    assert 'SUM("sales.orders"."amount")' in sql


def test_other_schema_table_needs_schema():
    with pytest.raises(ValueError, match="메타데이터에 없는 테이블"):
        parse_sql("select amount from orders", MULTI)


def test_default_schema_prefix_is_still_dropped():
    ast = parse_sql("select vehicle_id from public.bms_log", MULTI)["ast"]
    assert ast["from"] == "bms_log"


def test_join_on_accepts_col_objects():
    ast = {
        "from": "sales.orders",
        "joins": [{"table": "vehicle", "type": "inner",
                   "on": [[{"col": "sales.orders.vehicle_id"}, {"col": "vehicle.vehicle_id"}]]}],
        "select": [{"expr": {"col": "vehicle.vehicle_name"}, "as": "name"}],
    }
    sql = Compiler(MULTI).compile(ast)
    assert 'ON "sales.orders"."vehicle_id" = "vehicle"."vehicle_id"' in sql


def test_non_string_column_ref_has_clear_error():
    ast = {"from": "bms_log", "select": [{"expr": {"col": {"col": "vehicle_id", "x": 1}}, "as": "v"}]}
    with pytest.raises(Exception, match="문자열로 적습니다"):
        Compiler(MULTI).compile(ast)


def test_table_aliases_from_log():
    """질문 로그에서 그대로 가져온 AST — 조인 테이블에 별칭(as)을 붙이고 on 을 {"col"} 로 적었다"""
    contract = {
        "driver": "postgresql",
        "tables": [
            {"name": "violet.tb_users", "schema": "violet",
             "columns": [{"name": "user_id", "type": "varchar"}, {"name": "user_nm", "type": "varchar"}]},
            {"name": "violet.tb_device_user", "schema": "violet",
             "columns": [{"name": "user_id", "type": "varchar"}, {"name": "sensor_id", "type": "varchar"}]},
            {"name": "vehicle_info",
             "columns": [{"name": "vehicle_id", "type": "varchar"}, {"name": "model", "type": "varchar"}]},
        ],
    }
    ast = {
        "from": "violet.tb_users",
        "joins": [
            {"table": "violet.tb_device_user", "as": "du", "type": "inner",
             "on": [[{"col": "violet.tb_users.user_id"}, {"col": "du.user_id"}]]},
            {"table": "vehicle_info", "as": "vi", "type": "inner",
             "on": [[{"col": "du.sensor_id"}, {"col": "vi.vehicle_id"}]]},
        ],
        "select": [
            {"expr": {"col": "violet.tb_users.user_nm"}, "as": "userName"},
            {"expr": {"col": "vi.vehicle_id"}, "as": "vehicleId"},
            {"expr": {"col": "vi.model"}, "as": "model"},
        ],
        "where": [{"expr": {"col": "violet.tb_users.user_id"}, "op": "=", "value": {"str": "baroness-admin"}}],
    }
    sql = Compiler(contract).compile(ast)
    assert sql == (
        'SELECT "violet.tb_users"."user_nm" AS "userName", "vi"."vehicle_id" AS "vehicleId", "vi"."model" AS "model" '
        'FROM "violet"."tb_users" "violet.tb_users" '
        'JOIN "violet"."tb_device_user" "du" ON "violet.tb_users"."user_id" = "du"."user_id" '
        'JOIN "vehicle_info" "vi" ON "du"."sensor_id" = "vi"."vehicle_id" '
        "WHERE \"violet.tb_users\".\"user_id\" = 'baroness-admin'"
    )
    assert ast_tables(ast) == ["violet.tb_users", "violet.tb_device_user", "vehicle_info"]
    aliased = Compiler(contract).compile({**ast, "from": {"table": "violet.tb_users", "as": "u"},
                                          "select": [{"expr": {"col": "u.user_nm"}, "as": "n"}], "where": []})
    assert 'FROM "violet"."tb_users" "u"' in aliased
