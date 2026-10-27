import copy

import pytest

from compiler import AstError, Compiler, ast_tables

GROUPS = {
    "driver": "postgresql",
    "tables": [
        {"name": "grp", "columns": [{"name": "id", "type": "integer"}, {"name": "parent_id", "type": "varchar"},
                                    {"name": "name", "type": "varchar"}]},
        {"name": "users", "columns": [{"name": "user_id", "type": "varchar"}, {"name": "group_id", "type": "varchar"}]},
    ],
}

USER_GROUP = {"query": {"from": "users", "select": [{"expr": {"col": "group_id"}, "as": "group_id"}],
                        "where": [{"expr": {"col": "user_id"}, "op": "=", "value": "admin"}]}}


def tree_ast(direction="down", start=None, driver_select=None):
    start = copy.deepcopy(start or [{"expr": {"col": "id"}, "op": "in", "value": USER_GROUP}])
    return {
        "from": {"tree": {"table": "grp", "key": "id", "parent": "parent_id", "direction": direction,
                          "start": start},
                 "as": "g"},
        "joins": [{"table": "users", "as": "u", "type": "inner", "on": [["u.group_id", "g.id"]]}],
        "select": driver_select or [{"expr": {"col": "g.name"}, "as": "name"}, {"expr": {"col": "g.depth"}, "as": "depth"},
                                    {"expr": {"col": "u.user_id"}, "as": "user_id"}],
    }


def test_tree_down_from_users_group():
    sql = Compiler(GROUPS).compile(tree_ast())
    assert sql == (
        'SELECT "g"."name" AS "name", "g"."depth" AS "depth", "u"."user_id" AS "user_id" FROM '
        '(WITH RECURSIVE "tree1" ("k", "p", "depth") AS ('
        'SELECT "id", "parent_id", 0 FROM "grp" WHERE CAST("id" AS VARCHAR) IN '
        '(SELECT CAST(x."group_id" AS VARCHAR) FROM (SELECT "group_id" AS "group_id" FROM "users" WHERE "user_id" = \'admin\') x) '
        'UNION ALL SELECT c."id", c."parent_id", t."depth" + 1 FROM "grp" c '
        'JOIN "tree1" t ON CAST(c."parent_id" AS VARCHAR) = CAST(t."k" AS VARCHAR) WHERE t."depth" < 20) '
        'SELECT s."id", s."parent_id", s."name", m."depth" AS "depth" FROM "grp" s '
        'JOIN (SELECT "k", MIN("depth") AS "depth" FROM "tree1" GROUP BY "k") m ON s."id" = m."k") "g" '
        'JOIN "users" "u" ON CAST("u"."group_id" AS VARCHAR) = CAST("g"."id" AS VARCHAR)'
    )


def test_tree_up_follows_parent():
    sql = Compiler(GROUPS).compile(tree_ast("up", [{"expr": {"col": "id"}, "op": "=", "value": 17}]))
    assert 'WHERE "id" = 17 UNION ALL' in sql
    assert 'ON CAST(c."id" AS VARCHAR) = CAST(t."p" AS VARCHAR)' in sql


def test_tree_on_oracle_and_mysql():
    oracle = Compiler({**GROUPS, "driver": "oracle"}).compile(tree_ast())
    assert oracle.count("WITH ") == 1 and "RECURSIVE" not in oracle
    assert 'TO_CHAR(c."parent_id") = TO_CHAR(t."k")' in oracle
    mysql = Compiler({**GROUPS, "driver": "mysql"}).compile(tree_ast())
    assert "WITH RECURSIVE `tree1`" in mysql and "CAST(c.`parent_id` AS CHAR)" in mysql


def test_tree_table_is_listed_and_name_refers_to_alias():
    ast = tree_ast(driver_select=[{"expr": {"col": "grp.name"}, "as": "name"}])
    assert ast_tables(ast) == ["grp", "users"]
    assert '"g"."name" AS "name"' in Compiler(GROUPS).compile(ast)


def test_tree_errors_say_what_to_fix():
    bad = tree_ast()
    bad["from"]["tree"]["parent"] = "parent"
    with pytest.raises(AstError, match="parent 는 grp 의 컬럼"):
        Compiler(GROUPS).compile(bad)
    bad = tree_ast()
    bad["from"]["tree"]["start"] = []
    with pytest.raises(AstError, match="start"):
        Compiler(GROUPS).compile(bad)
    bad = tree_ast()
    bad["from"]["tree"]["start"][0]["value"]["query"]["select"].append({"expr": {"col": "user_id"}, "as": "u"})
    with pytest.raises(AstError, match="select 가 하나"):
        Compiler(GROUPS).compile(bad)


def test_same_type_join_is_not_cast():
    sql = Compiler(GROUPS).compile({
        "from": "users", "joins": [{"table": "grp", "type": "inner", "on": [["users.group_id", "grp.parent_id"]]}],
        "select": [{"expr": {"col": "users.user_id"}, "as": "u"}],
    })
    assert 'ON "users"."group_id" = "grp"."parent_id"' in sql


def test_wrapped_column_refs_are_unwrapped():
    sql = Compiler(GROUPS).compile({
        "from": "users",
        "joins": [{"table": "grp", "type": "inner",
                   "on": [[{"expr": {"col": "users.group_id"}}, {"col": {"expr": {"col": "grp.parent_id"}}}]]}],
        "select": [{"expr": {"col": {"expr": {"col": "users.user_id"}}}, "as": "u"}],
    })
    assert 'SELECT "users"."user_id" AS "u"' in sql
    assert 'ON "users"."group_id" = "grp"."parent_id"' in sql


def roundtrip(sql: str) -> tuple[dict, str]:
    from sql_ast import parse_sql

    ast = parse_sql(sql, GROUPS)["ast"]
    return ast, Compiler(GROUPS).compile(ast)


def test_compiled_tree_sql_reads_back_as_tree():
    """질문 처리가 만든 계층 SQL 을 지표로 등록하면 같은 tree AST 로 돌아온다"""
    first = Compiler(GROUPS).compile(tree_ast())
    ast, again = roundtrip(first)
    assert ast["from"]["tree"] == {
        "table": "grp", "key": "id", "parent": "parent_id", "direction": "down",
        "start": [{"expr": {"col": "id"}, "op": "in", "value": {"query": {
            "from": "users", "select": [{"expr": {"col": "group_id"}, "as": "group_id"}],
            "where": [{"expr": {"col": "user_id"}, "op": "=", "value": "admin"}]}}}],
    }
    # 다시 컴파일해도 계층 부분은 같은 SQL (테이블 별칭 u 는 테이블 이름으로 바뀐다)
    def tree_part(sql):
        return sql[sql.index("(WITH RECURSIVE"):sql.index(') "g"')]

    assert tree_part(again) == tree_part(first)


def test_handwritten_recursive_with():
    ast, sql = roundtrip(
        """with recursive my_groups as (
               select id, parent_id from grp where id = 5
               union
               select c.id, c.parent_id from grp c join my_groups p on c.parent_id = p.id::text)
           select u.user_id from users u join my_groups g on u.group_id = g.id"""
    )
    assert ast["joins"][0]["tree"]["key"] == "id" and ast["joins"][0]["tree"]["direction"] == "down"
    assert ast["joins"][0]["as"] == "g"
    assert 'WITH RECURSIVE "tree1"' in sql and 'CAST("users"."group_id" AS VARCHAR) = CAST("g"."id" AS VARCHAR)' in sql


def test_upward_recursive_with():
    ast, _ = roundtrip(
        """with recursive up as (
               select id, parent_id from grp where id = 17
               union all
               select c.id, c.parent_id from grp c join up t on c.id = t.parent_id)
           select id, name, depth from up"""
    )
    assert ast["from"]["tree"]["direction"] == "up" and ast["from"]["tree"]["parent"] == "parent_id"


def test_recursive_with_shapes_that_cannot_move_say_why():
    from sql_ast import parse_sql

    with pytest.raises(ValueError, match="시작 SELECT 는 테이블 하나"):
        parse_sql("""with recursive g as (select grp.id from grp join users on users.group_id = grp.id where users.user_id = 'a'
                     union select c.id from grp c join g on c.parent_id = g.id) select id from g""", GROUPS)
    with pytest.raises(ValueError, match="컬럼 이름 그대로"):
        parse_sql("""with recursive g as (select id as gid from grp where id = 5
                     union select c.id from grp c join g on c.parent_id = g.gid) select gid from g""", GROUPS)


def test_tables_inside_conditions_are_listed():
    ast = {
        "from": "users",
        "where": [{"or": [{"expr": {"col": "group_id"}, "op": "in", "value": {"query": {
            "from": {"tree": {"table": "grp", "key": "id", "parent": "parent_id",
                              "start": [{"expr": {"col": "id"}, "op": "in", "value": USER_GROUP}]}, "as": "g"},
            "select": [{"expr": {"col": "id"}, "as": "id"}]}}}, {"expr": {"col": "user_id"}, "op": "=", "value": "a"}]}],
        "select": [{"expr": {"col": "user_id"}, "as": "u"}],
    }
    assert ast_tables(ast) == ["users", "grp"]
