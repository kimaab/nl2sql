package com.nl2sql.db;

import java.sql.SQLException;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import com.nl2sql.common.ApiException;
import com.nl2sql.common.Dyn;
import com.nl2sql.common.Py;

/** 대상 DB 의 스키마 · FK 관계 · 인덱스 읽기 (catalog.py) */
@Component
public class Catalog {

    private static final Logger log = LoggerFactory.getLogger("nl2sql.catalog");
    private static final int READ_TIMEOUT_S = 30;

    /** 테이블 [{name, schema, description, columns: [{name, type, description, is_pk}]}] 과 FK 관계 (못 읽었으면 null) */
    public record Read(List<Map<String, Object>> tables, List<Map<String, Object>> relations) {
    }

    private final TargetDb targetDb;

    public Catalog(TargetDb targetDb) {
        this.targetDb = targetDb;
    }

    /**
     * 대상 DB의 스키마와 FK 관계 읽기.
     *
     * 관계는 null 일 수 있다 — FK 카탈로그를 읽을 권한이 없을 때다. 그때 기존 FK 관계를 지우면 안 되므로 '빈 목록'과 구분한다.
     */
    public Read readCatalog(Map<String, Object> row) {
        TargetSession session;
        try {
            session = targetDb.plain(row, READ_TIMEOUT_S);
        } catch (SQLException error) {
            throw unreachable(row, error);
        }
        try (session) {
            return switch ((String) row.get("driver")) {
                case "mysql" -> readMysql(row, session);
                case "oracle" -> readOracle(row, session);
                default -> readPostgresql(row, session);
            };
        } catch (SQLException error) {
            throw unreachable(row, error);
        }
    }

    /**
     * 읽을 스키마. 첫 스키마가 기본 스키마다 — 그 테이블은 이름만, 나머지 스키마의 테이블은 '스키마.테이블' 로 쓴다.
     *
     * 비우면 PostgreSQL 은 public, Oracle 은 접속 계정이다. MySQL 은 접속한 데이터베이스 하나만 읽는다.
     */
    public static List<String> schemas(Map<String, Object> row) {
        String driver = (String) row.get("driver");
        if ("mysql".equals(driver)) {
            return List.of((String) row.get("db_name"));
        }
        List<String> listed = new ArrayList<>();
        for (String s : Dyn.strings(row.get("db_schemas"))) {
            listed.add("oracle".equals(driver) ? s.strip().toUpperCase() : s.strip());
        }
        if (!listed.isEmpty()) {
            return listed;
        }
        if ("oracle".equals(driver)) {
            String owner = Py.text(row.get("username")).strip().toUpperCase();
            if (owner.isEmpty()) {
                throw new ApiException(400, "Oracle 시스템은 스키마나 계정 중 하나가 필요합니다");
            }
            return List.of(owner);
        }
        return List.of("public");
    }

    /** 메타데이터 테이블 → [스키마, 대상 DB 의 테이블 이름] */
    public static String[] tableParts(Map<String, Object> row, Map<String, Object> table) {
        String schema = Py.text(table.get("schema"));
        String name = (String) table.get("name");
        if (!schema.isEmpty() && name.startsWith(schema + ".")) {
            return new String[] {schema, name.substring(schema.length() + 1)};
        }
        return new String[] {schemas(row).get(0), name};
    }

    /** 기본 스키마의 테이블은 이름만, 나머지는 '스키마.테이블' */
    static String qualified(String schema, String name, String defaultSchema) {
        return schema.equals(defaultSchema) ? name : schema + "." + name;
    }

    private Read readPostgresql(Map<String, Object> row, TargetSession cur) throws SQLException {
        List<String> names = schemas(row);
        String defaultSchema = names.get(0);
        Map<String, Map<String, Object>> tables = tableMap(cur.rows("""
                SELECT n.nspname, c.relname, coalesce(obj_description(c.oid, 'pg_class'), '')
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = ANY(?) AND c.relkind IN ('r','v','m','p','f')
                 ORDER BY n.nspname, c.relname
                """, names), defaultSchema);
        for (List<Object> r : cur.rows("""
                SELECT n.nspname, c.relname, a.attname,
                       format_type(a.atttypid, a.atttypmod),
                       coalesce(col_description(c.oid, a.attnum), '')
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                  JOIN pg_attribute a ON a.attrelid = c.oid
                 WHERE n.nspname = ANY(?) AND c.relkind IN ('r','v','m','p','f')
                   AND a.attnum > 0 AND NOT a.attisdropped
                 ORDER BY n.nspname, c.relname, a.attnum
                """, names)) {
            Map<String, Object> table = tables.get(qualified(text(r.get(0)), text(r.get(1)), defaultSchema));
            if (table != null) {
                columns(table).add(Dyn.of("name", text(r.get(2)), "type", text(r.get(3)), "description", text(r.get(4))));
            }
        }
        markKeys(tables, cur.rows("""
                SELECT n.nspname, c.relname, a.attname
                  FROM pg_index i
                  JOIN pg_class c ON c.oid = i.indrelid
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
                 WHERE i.indisprimary AND n.nspname = ANY(?)
                """, names), defaultSchema);
        List<Map<String, Object>> relations = readRelations(row, cur, """
                SELECT con.conname, n.nspname, cl.relname, att.attname, rn.nspname, rcl.relname, ratt.attname
                  FROM pg_constraint con
                  JOIN pg_class cl ON cl.oid = con.conrelid
                  JOIN pg_namespace n ON n.oid = cl.relnamespace
                  JOIN pg_class rcl ON rcl.oid = con.confrelid
                  JOIN pg_namespace rn ON rn.oid = rcl.relnamespace
                  JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY AS k(attnum, refnum, ord) ON true
                  JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = k.attnum
                  JOIN pg_attribute ratt ON ratt.attrelid = con.confrelid AND ratt.attnum = k.refnum
                 WHERE con.contype = 'f' AND n.nspname = ANY(?)
                 ORDER BY n.nspname, con.conname, k.ord
                """, new Object[] {names}, tables, defaultSchema);
        return new Read(new ArrayList<>(tables.values()), relations);
    }

    private Read readMysql(Map<String, Object> row, TargetSession cur) throws SQLException {
        String db = (String) row.get("db_name");
        Map<String, Map<String, Object>> tables = tableMap(cur.rows(
                "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_COMMENT FROM information_schema.TABLES"
                        + " WHERE TABLE_SCHEMA = ? AND TABLE_TYPE IN ('BASE TABLE','VIEW')"
                        + " ORDER BY TABLE_NAME", db), db);
        for (List<Object> r : cur.rows(
                "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT, COLUMN_KEY"
                        + "  FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = ?"
                        + " ORDER BY TABLE_NAME, ORDINAL_POSITION", db)) {
            Map<String, Object> table = tables.get(text(r.get(0)));
            if (table != null) {
                columns(table).add(Dyn.of("name", text(r.get(1)), "type", text(r.get(2)), "description",
                        text(r.get(3)), "is_pk", "PRI".equals(text(r.get(4)))));
            }
        }
        List<Map<String, Object>> relations = readRelations(row, cur, """
                SELECT CONSTRAINT_NAME, TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME,
                       REFERENCED_TABLE_SCHEMA, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
                  FROM information_schema.KEY_COLUMN_USAGE
                 WHERE TABLE_SCHEMA = ? AND REFERENCED_TABLE_SCHEMA = ? AND REFERENCED_TABLE_NAME IS NOT NULL
                 ORDER BY CONSTRAINT_NAME, ORDINAL_POSITION
                """, new Object[] {db, db}, tables, db);
        return new Read(new ArrayList<>(tables.values()), relations);
    }

    private Read readOracle(Map<String, Object> row, TargetSession cur) throws SQLException {
        List<String> names = schemas(row);
        String defaultSchema = names.get(0);
        List<String> marks = new ArrayList<>();
        for (int i = 0; i < names.size(); i++) {
            marks.add("?");
        }
        String owners = "(" + String.join(", ", marks) + ")";
        Object[] binds = names.toArray();

        Map<String, Map<String, Object>> tables = tableMap(cur.rows(
                "SELECT owner, table_name, comments FROM all_tab_comments"
                        + " WHERE owner IN " + owners + " AND table_type IN ('TABLE','VIEW')"
                        + " ORDER BY owner, table_name", binds), defaultSchema);
        for (List<Object> r : cur.rows("""
                SELECT c.owner, c.table_name, c.column_name, c.data_type, c.data_length,
                       c.data_precision, c.data_scale, cc.comments
                  FROM all_tab_columns c
                  LEFT JOIN all_col_comments cc
                    ON cc.owner = c.owner AND cc.table_name = c.table_name
                   AND cc.column_name = c.column_name
                 WHERE c.owner IN %s
                 ORDER BY c.owner, c.table_name, c.column_id
                """.formatted(owners), binds)) {
            Map<String, Object> table = tables.get(qualified(text(r.get(0)), text(r.get(1)), defaultSchema));
            if (table != null) {
                columns(table).add(Dyn.of("name", text(r.get(2)), "type",
                        oracleType(r.get(3), r.get(4), r.get(5), r.get(6)), "description", text(r.get(7))));
            }
        }
        markKeys(tables, cur.rows("""
                SELECT cc.owner, cc.table_name, cc.column_name
                  FROM all_constraints c
                  JOIN all_cons_columns cc ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name
                 WHERE c.owner IN %s AND c.constraint_type = 'P'
                """.formatted(owners), binds), defaultSchema);
        List<Map<String, Object>> relations = readRelations(row, cur, """
                SELECT c.constraint_name, cc.owner, cc.table_name, cc.column_name, rc.owner, rc.table_name, rcc.column_name
                  FROM all_constraints c
                  JOIN all_cons_columns cc ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name
                  JOIN all_constraints rc ON rc.owner = c.r_owner AND rc.constraint_name = c.r_constraint_name
                  JOIN all_cons_columns rcc ON rcc.owner = rc.owner AND rcc.constraint_name = rc.constraint_name
                                           AND rcc.position = cc.position
                 WHERE c.owner IN %s AND c.constraint_type = 'R'
                 ORDER BY c.owner, c.constraint_name, cc.position
                """.formatted(owners), binds, tables, defaultSchema);
        return new Read(new ArrayList<>(tables.values()), relations);
    }

    /**
     * 메타데이터 테이블의 B-tree 인덱스 [{name, columns}] (columns 는 인덱스 순서).
     *
     * 식·함수 인덱스 칸, 앞부분만 거는 칸(MySQL prefix), 모르는 컬럼이 나오면 그 앞까지만 남긴다 —
     * 인덱스는 앞 칸부터 쓰이므로 그 뒤 칸은 조회에 쓸 수 없다. 부분 인덱스(PostgreSQL WHERE)는 뺀다.
     */
    public static List<Map<String, Object>> readIndexes(TargetSession cur, Map<String, Object> row,
            Map<String, Object> table, Set<String> columns) throws SQLException {
        String[] parts = tableParts(row, table);
        String schema = parts[0];
        String name = parts[1];
        List<String[]> fetched = new ArrayList<>();
        switch ((String) row.get("driver")) {
            case "mysql" -> {
                for (List<Object> r : cur.rows(
                        "SELECT INDEX_NAME, COLUMN_NAME, SUB_PART FROM information_schema.STATISTICS"
                                + " WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND INDEX_TYPE = 'BTREE'"
                                + " ORDER BY INDEX_NAME = 'PRIMARY' DESC, INDEX_NAME, SEQ_IN_INDEX", schema, name)) {
                    fetched.add(new String[] {text(r.get(0)), r.get(2) != null || r.get(1) == null ? null : text(r.get(1))});
                }
            }
            case "oracle" -> {
                for (List<Object> r : cur.rows("""
                        SELECT ic.index_name, ic.column_name
                          FROM all_indexes i
                          JOIN all_ind_columns ic ON ic.index_owner = i.owner AND ic.index_name = i.index_name
                         WHERE i.table_owner = ? AND i.table_name = ? AND i.index_type = 'NORMAL'
                           AND i.status IN ('VALID', 'N/A')
                         ORDER BY ic.index_name, ic.column_position
                        """, schema, name)) {
                    fetched.add(new String[] {text(r.get(0)), r.get(1) == null ? null : text(r.get(1))});
                }
            }
            default -> {
                for (List<Object> r : cur.rows("""
                        SELECT ic.relname, a.attname
                          FROM pg_index x
                          JOIN pg_class c ON c.oid = x.indrelid
                          JOIN pg_namespace n ON n.oid = c.relnamespace
                          JOIN pg_class ic ON ic.oid = x.indexrelid
                          JOIN pg_am am ON am.oid = ic.relam
                          JOIN LATERAL unnest(x.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) ON k.ord <= x.indnkeyatts
                          LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum AND k.attnum > 0
                         WHERE n.nspname = ? AND c.relname = ?
                           AND x.indisvalid AND x.indpred IS NULL AND am.amname = 'btree'
                         ORDER BY x.indisprimary DESC, ic.relname, k.ord
                        """, schema, name)) {
                    fetched.add(new String[] {text(r.get(0)), r.get(1) == null ? null : text(r.get(1))});
                }
            }
        }

        List<Map<String, Object>> indexes = new ArrayList<>();
        Set<String> cut = new HashSet<>();
        for (String[] f : fetched) {
            String indexName = f[0];
            String column = f[1];
            if (cut.contains(indexName)) {
                continue;
            }
            if (indexes.isEmpty() || !indexes.get(indexes.size() - 1).get("name").equals(indexName)) {
                indexes.add(Dyn.of("name", indexName, "columns", new ArrayList<String>()));
            }
            if (column == null || !columns.contains(column)) {
                cut.add(indexName);
                continue;
            }
            Dyn.items(indexes.get(indexes.size() - 1).get("columns")).add(column);
        }
        List<Map<String, Object>> usable = new ArrayList<>();
        for (Map<String, Object> index : indexes) {
            if (!Dyn.items(index.get("columns")).isEmpty()) {
                usable.add(index);
            }
        }
        return usable;
    }

    /**
     * FK 를 관계 목록으로. 두 테이블이 모두 읽은 범위에 있을 때만 남긴다.
     *
     * 실패해도 동기화는 계속한다 — 관계는 있으면 좋은 것이고, 테이블·컬럼이 본체다.
     */
    private List<Map<String, Object>> readRelations(Map<String, Object> row, TargetSession cur, String sql,
            Object[] params, Map<String, Map<String, Object>> tables, String defaultSchema) {
        List<List<Object>> fetched;
        try {
            fetched = cur.rows(sql, params);
        } catch (SQLException error) {
            log.warn("{} 의 FK 를 읽지 못했습니다 (기존 FK 관계를 유지합니다): {}", row.get("name"), error.getMessage());
            cur.rollback();
            return null;
        }
        List<Map<String, Object>> out = new ArrayList<>();
        for (List<Object> r : fetched) {
            String table = qualified(text(r.get(1)), text(r.get(2)), defaultSchema);
            String refTable = qualified(text(r.get(4)), text(r.get(5)), defaultSchema);
            if (tables.containsKey(table) && tables.containsKey(refTable)) {
                out.add(Dyn.of("constraint", text(r.get(0)), "left_table", table, "left_column", text(r.get(3)),
                        "right_table", refTable, "right_column", text(r.get(6))));
            }
        }
        return out;
    }

    /** 기본키 컬럼 표시. SQL 생성 단계에서 컬럼을 골라 실을 때 항상 포함한다. */
    private static void markKeys(Map<String, Map<String, Object>> tables, List<List<Object>> rows, String defaultSchema) {
        Set<String> keys = new HashSet<>();
        for (List<Object> r : rows) {
            keys.add(qualified(text(r.get(0)), text(r.get(1)), defaultSchema) + "\u0000" + text(r.get(2)));
        }
        for (Map<String, Object> table : tables.values()) {
            for (Map<String, Object> column : columns(table)) {
                column.put("is_pk", keys.contains(table.get("name") + "\u0000" + column.get("name")));
            }
        }
    }

    /**
     * (스키마, 테이블 이름, 코멘트) 행 → {이름: 빈 컬럼 목록을 가진 테이블}
     *
     * schema 는 기본 스키마가 아닌 테이블만 채운다 (meta_table.db_schema).
     */
    private static Map<String, Map<String, Object>> tableMap(List<List<Object>> rows, String defaultSchema) {
        Map<String, Map<String, Object>> tables = new LinkedHashMap<>();
        for (List<Object> r : rows) {
            String schema = text(r.get(0));
            String name = qualified(schema, text(r.get(1)), defaultSchema);
            tables.put(name, Dyn.of("name", name, "schema", schema.equals(defaultSchema) ? "" : schema,
                    "description", text(r.get(2)), "columns", new ArrayList<Map<String, Object>>()));
        }
        return tables;
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> columns(Map<String, Object> table) {
        return (List<Map<String, Object>>) table.get("columns");
    }

    /** 값을 문자열로 정규화 */
    static String text(Object value) {
        if (value instanceof byte[] bytes) {
            return new String(bytes, java.nio.charset.StandardCharsets.UTF_8);
        }
        return value == null ? "" : String.valueOf(value);
    }

    /** Oracle 타입을 길이까지 포함한 형태로 */
    private static String oracleType(Object name, Object length, Object precision, Object scale) {
        String base = text(name).toUpperCase();
        if (base.equals("NUMBER")) {
            if (precision == null) {
                return "NUMBER";
            }
            int p = ((Number) precision).intValue();
            int s = scale == null ? 0 : ((Number) scale).intValue();
            return s != 0 ? "NUMBER(" + p + "," + s + ")" : "NUMBER(" + p + ")";
        }
        if (List.of("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW").contains(base) && length instanceof Number n
                && n.intValue() != 0) {
            return base + "(" + n.intValue() + ")";
        }
        return base;
    }

    /** DB 접속 실패 예외 */
    private static ApiException unreachable(Map<String, Object> row, Exception error) {
        return new ApiException(502, row.get("name") + "의 스키마를 읽지 못했습니다 (" + row.get("host") + ":"
                + row.get("port") + "/" + row.get("db_name") + "): " + Py.firstLine(error));
    }
}
