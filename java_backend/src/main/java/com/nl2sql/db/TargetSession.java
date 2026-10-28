package com.nl2sql.db;

import java.sql.Clob;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.ResultSetMetaData;
import java.sql.SQLException;
import java.sql.Statement;
import java.sql.Timestamp;
import java.sql.Types;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * 대상 DB 접속 하나 (파이썬의 커서 자리). 값은 자바 시각 타입으로 바꿔 준다:
 * timestamp → LocalDateTime, timestamptz → OffsetDateTime, date → LocalDate, time → LocalTime.
 */
public class TargetSession implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger("nl2sql.sql");

    /** 결과: 컬럼 이름과 행 */
    public record Result(List<String> columns, List<List<Object>> rows) {
    }

    private final Connection conn;
    private final String driver;
    private final int timeoutS;
    private final boolean sqlLog;

    TargetSession(Connection conn, String driver, int timeoutS, boolean sqlLog) {
        this.conn = conn;
        this.driver = driver;
        this.timeoutS = timeoutS;
        this.sqlLog = sqlLog;
    }

    public String driver() {
        return driver;
    }

    /** 결과 없는 문장 */
    public void execute(String sql) throws SQLException {
        long started = System.nanoTime();
        try (Statement st = conn.createStatement()) {
            st.setQueryTimeout(timeoutS);
            st.execute(sql);
        } finally {
            logged(sql, List.of(), started);
        }
    }

    /** 모든 행 */
    public List<List<Object>> rows(String sql, Object... params) throws SQLException {
        return query(sql, -1, params).rows();
    }

    /** 첫 행 (없으면 null) */
    public List<Object> one(String sql, Object... params) throws SQLException {
        List<List<Object>> rows = query(sql, 1, params).rows();
        return rows.isEmpty() ? null : rows.get(0);
    }

    /** max 행까지 읽는다 (음수면 모두). 목록 파라미터는 배열로 보낸다 (PostgreSQL = ANY(?)). */
    public Result query(String sql, int max, Object... params) throws SQLException {
        long started = System.nanoTime();
        try (PreparedStatement st = conn.prepareStatement(sql)) {
            st.setQueryTimeout(timeoutS);
            for (int i = 0; i < params.length; i++) {
                Object p = params[i];
                if (p instanceof List<?> list) {
                    st.setArray(i + 1, conn.createArrayOf("text", list.toArray()));
                } else if (p == null) {
                    st.setNull(i + 1, Types.VARCHAR);
                } else {
                    st.setObject(i + 1, p);
                }
            }
            try (ResultSet rs = st.executeQuery()) {
                ResultSetMetaData meta = rs.getMetaData();
                List<String> columns = new ArrayList<>();
                for (int i = 1; i <= meta.getColumnCount(); i++) {
                    columns.add(meta.getColumnLabel(i));
                }
                List<List<Object>> rows = new ArrayList<>();
                while ((max < 0 || rows.size() < max) && rs.next()) {
                    List<Object> row = new ArrayList<>();
                    for (int i = 1; i <= columns.size(); i++) {
                        row.add(value(rs, i, meta));
                    }
                    rows.add(row);
                }
                return new Result(columns, rows);
            }
        } finally {
            logged(sql, List.of(params), started);
        }
    }

    /** 실패한 문장 뒤에도 접속을 쓸 수 있게 되돌린다 (PostgreSQL 은 오류 뒤 트랜잭션이 막힌다) */
    public void rollback() {
        try {
            if (!conn.getAutoCommit()) {
                conn.rollback();
            }
        } catch (SQLException ignored) {
            // 되돌리지 못해도 닫을 때 끝난다
        }
    }

    @Override
    public void close() {
        try {
            rollback();
        } finally {
            try {
                conn.close();
            } catch (SQLException ignored) {
                // 이미 끊겼다
            }
        }
    }

    private static Object value(ResultSet rs, int i, ResultSetMetaData meta) throws SQLException {
        String type = meta.getColumnTypeName(i).toLowerCase();
        int sqlType = meta.getColumnType(i);
        if (type.equals("timestamptz") || type.contains("with time zone") || sqlType == Types.TIMESTAMP_WITH_TIMEZONE) {
            try {
                return rs.getObject(i, OffsetDateTime.class);
            } catch (SQLException error) {
                // 드라이버가 OffsetDateTime 을 못 주면 아래에서 Timestamp 로 읽는다
            }
        }
        Object value = rs.getObject(i);
        if (value == null) {
            return null;
        }
        if (value instanceof Timestamp ts) {
            return ts.toLocalDateTime();
        }
        if (value instanceof java.sql.Date d) {
            return d.toLocalDate();
        }
        if (value instanceof java.sql.Time t) {
            return t.toLocalTime();
        }
        if (value instanceof Clob clob) {
            return clob.getSubString(1, (int) Math.min(clob.length(), Integer.MAX_VALUE));
        }
        if (value.getClass().getName().startsWith("oracle.")) {
            if (type.contains("timestamp") || type.equals("date")) {
                Timestamp timestamp = rs.getTimestamp(i);
                return timestamp == null ? null : timestamp.toLocalDateTime();
            }
            return value.toString();
        }
        if (value instanceof org.postgresql.util.PGobject pg) {
            return pg.getValue();
        }
        return value;
    }

    private void logged(String sql, List<Object> params, long started) {
        if (sqlLog) {
            log.info("{}ms {}{}", (System.nanoTime() - started) / 1_000_000, sql.strip().replaceAll("\\s+", " "),
                    params.isEmpty() ? "" : " -- params=" + params);
        }
    }
}
