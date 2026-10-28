package com.nl2sql.service;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.springframework.stereotype.Service;

import com.nl2sql.common.ApiException;
import com.nl2sql.common.Dyn;
import com.nl2sql.common.Py;
import com.nl2sql.db.MetaDb;

/** 계약서: 질문 처리 · 지표 등록이 보는 스키마 · 관계 · 지표 (contract.py) */
@Service
public class ContractService {

    private final MetaDb db;

    public ContractService(MetaDb db) {
        this.db = db;
    }

    /**
     * 살아 있는 스키마를 {테이블명: {id, name, description, purpose, columns}} 로.
     *
     * 삭제 표시된 테이블·컬럼은 빠진다.
     */
    public Map<String, Map<String, Object>> loadTables(String systemId) {
        List<Map<String, Object>> rows = db.query("""
                SELECT t.id AS table_id, t.name AS table_name, t.db_schema AS table_schema, t.comment AS table_note, t.ai_comment AS table_ai, t.purpose,
                       c.name AS column_name, c.data_type, c.comment AS column_note, c.ai_comment AS column_ai, c.note AS column_human, c.is_pk,
                       c.utc_offset_hours, c.needs_calc
                  FROM meta_table t
                  LEFT JOIN meta_column c ON c.table_id = t.id AND c.deleted_at IS NULL
                 WHERE t.system_id = ? AND t.deleted_at IS NULL
                 ORDER BY t.name, c.ordinal
                """, systemId);
        Map<String, Map<String, Object>> tables = new LinkedHashMap<>();
        for (Map<String, Object> r : rows) {
            String name = (String) r.get("table_name");
            Map<String, Object> table = tables.get(name);
            if (table == null) {
                table = Dyn.of(
                        "id", String.valueOf(r.get("table_id")),
                        "name", name,
                        // 코멘트가 없으면 추론한 설명 (DescribeService)
                        "description", firstText(r.get("table_note"), r.get("table_ai")),
                        "purpose", Py.text(r.get("purpose")),
                        "columns", new ArrayList<Map<String, Object>>());
                if (Py.truthy(r.get("table_schema"))) {  // 기본 스키마가 아닌 테이블만 (이름이 '스키마.테이블')
                    table.put("schema", r.get("table_schema"));
                }
                tables.put(name, table);
            }
            if (r.get("column_name") != null) {
                Map<String, Object> column = Dyn.of(
                        "name", r.get("column_name"),
                        "type", Py.text(r.get("data_type")),
                        // 사람이 적은 설명, 대상 DB 코멘트, 추론한 설명 (DescribeService) 순
                        "description", firstText(r.get("column_human"), r.get("column_note"), r.get("column_ai")));
                if (Py.truthy(r.get("is_pk"))) {
                    column.put("is_pk", true);
                }
                if (Py.truthy(r.get("utc_offset_hours"))) {
                    column.put("utc_offset", ((Number) r.get("utc_offset_hours")).intValue());
                }
                if (Py.truthy(r.get("needs_calc"))) {
                    column.put("needs_calc", true);
                }
                Dyn.items(table.get("columns")).add(column);
            }
        }
        return tables;
    }

    /** 두 테이블과 컬럼이 모두 살아 있는 관계만 돌려준다. */
    public List<Map<String, Object>> loadRelations(String systemId, Map<String, Map<String, Object>> tables) {
        List<Map<String, Object>> out = new ArrayList<>();
        for (Map<String, Object> r : db.query("""
                SELECT r.constraint_name, lt.name AS left_table, r.left_column, rt.name AS right_table, r.right_column
                  FROM meta_relation r
                  JOIN meta_table lt ON lt.id = r.left_table_id
                  JOIN meta_table rt ON rt.id = r.right_table_id
                 WHERE r.system_id = ?
                 ORDER BY r.constraint_name, lt.name, r.left_column
                """, systemId)) {
            Map<String, Object> left = tables.get((String) r.get("left_table"));
            Map<String, Object> right = tables.get((String) r.get("right_table"));
            if (left == null || right == null) {
                continue;
            }
            if (!hasColumn(left, (String) r.get("left_column")) || !hasColumn(right, (String) r.get("right_column"))) {
                continue;
            }
            String constraint = Py.text(r.get("constraint_name"));
            out.add(Dyn.of(
                    "constraint", constraint.isEmpty() ? r.get("left_table") + "->" + r.get("right_table") : constraint,
                    "left_table", r.get("left_table"),
                    "left_column", r.get("left_column"),
                    "right_table", r.get("right_table"),
                    "right_column", r.get("right_column")));
        }
        return out;
    }

    private static boolean hasColumn(Map<String, Object> table, String name) {
        for (Map<String, Object> c : Dyn.maps(table.get("columns"))) {
            if (c.get("name").equals(name)) {
                return true;
            }
        }
        return false;
    }

    /** 지표 행 + 쓰는 테이블 이름 (tables, 기본 테이블이 먼저) */
    public List<Map<String, Object>> loadMetricRows(String systemId) {
        return db.query("""
                SELECT m.*,
                       COALESCE((SELECT jsonb_agg(t.name ORDER BY mt.role, t.name)
                                   FROM metric_table mt JOIN meta_table t ON t.id = mt.table_id
                                  WHERE mt.metric_id = m.id), '[]') AS tables
                  FROM metric m
                 WHERE m.system_id = ? ORDER BY m.name
                """, systemId);
    }

    /** 메타데이터 DB의 지표 행을 계약서 항목으로 */
    public static Map<String, Object> metricEntry(Map<String, Object> m) {
        Map<String, Object> entry = Dyn.of(
                "name", m.get("name"),
                "description", m.get("description"),
                "tables", m.get("tables") == null ? new ArrayList<>() : m.get("tables"),
                "sql", m.get("source_sql"),
                "ast", m.get("ast"));
        if (Py.truthy(m.get("synonyms"))) {
            entry.put("synonyms", m.get("synonyms"));
        }
        return entry;
    }

    public Map<String, Object> loadContract(String systemId) {
        return loadContract(systemId, false);
    }

    /**
     * 메타데이터 DB에서 계약서 로드.
     *
     * activeOnly=true 는 질문 처리용 — 깨지지 않은 지표만 싣는다. false 는 깨진 지표도 싣는다.
     */
    public Map<String, Object> loadContract(String systemId, boolean activeOnly) {
        Map<String, Object> row = db.one("SELECT id, code, name, domain_desc, driver FROM meta_system WHERE id = ?", systemId);
        if (row == null) {
            throw new ApiException(404, "시스템을 찾을 수 없습니다");
        }
        Map<String, Map<String, Object>> tables = loadTables(systemId);
        if (tables.isEmpty()) {
            throw new ApiException(400, "'" + row.get("name") + "'의 스키마가 비어 있습니다. 시스템 화면에서 동기화를 먼저 실행하십시오");
        }
        List<Map<String, Object>> metrics = new ArrayList<>();
        for (Map<String, Object> m : loadMetricRows(systemId)) {
            if (!activeOnly || "active".equals(m.get("status"))) {
                metrics.add(metricEntry(m));
            }
        }
        return Dyn.of(
                "name", row.get("name"),
                "code", row.get("code"),
                "domain", row.get("domain_desc"),
                "driver", row.get("driver"),
                "tables", new ArrayList<>(tables.values()),
                "relations", loadRelations(systemId, tables),
                "metrics", metrics);
    }

    /** 처음으로 비지 않은 글 (a or b or c or "") */
    static String firstText(Object... values) {
        for (Object v : values) {
            if (Py.truthy(v)) {
                return String.valueOf(v);
            }
        }
        return "";
    }
}
