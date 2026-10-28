package com.nl2sql.service;

import java.sql.SQLException;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import com.nl2sql.common.Dyn;
import com.nl2sql.common.Json;
import com.nl2sql.compiler.Dialect;
import com.nl2sql.db.TargetSession;

/** 실행 계획 확인 (metric_draft.full_scans) — 지표 초안 · 자동 등록 · 검증이 함께 쓴다 */
public final class Plans {

    static final int SMALL_TABLE_ROWS = 10_000;  // 이보다 작은 테이블은 전체를 훑어도 된다 (작으면 실행 계획이 일부러 그렇게 고른다)

    private Plans() {
    }

    /**
     * 실행 계획에서 전체를 훑는 큰 테이블 (PostgreSQL Seq Scan, MySQL type ALL). 실행하지 않고 EXPLAIN 만 본다.
     *
     * 작은 테이블(SMALL_TABLE_ROWS 미만)은 뺀다 — 작으면 인덱스가 있어도 전체 훑기가 더 싸다.
     * Oracle 은 EXPLAIN PLAN 이 PLAN_TABLE 에 써야 해서 읽기 전용 트랜잭션에서 볼 수 없다 — 빈 목록이다.
     */
    public static List<String> fullScans(TargetSession cur, Dialect dialect, String sql, Object... params) throws SQLException {
        List<String> found = new ArrayList<>();
        if (dialect.driver().equals("postgresql")) {
            // VERBOSE 라야 계획에 스키마가 나온다 — 기본 스키마가 아닌 테이블도 통계를 찾는다
            Object plan = cur.one("EXPLAIN (FORMAT JSON, VERBOSE) " + sql, params).get(0);
            if (plan instanceof String text) {
                plan = Json.parse(text);
            }
            List<String[]> scanned = new ArrayList<>();
            seqScans(Dyn.map(Dyn.map(Dyn.items(plan).get(0)).get("Plan")), scanned);
            String defaultSchema = String.valueOf(cur.one("SELECT current_schema()").get(0));
            for (String[] s : scanned) {
                String schema = s[0];
                String relation = s[1];
                String name = schema.isEmpty() || schema.equals(defaultSchema) ? relation : schema + "." + relation;
                List<Object> rows = cur.one("SELECT reltuples FROM pg_class WHERE oid = to_regclass(?)", dialect.table(name, schema));
                Object tuples = rows == null ? null : rows.get(0);
                // 통계가 없으면(-1) 큰 테이블로 본다
                boolean big = tuples == null || ((Number) tuples).doubleValue() < 0
                        || ((Number) tuples).doubleValue() >= SMALL_TABLE_ROWS;
                if (big && !found.contains(name)) {
                    found.add(name);
                }
            }
        } else if (dialect.driver().equals("mysql")) {
            TargetSession.Result result = cur.query("EXPLAIN " + sql, -1, params);
            List<String> names = new ArrayList<>();
            for (String c : result.columns()) {
                names.add(c.toLowerCase());
            }
            for (List<Object> r : result.rows()) {
                String table = String.valueOf(r.get(names.indexOf("table")));
                Object rows = r.get(names.indexOf("rows"));
                long count = rows == null ? 0 : ((Number) rows).longValue();
                if ("ALL".equals(r.get(names.indexOf("type"))) && count >= SMALL_TABLE_ROWS && !found.contains(table)) {
                    found.add(table);
                }
            }
        }
        return found;
    }

    private static void seqScans(Map<String, Object> node, List<String[]> out) {
        if ("Seq Scan".equals(node.get("Node Type")) && node.get("Relation Name") != null) {
            out.add(new String[] {node.get("Schema") == null ? "" : String.valueOf(node.get("Schema")),
                    String.valueOf(node.get("Relation Name"))});
        }
        for (Map<String, Object> child : Dyn.maps(node.get("Plans"))) {
            seqScans(child, out);
        }
    }
}
