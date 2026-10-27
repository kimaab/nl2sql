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
            "db_schemas": [TARGET_SCHEMA],
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


def test_manual_relation_and_join_compile(client, ds):
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
                "from": "TB_ORDER",
                "joins": [{"table": "TB_MEMBER", "on": [["TB_ORDER.MEMBER_ID", "TB_MEMBER.MEMBER_ID"]]}],
                "select": [{"expr": {"fn": "COUNT", "args": []}, "as": "n"}],
                "where": [{"expr": {"col": "TB_MEMBER.GRADE_CD"}, "op": "=", "value": "G3"}],
            }
        },
    ).json()
    assert out["error"] is None
    assert 'JOIN "TB_MEMBER" ON "TB_ORDER"."MEMBER_ID" = "TB_MEMBER"."MEMBER_ID"' in out["sql"]


def test_needs_calc_mark(client, ds):
    table = next(t for t in client.get(f"/api/systems/{ds}/tables").json() if t["name"] == "TB_ORDER")
    res = client.put(
        f"/api/systems/{ds}/tables/{table['id']}/needs-calc", json={"column": "TOTAL_AMT", "needs_calc": True}
    )
    assert res.status_code == 200, res.text
    assert {c["name"]: c["needs_calc"] for c in res.json()["columns"]}["TOTAL_AMT"] is True
    bad = client.put(f"/api/systems/{ds}/tables/{table['id']}/needs-calc", json={"column": "NOPE", "needs_calc": True})
    assert bad.status_code == 400


DETAIL_SQL = (
    'SELECT o."MEMBER_ID", SUM(i."QTY") AS qty '
    'FROM "TB_ORDER" o JOIN "TB_ORDER_ITEM" i ON o."ORDER_ID" = i."ORDER_ID" '
    'GROUP BY o."MEMBER_ID"'
)


def test_metric_lifecycle_links_status_and_history(client, ds):
    base = f"/api/systems/{ds}/metrics"
    check = client.post(f"{base}/check", json={"sql": DETAIL_SQL}).json()
    assert check["error"] is None, check
    assert check["tables"] == ["TB_ORDER", "TB_ORDER_ITEM"] and check["rows"] == 0
    assert check["ast"]["joins"][0]["on"] == [["TB_ORDER.ORDER_ID", "TB_ORDER_ITEM.ORDER_ID"]]

    detail = client.post(base, json={"name": "주문상세", "description": "회원별 주문 수량", "sql": DETAIL_SQL})
    assert detail.status_code == 200, detail.text
    assert detail.json()["status"] == "active" and detail.json()["version"] == 1
    links = _q(
        """SELECT t.name, mt.role FROM metric_table mt JOIN meta_table t ON t.id = mt.table_id
                  WHERE mt.metric_id = %s ORDER BY mt.role, t.name""",
        detail.json()["id"],
    )
    assert links == [("TB_ORDER", "base"), ("TB_ORDER_ITEM", "join")]

    cnt = client.post(base, json={"name": "주문", "sql": 'SELECT count(*) AS n FROM "TB_ORDER"'}).json()
    assert cnt["status"] == "active"
    updated = client.put(
        f"{base}/{cnt['id']}",
        json={
            "name": "주문",
            "description": "주문 기록",
            "synonyms": ["주문 건수"],
            "sql": "SELECT count(*) AS n FROM \"TB_ORDER\" WHERE \"ORDER_STATUS\" <> '99'",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2 and updated.json()["synonyms"] == ["주문 건수"]
    assert updated.json()["compiled_sql"].endswith("\"ORDER_STATUS\" <> '99'")
    history = client.get(f"{base}/{cnt['id']}/history").json()
    assert [h["version"] for h in history] == [2, 1]
    assert "99" not in history[1]["snapshot"]["sql"] and "99" in history[0]["snapshot"]["sql"]

    # AST 로 못 옮기는 SQL 은 이유와 함께 거부한다
    bad = client.post(base, json={"name": "x", "sql": 'SELECT "ORDER_ID" FROM "TB_ORDER" LIMIT 3 OFFSET 3'})
    assert bad.status_code == 400 and "AST 변환 실패" in bad.text and "OFFSET" in bad.text
    # 옮기며 뜻이 바뀌면 (정수 나누기) 결과 비교에서 거부한다
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        conn.execute(f"INSERT INTO {TARGET_SCHEMA}.\"TB_MEMBER\" VALUES ('m1', 'a', 'G1')")
        conn.execute(f"INSERT INTO {TARGET_SCHEMA}.\"TB_ORDER\" VALUES (1, 'm1', '2026-10-01', '10', 5)")
        conn.execute(f"INSERT INTO {TARGET_SCHEMA}.\"TB_ORDER_ITEM\" VALUES (1, 1, 3), (1, 2, 4)")
    divided = client.post(f"{base}/check", json={"sql": 'SELECT sum("QTY") / 2 AS half FROM "TB_ORDER_ITEM"'}).json()
    assert divided["stage"] == "verify" and "결과가 다릅니다" in divided["error"]

    client.delete(f"{base}/{cnt['id']}")
    assert client.get(f"{base}/{cnt['id']}/history").json()[0]["action"] == "delete"
    assert client.post(base, json={"name": "주문수", "sql": 'SELECT count(*) AS n FROM "TB_ORDER"'}).status_code == 200


class FakeLLM:
    """질문 처리용 가짜 LLM. 테이블 선택은 TB_ORDER + 지표 '주문수', 조회는 그 지표의 AST 그대로."""

    model_name = "fake"

    def invoke(self, messages):
        from langchain_core.messages import AIMessage

        if "[테이블 목록]" in messages[-1].content:
            body = {"tables": ["TB_ORDER"], "needs_metric": True, "metrics": ["주문수"], "reason": "건수", "clarify": ""}
        else:
            body = {"ast": {"from": "TB_ORDER", "select": [{"expr": {"fn": "COUNT", "args": []}, "as": "n"}]}}
        return AIMessage(content=json.dumps(body, ensure_ascii=False))


def test_ask_uses_active_metric(client, ds, monkeypatch):
    import app as app_module

    # 계산식 API 는 없다
    assert client.get(f"/api/systems/{ds}/formulas").status_code == 404
    monkeypatch.setattr(app_module, "make_model", lambda **kw: FakeLLM())
    res = client.post("/api/ask", json={"system_id": ds, "question": "주문 몇 개"}).json()
    assert res["sql"] == 'SELECT COUNT(*) AS "n" FROM "TB_ORDER"', res
    assert res["selected_tables"] == ["TB_ORDER"] and res["selected_metrics"] == ["주문수"]
    assert [s["stage"] for s in res["steps"]] == ["table", "sql"]
    trace = client.get(f"/api/history/{res['id']}/trace").json()
    assert [t["stage"] for t in trace] == ["table", "sql"]
    entry = client.get(f"/api/history?system_id={ds}").json()[0]
    assert entry["selected_metrics"] == ["주문수"]


def test_sync_merge_keeps_ids_and_soft_deletes(client, ds):
    before = dict(_q("SELECT name, id FROM meta_table WHERE system_id = %s", ds))
    saved = client.post(
        f"/api/systems/{ds}/metrics", json={"name": "합계", "sql": 'SELECT sum("TOTAL_AMT") AS s FROM "TB_ORDER"'}
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
            "ast": {"from": "TB_ORDER"},
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
        "ast": {"from": "TB_ORDER"},
        "expected_sql": "SELECT 1;",
        "note": "확인",
    } in exported["cases"]
    assert client.post(f"/api/history/{ok}/eval-case").status_code in (404, 405)
    assert client.delete(f"/api/history/{fail}").status_code == 204


def test_cumulative_counter_metric_per_vehicle_day(client, ds):
    """누적값의 장비 × 일 증가분(MAX - MIN)을 SQL 로 등록하고, 날짜별 합계가 맞는지 본다"""
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        conn.execute(
            f'CREATE TABLE {TARGET_SCHEMA}."VLOG" ("VID" varchar(10), "TS" timestamp, "HOURS" real)'
        )
        conn.execute(f"""INSERT INTO {TARGET_SCHEMA}."VLOG" VALUES
            ('a', '2026-10-01 01:00', 10), ('a', '2026-10-01 05:00', 14), ('a', '2026-10-02 01:00', 15),
            ('a', '2026-10-02 09:00', 18), ('b', '2026-10-01 02:00', 7), ('b', '2026-10-01 03:00', 9)""")
    assert client.post(f"/api/systems/{ds}/sync").status_code == 200
    sql = (
        'SELECT d, SUM(h) AS hours FROM (SELECT DATE("TS") AS d, "VID", ABS(MAX("HOURS") - MIN("HOURS")) AS h '
        'FROM "VLOG" GROUP BY DATE("TS"), "VID") s GROUP BY d ORDER BY d'
    )
    metric = client.post(f"/api/systems/{ds}/metrics", json={"name": "장비 가동", "sql": sql})
    assert metric.status_code == 200, metric.text
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        conn.execute(f"SET search_path TO {TARGET_SCHEMA}")
        rows = conn.execute(metric.json()["compiled_sql"]).fetchall()
    assert [(str(r[0]), float(r[1])) for r in rows] == [("2026-10-01", 6.0), ("2026-10-02", 3.0)]
