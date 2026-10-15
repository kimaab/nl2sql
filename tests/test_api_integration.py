"""실제 PostgreSQL 이 필요한 API 통합 테스트.

TEST_DB_URL(예: postgresql://postgres:pw@localhost:55432/nl2sql_itest)이 있을 때만 돈다.
그 DB 에 메타데이터 테이블을 만들고, 같은 DB 의 shop_itest 스키마를 동기화 대상으로 쓴다.
"""

import base64
import json
import os
from urllib.parse import unquote, urlparse

import psycopg
import pytest

TEST_DB_URL = os.environ.get("TEST_DB_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DB_URL 이 없어 통합 테스트를 건너뜁니다"
)

TARGET_SCHEMA = "shop_itest"
TARGET_DDL = f"""
DROP SCHEMA IF EXISTS {TARGET_SCHEMA} CASCADE;
CREATE SCHEMA {TARGET_SCHEMA};
CREATE TABLE {TARGET_SCHEMA}."TB_MEMBER" ("MEMBER_ID" varchar(20) PRIMARY KEY, "MEMBER_NM" varchar(50),
                                          "GRADE_CD" varchar(2));
CREATE TABLE {TARGET_SCHEMA}."TB_ORDER" ("ORDER_ID" bigint PRIMARY KEY,
    "MEMBER_ID" varchar(20) REFERENCES {TARGET_SCHEMA}."TB_MEMBER" ("MEMBER_ID"),
    "ORDER_DTM" timestamp, "ORDER_STATUS" varchar(2), "TOTAL_AMT" numeric(12,2));
CREATE TABLE {TARGET_SCHEMA}."TB_ORDER_ITEM" ("ORDER_ID" bigint REFERENCES {TARGET_SCHEMA}."TB_ORDER" ("ORDER_ID"),
    "ITEM_SEQ" integer, "QTY" integer, PRIMARY KEY ("ORDER_ID", "ITEM_SEQ"));
COMMENT ON COLUMN {TARGET_SCHEMA}."TB_ORDER"."ORDER_STATUS" IS '주문 상태';
"""


@pytest.fixture(scope="module")
def client():
    os.environ["DB_URL"] = TEST_DB_URL
    os.environ["AUTO_SYNC_MINUTES"] = "0"
    os.environ.setdefault("NL2SQL_SECRET_KEY", base64.b64encode(b"k" * 32).decode())
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        conn.execute(TARGET_DDL)
        conn.execute(
            "DELETE FROM meta_system WHERE code LIKE 'itest-%'"
        ) if _has_schema(conn) else None
    from fastapi.testclient import TestClient
    import app as app_module

    with TestClient(app_module.app) as c:
        # 기동 때 schema.sql 이 한 번 돌았다. 두 번 돌아도 안전해야 한다.
        app_module._init_schema()
        yield c
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {TARGET_SCHEMA} CASCADE")
        conn.execute("DELETE FROM meta_system WHERE code LIKE 'itest-%'")


def _has_schema(conn) -> bool:
    return conn.execute("SELECT to_regclass('meta_system') IS NOT NULL").fetchone()[0]


@pytest.fixture(scope="module")
def ds(client):
    url = urlparse(TEST_DB_URL)
    res = client.post(
        "/api/systems",
        json={
            "code": "itest-shop",
            "name": "itest-shop",
            "domain_desc": "쇼핑몰 주문·회원",
            "driver": "postgresql",
            "host": url.hostname,
            "port": url.port or 5432,
            "db_name": url.path.lstrip("/") or "postgres",
            "db_schema": TARGET_SCHEMA,
            "username": unquote(url.username or ""),
            "password": unquote(url.password or ""),
        },
    )
    assert res.status_code == 201, res.text
    assert "password" not in res.json() and "password_enc" not in res.json()
    sid = res.json()["id"]
    synced = client.post(f"/api/systems/{sid}/sync")
    assert synced.status_code == 200, synced.text
    body = synced.json()
    assert (body["table_count"], body["tables_added"], body["relation_count"]) == (
        3,
        3,
        2,
    )
    assert body["broken_metrics"] == []
    return sid


def _q(sql, *args):
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        return conn.execute(sql, args).fetchall()


def test_password_is_encrypted_and_duplicate_code_rejected(client, ds):
    blob = _q("SELECT password_enc FROM meta_system WHERE id = %s", ds)[0][0]
    password = unquote(urlparse(TEST_DB_URL).password or "")
    assert password.encode() not in bytes(blob)
    dup = client.post(
        "/api/systems",
        json={
            "code": "itest-shop",
            "name": "다른이름",
            "driver": "postgresql",
            "host": "h",
            "db_name": "d",
        },
    )
    assert dup.status_code == 409


def test_sync_reads_foreign_keys_and_primary_keys(client, ds):
    rels = client.get(f"/api/systems/{ds}/relations").json()
    pairs = {
        (
            r["left_table"],
            r["left_column"],
            r["right_table"],
            r["right_column"],
            r["source"],
        )
        for r in rels
    }
    assert ("TB_ORDER", "MEMBER_ID", "TB_MEMBER", "MEMBER_ID", "fk") in pairs
    fk_id = next(r["id"] for r in rels if r["source"] == "fk")
    assert client.delete(f"/api/systems/{ds}/relations/{fk_id}").status_code == 400
    pk = _q(
        """SELECT c.name FROM meta_column c JOIN meta_table t ON t.id = c.table_id
               WHERE t.system_id = %s AND t.name = 'TB_ORDER_ITEM' AND c.is_pk ORDER BY c.name""",
        ds,
    )
    assert [r[0] for r in pk] == ["ITEM_SEQ", "ORDER_ID"]


def test_manual_relation_and_model_join_compile(client, ds):
    res = client.post(
        f"/api/systems/{ds}/relations",
        json={
            "left_table": "tb_order_item",
            "left_column": "order_id",
            "right_table": "TB_ORDER",
            "right_column": "ORDER_ID",
        },
    )
    assert res.status_code == 409  # FK 와 같은 관계
    out = client.post(
        f"/api/systems/{ds}/compile",
        json={
            "ast": {
                "target_table": "TB_ORDER",
                "joins": ["TB_MEMBER"],
                "aggregations": [{"field": "*", "function": "COUNT"}],
                "filters": [
                    {"field": "TB_MEMBER.GRADE_CD", "operator": "equals", "value": "G3"}
                ],
            }
        },
    ).json()
    assert (
        out["error"] is None
        and 'INNER JOIN "TB_MEMBER" ON "TB_ORDER"."MEMBER_ID" = "TB_MEMBER"."MEMBER_ID"'
        in out["sql"]
    )


def test_metric_lifecycle_links_status_and_history(client, ds):
    base = f"/api/systems/{ds}/metrics"
    item = client.post(
        f"/api/systems/{ds}/formulas",
        json={"name": "수량 합", "source_sql": "SUM(QTY)", "description": "주문 상품 수량"},
    )
    assert item.status_code == 200, item.text
    assert item.json()["expr"] == {"fn": "SUM", "args": [{"col": "QTY"}]}
    assert item.json()["level"] == "aggregate" and item.json()["mode"] == "organic"
    detail = client.post(
        base,
        json={
            "name": "주문상세",
            "table_name": "TB_ORDER",
            "joins": [
                {
                    "table": "TB_ORDER_ITEM",
                    "type": "inner",
                    "on": [
                        {"left": "TB_ORDER.ORDER_ID", "right": "TB_ORDER_ITEM.ORDER_ID"}
                    ],
                }
            ],
            "dimensions": ["TB_ORDER.MEMBER_ID"],
            "formulas": ["수량 합"],
            "fixed_filters": [
                {
                    "field": "TB_ORDER.MEMBER_ID",
                    "operator": "equals",
                    "source": "question",
                }
            ],
        },
    )
    assert detail.status_code == 200, detail.text
    # 깨지지 않았으면 저장하자마자 active
    assert detail.json()["status"] == "active" and detail.json()["version"] == 1
    assert detail.json()["formulas"] == ["수량 합"]
    links = _q(
        """SELECT t.name, mt.role FROM metric_table mt JOIN meta_table t ON t.id = mt.table_id
                  WHERE mt.metric_id = %s ORDER BY mt.role, t.name""",
        detail.json()["id"],
    )
    assert links == [("TB_ORDER", "base"), ("TB_ORDER_ITEM", "join")]

    cnt = client.post(base, json={"name": "주문", "table_name": "TB_ORDER"}).json()
    assert cnt["status"] == "active" and cnt["dimensions"] == []
    # 지표가 허용한 계산식은 지울 수 없다
    assert client.delete(f"/api/systems/{ds}/formulas/{item.json()['id']}").status_code == 400

    updated = client.put(
        f"{base}/{cnt['id']}",
        json={
            "name": "주문",
            "description": "주문 기록",
            "table_name": "TB_ORDER",
            "dimensions": [{"field": "ORDER_DTM", "grain": "day"}],
            "synonyms": ["주문 건수"],
            "fixed_filters": [
                {"field": "ORDER_STATUS", "operator": "not_equals", "value": "99"}
            ],
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2 and updated.json()["synonyms"] == ["주문 건수"]
    history = client.get(f"{base}/{cnt['id']}/history").json()
    assert [h["version"] for h in history] == [2, 1]
    assert history[1]["snapshot"]["dimensions"] == []
    assert history[0]["snapshot"]["dimensions"] == [{"field": "ORDER_DTM", "grain": "day"}]
    bad = client.post(
        base,
        json={"name": "x", "table_name": "TB_ORDER", "dimensions": [{"field": "MEMBER_ID", "grain": "day"}]},
    )
    assert bad.status_code == 400 and "날짜 컬럼이 아니라" in bad.text

    out = client.post(
        f"/api/systems/{ds}/compile",
        json={"ast": {"metric": "주문", "aggregations": [{"field": "*", "function": "COUNT", "alias": "주문수"}]}},
    ).json()
    assert out["sql"] == 'SELECT COUNT(*) AS "주문수" FROM "TB_ORDER" WHERE "ORDER_STATUS" != \'99\';'
    client.delete(f"{base}/{cnt['id']}")
    assert client.get(f"{base}/{cnt['id']}/history").json()[0]["action"] == "delete"
    assert client.post(base, json={"name": "주문수", "table_name": "TB_ORDER"}).status_code == 200


class FakeLLM:
    """질문 처리용 가짜 LLM. 지표 선택은 '주문수', 조회는 건수."""

    model_name = "fake"

    def invoke(self, messages):
        from langchain_core.messages import AIMessage

        body = {"metric": "주문수", "reason": "건수"}
        return AIMessage(content=json.dumps(body, ensure_ascii=False))

    def bind_tools(self, tools):
        from langchain_core.messages import AIMessage

        class Bound:
            def invoke(self, messages):
                ast = {
                    "metric": "주문수",
                    "aggregations": [{"field": "*", "function": "COUNT", "alias": "주문수"}],
                }
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "compile_sql",
                            "id": "c1",
                            "args": {"ast": json.dumps(ast, ensure_ascii=False)},
                        }
                    ],
                )

        return Bound()


def test_ask_uses_active_metric(client, ds, monkeypatch):
    import app as app_module

    # 보강·검수·평가 API 는 없다
    for path in ("enrich", "review", "eval-runs"):
        assert client.get(f"/api/systems/{ds}/{path}").status_code == 404
    metric = next(
        m
        for m in client.get(f"/api/systems/{ds}/metrics").json()
        if m["name"] == "주문수"
    )
    assert metric["status"] == "active"

    monkeypatch.setattr(app_module, "make_model", lambda **kw: FakeLLM())
    res = client.post(
        "/api/ask", json={"system_id": ds, "question": "주문 몇 개"}
    ).json()
    assert res["sql"] == 'SELECT COUNT(*) AS "주문수" FROM "TB_ORDER";', res
    assert res["selected_tables"] == ["TB_ORDER"] and res["selected_metrics"] == [
        "주문수"
    ]
    assert [s["stage"] for s in res["steps"]] == ["metric", "sql"]
    trace = client.get(f"/api/history/{res['id']}/trace").json()
    assert [t["stage"] for t in trace] == ["metric", "sql"]
    entry = client.get(f"/api/history?system_id={ds}").json()[0]
    assert entry["selected_metrics"] == ["주문수"]


def test_sync_merge_keeps_ids_and_soft_deletes(client, ds):
    before = dict(_q("SELECT name, id FROM meta_table WHERE system_id = %s", ds))
    client.post(
        f"/api/systems/{ds}/formulas",
        json={"name": "금액 합", "source_sql": "SUM(TOTAL_AMT)"},
    )
    saved = client.post(
        f"/api/systems/{ds}/metrics",
        json={"name": "합계", "table_name": "TB_ORDER", "formulas": ["금액 합"]},
    )
    assert saved.status_code == 200, saved.text
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        conn.execute(f'ALTER TABLE {TARGET_SCHEMA}."TB_ORDER" DROP COLUMN "TOTAL_AMT"')
        conn.execute(f'CREATE TABLE {TARGET_SCHEMA}."TB_NEW" ("ID" int)')
        conn.execute(f'DROP TABLE {TARGET_SCHEMA}."TB_ORDER_ITEM"')
    body = client.post(f"/api/systems/{ds}/sync").json()
    assert (body["tables_added"], body["tables_changed"], body["tables_removed"]) == (
        1,
        1,
        1,
    )
    assert [b["name"] for b in body["broken_metrics"]] == ["주문상세", "합계"]
    after = dict(_q("SELECT name, id FROM meta_table WHERE system_id = %s", ds))
    assert after["TB_ORDER"] == before["TB_ORDER"]  # id 유지
    gone = _q(
        "SELECT deleted_at IS NOT NULL FROM meta_table WHERE id = %s",
        before["TB_ORDER_ITEM"],
    )[0][0]
    assert gone  # 지우지 않고 표시만
    statuses = {
        m["name"]: m["status"] for m in client.get(f"/api/systems/{ds}/metrics").json()
    }
    assert statuses["합계"] == "broken" and statuses["주문상세"] == "broken"
    logs = client.get(f"/api/sync-logs?system_id={ds}").json()
    assert logs[0]["status"] == "ok" and logs[0]["tables_removed"] == 1


def test_sync_failure_is_logged(client):
    res = client.post(
        "/api/systems",
        json={
            "code": "itest-down",
            "name": "itest-down",
            "driver": "postgresql",
            "host": "127.0.0.1",
            "port": 1,
            "db_name": "x",
        },
    )
    sid = res.json()["id"]
    assert client.post(f"/api/systems/{sid}/sync").status_code == 502
    log = client.get(f"/api/sync-logs?system_id={sid}").json()[0]
    assert log["status"] == "error" and log["error"]


def test_history_feedback_and_export(client, ds):
    import history
    from uuid import UUID

    ok = history.record_ask(
        UUID(ds),
        "주문 수",
        {
            "sql": "SELECT 1;",
            "ast": {"metric": "주문수"},
            "error": None,
            "clarification": None,
            "attempts": 1,
            "tables": ["TB_ORDER"],
            "metrics": ["주문수"],
            "steps": [],
            "total_tokens": 0,
        },
        12.3,
    )
    fail = history.record_ask(
        UUID(ds),
        "모름",
        {
            "sql": None,
            "ast": None,
            "error": "error: x",
            "clarification": None,
            "attempts": 4,
        },
        5,
    )
    assert (
        client.patch(f"/api/history/{fail}", json={"feedback": "up"}).status_code == 400
    )
    patched = client.patch(
        f"/api/history/{ok}",
        json={"favorite": True, "feedback": "up", "feedback_note": " 확인 "},
    ).json()
    assert (
        patched["favorite"]
        and patched["feedback"] == "up"
        and patched["feedback_note"] == "확인"
    )
    exported = client.get(f"/api/history/export?system_id={ds}").json()
    assert {
        "question": "주문 수",
        "ast": {"metric": "주문수"},
        "expected_sql": "SELECT 1;",
        "note": "확인",
    } in exported["cases"]
    assert client.post(f"/api/history/{ok}/eval-case").status_code in (404, 405)
    assert client.delete(f"/api/history/{fail}").status_code == 204


def test_cumulative_counter_formula_per_vehicle_day(client, ds):
    """누적값 계산식(MAX - MIN)을 장비 × 일 디멘젼으로 계산하고, 날짜별 합계로 다시 집계한다"""
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        conn.execute(
            f'CREATE TABLE {TARGET_SCHEMA}."VLOG" ("VID" varchar(10), "TS" timestamp, "HOURS" real)'
        )
        conn.execute(f"""INSERT INTO {TARGET_SCHEMA}."VLOG" VALUES
            ('a', '2026-10-01 01:00', 10), ('a', '2026-10-01 05:00', 14), ('a', '2026-10-02 01:00', 15),
            ('a', '2026-10-02 09:00', 18), ('b', '2026-10-01 02:00', 7), ('b', '2026-10-01 03:00', 9)""")
    assert client.post(f"/api/systems/{ds}/sync").status_code == 200
    parsed = client.post(
        f"/api/systems/{ds}/formulas/parse",
        json={
            "sql": 'SELECT d, SUM(h) FROM (SELECT DATE("TS") AS d, "VID", ABS(MAX("HOURS") - MIN("HOURS")) AS h '
            'FROM "VLOG" GROUP BY DATE("TS"), "VID") s GROUP BY d'
        },
    ).json()
    assert [f["text"] for f in parsed["formulas"]] == ["ABS(MAX(HOURS) - MIN(HOURS))"]
    assert parsed["dimensions"] == [{"field": "TS", "grain": "day"}, "VID"] and parsed["table"] == "VLOG"
    formula = client.post(
        f"/api/systems/{ds}/formulas",
        json={
            "name": "가동시간",
            "mode": "forced",
            "column_name": "HOURS",
            "expr": parsed["formulas"][0]["expr"],
        },
    )
    assert formula.status_code == 200, formula.text
    definition = {
        "name": "장비 가동",
        "table_name": "VLOG",
        "dimensions": parsed["dimensions"],
        "formulas": ["가동시간"],
    }
    preview = client.post(f"/api/systems/{ds}/metrics/preview", json=definition).json()
    assert preview["error"] is None and preview["sql"].startswith("-- 가동시간\n")
    metric = client.post(f"/api/systems/{ds}/metrics", json=definition)
    assert metric.status_code == 200, metric.text

    def run(ast):
        out = client.post(
            f"/api/systems/{ds}/compile", json={"ast": {"metric": "장비 가동", **ast}}
        ).json()
        assert out["error"] is None, out["error"]
        with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
            conn.execute(f"SET search_path TO {TARGET_SCHEMA}")
            return conn.execute(out["sql"]).fetchall()

    per = run(
        {
            "aggregations": [{"formula": "가동시간"}],
            "order_by": [{"field": "TS_day"}, {"field": "VID"}],
        }
    )
    assert [(str(r[0]), r[1], float(r[2])) for r in per] == [
        ("2026-10-01", "a", 4.0),
        ("2026-10-01", "b", 2.0),
        ("2026-10-02", "a", 3.0),
    ]
    daily = run(
        {
            "aggregations": [{"formula": "가동시간", "function": "SUM"}],
            "group_by": [{"field": "TS", "grain": "day"}],
            "order_by": [{"field": "TS_day"}],
        }
    )
    assert [(str(r[0]), float(r[1])) for r in daily] == [("2026-10-01", 6.0), ("2026-10-02", 3.0)]
    monthly = run(
        {
            "aggregations": [{"formula": "가동시간"}],
            "group_by": [{"field": "TS", "grain": "month"}, "VID"],
        }
    )
    assert {r[1]: float(r[2]) for r in monthly} == {"a": 7.0, "b": 2.0}
    # 다른 계산식이 가리키는 계산식은 이름을 바꿀 수 없고, 서로 가리키게 만들 수도 없다
    double = client.post(
        f"/api/systems/{ds}/formulas", json={"name": "가동 두배", "source_sql": "[가동시간] * 2"}
    )
    assert double.status_code == 200, double.text
    renamed = client.put(
        f"/api/systems/{ds}/formulas/{formula.json()['id']}",
        json={"name": "가동", "expr": parsed["formulas"][0]["expr"]},
    )
    assert renamed.status_code == 400 and "가동 두배" in renamed.text
    loop = client.put(
        f"/api/systems/{ds}/formulas/{formula.json()['id']}",
        json={"name": "가동시간", "source_sql": "[가동 두배] + 1"},
    )
    assert loop.status_code == 400 and "서로를 참조" in loop.text
