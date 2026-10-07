"""AS-IS 메타데이터(datasource_*)를 TO-BE 표(meta_* · metric*)로 옮긴다.

    uv run python backend/migrate_v2.py            # 무엇을 옮길지 보여주기만 (기본)
    uv run python backend/migrate_v2.py --apply    # 실제로 옮기기

- 여러 번 돌려도 안전하다: 이미 옮긴 시스템(같은 id)은 건너뛴다.
- AS-IS 표는 지우지 않는다. 확인이 끝나면 사람이 지운다.
- AS-IS 질문 기록은 schema.sql 이 ask_log_v1 로 이름을 바꿔 두며, 여기서 새 ask_log 로 옮긴다.
- 비밀번호는 평문이었으므로 옮기면서 암호화한다 (NL2SQL_SECRET_KEY 필요).
- 옮긴 뒤 지표마다 테이블 연결(metric_table)을 정의에서 만들고 상태를 다시 매긴다.
  예시 질문이 있던 지표는 active, 없던 지표는 draft — 보강·검수를 거쳐 active 가 된다.
"""
import argparse
import json
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BASE_DIR / ".env")

import db  # noqa: E402
import secret  # noqa: E402


def _exists(table: str) -> bool:
    return db.one("SELECT to_regclass(%s) IS NOT NULL AS ok", table)["ok"]


def _code(name: str, system_id, used: set) -> str:
    """영문 이름이면 이름에서, 아니면 sys-<id 앞 8자리>. 화면에서 바꿀 수 있다."""
    if name.isascii():
        base = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-").lower()[:30] or f"sys-{str(system_id)[:8]}"
    else:
        base = f"sys-{str(system_id)[:8]}"
    code, n = base, 2
    while code in used:
        code, n = f"{base}-{n}", n + 1
    used.add(code)
    return code


def plan() -> list[dict]:
    if not _exists("datasource"):
        return []
    done = {r["id"] for r in db.query("SELECT id FROM meta_system")}
    rows = db.query("""
        SELECT d.*, (SELECT count(*) FROM datasource_table t WHERE t.datasource_id = d.id) AS tables,
               (SELECT count(*) FROM datasource_metric m WHERE m.datasource_id = d.id) AS metrics
          FROM datasource d ORDER BY d.name
    """)
    return [{**r, "skip": r["id"] in done} for r in rows]


def migrate_one(ds: dict, code: str) -> dict:
    sid = str(ds["id"])
    counts = {}
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO meta_system (id, code, name, domain_desc, driver, host, port, db_name, db_schema,
                                         username, password_enc, synced_at, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (sid, code, ds["name"], ds["description"], ds["driver"], ds["host"], ds["port"], ds["db_name"],
                  ds["db_schema"], ds["username"], secret.encrypt(ds["password"] or ""), ds["synced_at"],
                  ds["created_at"], ds["updated_at"]))

            # 테이블 · 컬럼 (id 그대로). 컬럼 사전(column_annotation)은 컬럼 행으로 합친다.
            cur.execute("""
                INSERT INTO meta_table (id, system_id, name, comment)
                SELECT id, datasource_id, name, description FROM datasource_table WHERE datasource_id = %s
            """, (sid,))
            counts["tables"] = cur.rowcount
            cur.execute("""
                INSERT INTO meta_column (table_id, name, data_type, comment, ordinal, synonyms, codes)
                SELECT c.table_id, c.name, c.data_type, c.description, c.ordinal,
                       COALESCE(a.synonyms, '[]'), COALESCE(a.codes, '[]')
                  FROM datasource_column c
                  JOIN datasource_table t ON t.id = c.table_id
                  LEFT JOIN column_annotation a ON a.datasource_id = t.datasource_id
                       AND a.table_name = t.name AND a.column_name = c.name
                 WHERE t.datasource_id = %s
            """, (sid,))
            counts["columns"] = cur.rowcount

            # 관계: 이름 → id
            cur.execute("""
                INSERT INTO meta_relation (id, system_id, left_table_id, left_column, right_table_id, right_column,
                                           constraint_name, source, created_at)
                SELECT r.id, r.datasource_id, lt.id, r.left_column, rt.id, r.right_column, r.constraint_name,
                       r.source, r.created_at
                  FROM datasource_relation r
                  JOIN meta_table lt ON lt.system_id = r.datasource_id AND lt.name = r.left_table
                  JOIN meta_table rt ON rt.system_id = r.datasource_id AND rt.name = r.right_table
                 WHERE r.datasource_id = %s
            """, (sid,))
            counts["relations"] = cur.rowcount

            # 지표: table_name → base_table_id. 기본 테이블이 사라진 지표는 옮기지 못한다.
            cur.execute("""
                INSERT INTO metric (id, system_id, name, description, kind, base_table_id, joins, agg_field,
                                    agg_function, select_columns, expression, series, fixed_filters, version,
                                    created_at, updated_at)
                SELECT m.id, m.datasource_id, m.name, m.description, m.kind, t.id, m.joins, m.agg_field,
                       m.agg_function, m.select_columns, m.expression, m.series, m.fixed_filters, m.version,
                       m.created_at, m.updated_at
                  FROM datasource_metric m
                  JOIN meta_table t ON t.system_id = m.datasource_id AND t.name = m.table_name
                 WHERE m.datasource_id = %s
            """, (sid,))
            counts["metrics"] = cur.rowcount
            cur.execute("""
                SELECT name FROM datasource_metric m WHERE m.datasource_id = %s
                   AND NOT EXISTS (SELECT 1 FROM metric x WHERE x.id = m.id)
            """, (sid,))
            counts["metrics_skipped"] = [r[0] for r in cur.fetchall()]

            # 예시 질문: 사람이 화면에서 적은 것이므로 승인된 것으로
            cur.execute("SELECT id, examples FROM datasource_metric WHERE datasource_id = %s", (sid,))
            examples = [(str(mid), e.get("question"), json.dumps(e.get("ast"), ensure_ascii=False) if e.get("ast") else None)
                        for mid, items in cur.fetchall() for e in (items or []) if e.get("question")]
            cur.executemany("""
                INSERT INTO metric_example (metric_id, question, ast, origin, status, reviewed_at)
                SELECT %s, %s, %s, 'human', 'approved', now() WHERE EXISTS (SELECT 1 FROM metric WHERE id = %s)
            """, [(m, q, a, m) for m, q, a in examples])
            counts["examples"] = len(examples)

            if _exists("datasource_metric_history"):
                cur.execute("""
                    INSERT INTO metric_history (system_id, metric_id, version, action, snapshot, created_at)
                    SELECT datasource_id, metric_id, version, action, snapshot, created_at
                      FROM datasource_metric_history WHERE datasource_id = %s ORDER BY id
                """, (sid,))
                counts["history"] = cur.rowcount

            # 질문 기록: schema.sql 이 AS-IS ask_log 를 ask_log_v1 로 비켜 두었다
            if _exists("ask_log_v1"):
                cur.execute("""
                    INSERT INTO ask_log (id, system_id, question, ast, sql, error, clarification, attempts,
                                         elapsed_ms, favorite, feedback, feedback_note, created_at)
                    SELECT id, datasource_id, question, ast, sql, error, clarification, attempts, elapsed_ms,
                           favorite, feedback, feedback_note, created_at
                      FROM ask_log_v1 WHERE datasource_id = %s
                    ON CONFLICT (id) DO NOTHING
                """, (sid,))
                counts["ask_log"] = cur.rowcount

    from metrics import rebuild_metric_tables, refresh_statuses
    from uuid import UUID
    for r in db.query("SELECT id FROM metric WHERE system_id = %s", sid):
        rebuild_metric_tables(UUID(sid), r["id"])
    refresh_statuses(UUID(sid))
    # 카드가 없는 테이블과 예시 질문이 없는 지표를 보강 대기열에 — 보강·검수 화면에서 돌린다
    from enrich import enqueue_all
    counts["enrich_queued"] = enqueue_all(UUID(sid))
    return counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AS-IS 메타데이터를 TO-BE 표로 옮긴다")
    parser.add_argument("--apply", action="store_true", help="실제로 옮긴다 (없으면 계획만 보여준다)")
    args = parser.parse_args(argv)

    secret.check()
    db.init_pool()
    try:
        schema = (BASE_DIR / "schema.sql").read_text(encoding="utf-8")
        if args.apply:
            with db.connection() as conn:
                conn.execute(schema)
        elif not _exists("meta_system"):
            print("TO-BE 표가 아직 없습니다 — --apply 로 실행하면 schema.sql 로 먼저 만듭니다.")
            rows = db.query("SELECT d.*, 0 AS tables, 0 AS metrics FROM datasource d") if _exists("datasource") else []
            for r in rows:
                print(f"  - {r['name']}")
            return 0
        items = plan()
        if not items:
            print("옮길 AS-IS 데이터가 없습니다.")
            return 0
        used = {r["code"] for r in db.query("SELECT code FROM meta_system")}
        for ds in items:
            if ds["skip"]:
                print(f"  = {ds['name']}: 이미 옮김")
                continue
            code = _code(ds["name"], ds["id"], used)
            print(f"  + {ds['name']} → code={code} (테이블 {ds['tables']}, 지표 {ds['metrics']})")
            if args.apply:
                counts = migrate_one(ds, code)
                print(f"    옮김: {counts}")
        if not args.apply:
            print("\n계획만 보여줬습니다. 옮기려면 --apply")
        else:
            print("\n완료. AS-IS 표(datasource_* · column_annotation · ask_log_v1)는 남아 있습니다 — 확인 후 지우십시오.")
    finally:
        db.close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
