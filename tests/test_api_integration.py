"""실제 PostgreSQL 이 필요한 API 통합 테스트.

TEST_DB_URL(예: postgresql://postgres:pw@localhost:55432/postgres)이 있을 때만 돈다.
그 DB 에 메타데이터 테이블을 만들고, 같은 서버의 shop_itest 스키마를 동기화 대상으로 쓴다.
"""
import os
from urllib.parse import urlparse

import psycopg
import pytest

TEST_DB_URL = os.environ.get("TEST_DB_URL")
pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DB_URL 이 없어 통합 테스트를 건너뜁니다")

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
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        conn.execute(TARGET_DDL)
    from fastapi.testclient import TestClient
    import app as app_module
    with TestClient(app_module.app) as c:
        # 기동 때 schema.sql 이 한 번 돌았다. 두 번 돌아도 안전해야 한다.
        app_module._init_schema()
        yield c
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {TARGET_SCHEMA} CASCADE")
        conn.execute("DELETE FROM datasource WHERE name LIKE 'itest-%'")


@pytest.fixture(scope="module")
def ds(client):
    url = urlparse(TEST_DB_URL)
    res = client.post("/api/datasources", json={
        "name": "itest-shop", "driver": "postgresql", "host": url.hostname, "port": url.port or 5432,
        "db_name": url.path.lstrip("/") or "postgres", "db_schema": TARGET_SCHEMA,
        "username": url.username or "", "password": url.password or "",
    })
    assert res.status_code == 201, res.text
    ds_id = res.json()["id"]
    synced = client.post(f"/api/datasources/{ds_id}/sync")
    assert synced.status_code == 200, synced.text
    body = synced.json()
    assert body["table_count"] == 3 and body["relation_count"] == 2 and body["broken_metrics"] == []
    return ds_id


def test_sync_reads_foreign_keys_as_relations(client, ds):
    rels = client.get(f"/api/datasources/{ds}/relations").json()
    pairs = {(r["left_table"], r["left_column"], r["right_table"], r["right_column"], r["source"]) for r in rels}
    assert ("TB_ORDER", "MEMBER_ID", "TB_MEMBER", "MEMBER_ID", "fk") in pairs
    assert ("TB_ORDER_ITEM", "ORDER_ID", "TB_ORDER", "ORDER_ID", "fk") in pairs
    fk_id = next(r["id"] for r in rels if r["source"] == "fk")
    assert client.delete(f"/api/datasources/{ds}/relations/{fk_id}").status_code == 400


def test_manual_relation_and_model_join_compile(client, ds):
    res = client.post(f"/api/datasources/{ds}/relations", json={
        "left_table": "tb_order_item", "left_column": "order_id", "right_table": "TB_ORDER", "right_column": "ORDER_ID"})
    assert res.status_code == 409  # FK 와 같은 관계
    out = client.post(f"/api/datasources/{ds}/compile", json={"ast": {
        "target_table": "TB_ORDER", "joins": ["TB_MEMBER"], "aggregations": [{"field": "*", "function": "COUNT"}],
        "filters": [{"field": "TB_MEMBER.GRADE_CD", "operator": "equals", "value": "G3"}]}}).json()
    assert out["error"] is None and 'INNER JOIN "TB_MEMBER" ON "TB_ORDER"."MEMBER_ID" = "TB_MEMBER"."MEMBER_ID"' in out["sql"]


def test_annotation_codes_map_labels(client, ds):
    res = client.put(f"/api/datasources/{ds}/annotations/TB_ORDER/order_status", json={
        "synonyms": ["상태", "상태"], "codes": [{"code": "03", "label": "배송완료"}, {"code": 99, "label": "취소"}]})
    assert res.status_code == 200, res.text
    assert res.json()["column_name"] == "ORDER_STATUS" and res.json()["synonyms"] == ["상태"]
    out = client.post(f"/api/datasources/{ds}/compile", json={"ast": {
        "target_table": "TB_ORDER", "columns": ["ORDER_ID"],
        "filters": [{"field": "ORDER_STATUS", "operator": "equals", "value": "배송완료"}]}}).json()
    assert out["sql"].endswith("WHERE \"ORDER_STATUS\" = '03';")
    bad = client.put(f"/api/datasources/{ds}/annotations/TB_ORDER/ORDER_STATUS", json={
        "codes": [{"code": "1", "label": "a"}, {"code": "1", "label": "b"}]})
    assert bad.status_code == 422


def test_metric_lifecycle_with_join_derived_examples_and_history(client, ds):
    base = f"/api/datasources/{ds}/metrics"
    detail = client.post(base, json={
        "name": "주문상세", "kind": "projection", "table_name": "TB_ORDER",
        "joins": [{"table": "TB_ORDER_ITEM", "type": "inner",
                   "on": [{"left": "TB_ORDER.ORDER_ID", "right": "TB_ORDER_ITEM.ORDER_ID"}]}],
        "select_columns": ["TB_ORDER.ORDER_ID", "TB_ORDER_ITEM.QTY"],
        "fixed_filters": [{"field": "TB_ORDER.MEMBER_ID", "operator": "equals", "source": "question"}],
        "examples": [{"question": "m1 주문상세", "ast": {"metric": "주문상세", "filters": [
            {"field": "MEMBER_ID", "operator": "equals", "value": "m1"}]}}],
    })
    assert detail.status_code == 200, detail.text
    assert detail.json()["version"] == 1

    bad_example = client.post(base, json={
        "name": "x", "kind": "aggregate", "table_name": "TB_ORDER", "agg_field": "*", "agg_function": "COUNT",
        "examples": [{"question": "q", "ast": {"metric": "x", "filters": [{"field": "NOPE", "operator": "equals", "value": 1}]}}],
    })
    assert bad_example.status_code == 400 and "예시" in bad_example.json()["detail"]

    missing_join_col = client.post(base, json={
        "name": "y", "kind": "projection", "table_name": "TB_ORDER", "select_columns": ["TB_ORDER_ITEM.QTY"]})
    assert missing_join_col.status_code == 400

    cnt = client.post(base, json={"name": "주문수", "table_name": "TB_ORDER", "agg_field": "*", "agg_function": "COUNT"}).json()
    cancel = client.post(base, json={"name": "취소주문수", "table_name": "TB_ORDER", "agg_field": "*",
                                     "agg_function": "COUNT", "fixed_filters": [
                                         {"field": "ORDER_STATUS", "operator": "equals", "value": "99"}]}).json()
    ratio = client.post(base, json={"name": "취소율", "kind": "derived", "table_name": "TB_ORDER",
                                    "expression": "[취소주문수] / [주문수]"})
    assert ratio.status_code == 200, ratio.text
    assert client.post(base, json={"name": "z", "kind": "derived", "table_name": "TB_ORDER",
                                   "expression": "[주문상세] + 1"}).status_code == 400

    # 파생 지표가 쓰는 지표는 지우거나 이름을 바꿀 수 없다
    assert client.delete(f"{base}/{cnt['id']}").status_code == 400
    renamed = client.put(f"{base}/{cnt['id']}", json={"name": "주문건수", "table_name": "TB_ORDER",
                                                       "agg_field": "*", "agg_function": "COUNT"})
    assert renamed.status_code == 400

    updated = client.put(f"{base}/{cancel['id']}", json={
        "name": "취소주문수", "description": "취소 건수", "table_name": "TB_ORDER", "agg_field": "ORDER_ID",
        "agg_function": "COUNT_DISTINCT",
        "fixed_filters": [{"field": "ORDER_STATUS", "operator": "equals", "value": "99"}]})
    assert updated.status_code == 200 and updated.json()["version"] == 2
    history = client.get(f"{base}/{cancel['id']}/history").json()
    assert [h["version"] for h in history] == [2, 1]
    assert history[1]["snapshot"]["agg_field"] == "*" and history[0]["snapshot"]["agg_function"] == "COUNT_DISTINCT"

    out = client.post(f"/api/datasources/{ds}/compile", json={"ast": {"metric": "취소율"}}).json()
    assert out["sql"] == ('SELECT COUNT(DISTINCT CASE WHEN "ORDER_STATUS" = \'99\' THEN "ORDER_ID" END) * 1.0 '
                          '/ NULLIF(COUNT(*), 0) AS "취소율" FROM "TB_ORDER";')

    client.delete(f"{base}/{ratio.json()['id']}")
    assert client.get(f"{base}/{ratio.json()['id']}/history").json()[0]["action"] == "delete"


def test_sync_reports_broken_metric_and_logs(client, ds):
    base = f"/api/datasources/{ds}/metrics"
    client.post(base, json={"name": "합계", "table_name": "TB_ORDER", "agg_field": "TOTAL_AMT", "agg_function": "SUM"})
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        conn.execute(f'ALTER TABLE {TARGET_SCHEMA}."TB_ORDER" DROP COLUMN "TOTAL_AMT"')
    body = client.post(f"/api/datasources/{ds}/sync").json()
    assert [b["name"] for b in body["broken_metrics"]] == ["합계"]
    logs = client.get(f"/api/sync-logs?datasource_id={ds}").json()
    assert logs[0]["status"] == "ok" and logs[0]["broken_metrics"][0]["name"] == "합계"
    assert logs[0]["trigger"] == "manual" and len(logs) >= 2


def test_sync_failure_is_logged(client):
    res = client.post("/api/datasources", json={
        "name": "itest-down", "driver": "postgresql", "host": "127.0.0.1", "port": 1, "db_name": "x"})
    ds_id = res.json()["id"]
    assert client.post(f"/api/datasources/{ds_id}/sync").status_code == 502
    log = client.get(f"/api/sync-logs?datasource_id={ds_id}").json()[0]
    assert log["status"] == "error" and log["error"]


def test_history_feedback_and_export(client, ds):
    import history
    from uuid import UUID
    ok = history.record_ask(UUID(ds), "주문 수", {"sql": "SELECT 1;", "ast": {"metric": "주문수"}, "error": None,
                                                 "clarification": None, "attempts": 1}, 12.3)
    fail = history.record_ask(UUID(ds), "모름", {"sql": None, "ast": None, "error": "error: x",
                                              "clarification": None, "attempts": 4}, 5)
    assert client.patch(f"/api/history/{fail}", json={"feedback": "up"}).status_code == 400
    patched = client.patch(f"/api/history/{ok}", json={"favorite": True, "feedback": "up", "feedback_note": " 확인 "}).json()
    assert patched["favorite"] and patched["feedback"] == "up" and patched["feedback_note"] == "확인"
    favs = client.get(f"/api/history?datasource_id={ds}&favorite=true").json()
    assert [h["question"] for h in favs] == ["주문 수"]
    exported = client.get(f"/api/history/export?datasource_id={ds}").json()
    assert exported["cases"] == [{"question": "주문 수", "ast": {"metric": "주문수"}, "expected_sql": "SELECT 1;",
                                  "note": "확인"}]
    cleared = client.patch(f"/api/history/{ok}", json={"feedback": ""}).json()
    assert cleared["feedback"] is None and cleared["favorite"]
    assert client.delete(f"/api/history/{fail}").status_code == 204


def test_series_metrics_registered_and_compiled(client, ds):
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        conn.execute(f'CREATE TABLE {TARGET_SCHEMA}."VLOG" ("VID" varchar(10), "TS" timestamp, "HOURS" real, "ERR" smallint)')
        conn.execute(f"""INSERT INTO {TARGET_SCHEMA}."VLOG" VALUES
            ('a', '2026-10-01 00:00', 10, 0), ('a', '2026-10-01 00:01', 11, 2), ('a', '2026-10-01 00:02', 11, 2),
            ('a', '2026-10-01 00:03', 3, 0), ('a', '2026-10-01 00:04', 4, 5), ('b', '2026-10-01 00:00', 7, 0)""")
    assert client.post(f"/api/datasources/{ds}/sync").status_code == 200
    base = f"/api/datasources/{ds}/metrics"
    series = {"partition_by": "VID", "order_by": "TS"}
    hours = client.post(base, json={"name": "가동", "table_name": "VLOG", "agg_field": "HOURS",
                                    "agg_function": "DELTA_SUM", "series": series})
    assert hours.status_code == 200, hours.text
    assert hours.json()["series"] == {**series, "baseline": None, "max_step": None}
    assert client.post(base, json={"name": "오류", "table_name": "VLOG", "agg_field": "ERR",
                                   "agg_function": "CHANGE_COUNT", "series": series}).status_code == 422  # 정상값 없음
    assert client.post(base, json={"name": "오류", "table_name": "VLOG", "agg_field": "ERR",
                                   "agg_function": "CHANGE_COUNT", "series": {**series, "baseline": 0}}).status_code == 200
    assert client.post(base, json={"name": "x", "table_name": "VLOG", "agg_field": "HOURS", "agg_function": "DELTA_SUM",
                                   "series": {"partition_by": "NOPE", "order_by": "TS"}}).status_code == 400
    assert client.post(base, json={"name": "시간당오류", "kind": "derived", "table_name": "VLOG",
                                   "expression": "[오류] / [가동]"}).status_code == 200

    sql = client.post(f"/api/datasources/{ds}/compile", json={"ast": {"metric": "시간당오류", "group_by": ["VID"]}}).json()["sql"]
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        conn.execute(f"SET search_path TO {TARGET_SCHEMA}")
        rows = dict(conn.execute(sql).fetchall())
    # a: 증가분 1 + 0 + (리셋 3→ 버림) + 1 = 2시간, 오류 발생 2회(0→2, 0→5) → 1.0 / b: 증가분 0 → NULL
    assert float(rows["a"]) == 1.0 and rows["b"] is None


def test_series_max_step_is_saved_and_applied(client, ds):
    base = f"/api/datasources/{ds}/metrics"
    res = client.post(base, json={"name": "가동_제한", "table_name": "VLOG", "agg_field": "HOURS", "agg_function": "DELTA_SUM",
                                  "series": {"partition_by": "VID", "order_by": "TS", "max_step": 0.5}})
    assert res.status_code == 200, res.text and res.json()["series"]["max_step"] == 0.5
    assert client.post(base, json={"name": "y", "table_name": "VLOG", "agg_field": "HOURS", "agg_function": "DELTA_SUM",
                                   "series": {"partition_by": "VID", "order_by": "TS", "max_step": 0}}).status_code == 422
    sql = client.post(f"/api/datasources/{ds}/compile", json={"ast": {"metric": "가동_제한", "group_by": ["VID"]}}).json()["sql"]
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        conn.execute(f"SET search_path TO {TARGET_SCHEMA}")
        rows = dict(conn.execute(sql).fetchall())
    # a 의 증가 1, 0, (리셋), 1 은 모두 0.5 를 넘으므로 0
    assert float(rows["a"]) == 0.0
