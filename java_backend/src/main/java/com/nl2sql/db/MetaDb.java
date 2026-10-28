package com.nl2sql.db;

import java.net.URI;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.sql.Array;
import java.sql.ResultSet;
import java.sql.ResultSetMetaData;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Consumer;

import javax.sql.DataSource;

import org.postgresql.util.PGobject;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;

import com.nl2sql.common.Env;
import com.nl2sql.common.Json;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;

/**
 * 메타데이터 DB (PostgreSQL). 파이썬 db.py 의 one · query · connection 자리다.
 *
 * 행은 {컬럼: 값} 으로 준다. jsonb 는 Map · List 로, text[] 는 List 로, timestamptz 는 OffsetDateTime 으로 바꾼다.
 * 파라미터의 문자열은 타입을 정하지 않고 보낸다 (stringtype=unspecified) — uuid · jsonb 컬럼에 문자열을 그대로 넣는다.
 */
@Component
public class MetaDb {

    private static final Logger log = LoggerFactory.getLogger("nl2sql.sql");

    private final HikariDataSource dataSource;
    private final JdbcTemplate jdbc;
    private final TransactionTemplate tx;
    private final boolean sqlLog;
    private final java.time.ZoneId zone;  // timestamptz 를 이 시간대로 준다 (psycopg 처럼 DB 세션 시간대)

    public MetaDb(Env env) {
        String url = env.get("DB_URL");
        if (url.isEmpty()) {
            throw new IllegalStateException("DB_URL이 설정되지 않았습니다 (backend/.env를 확인하십시오)");
        }
        this.dataSource = new HikariDataSource(config(url));
        this.jdbc = new JdbcTemplate(dataSource);
        this.tx = new TransactionTemplate(new DataSourceTransactionManager(dataSource));
        this.sqlLog = env.flag("SQL_LOG");
        this.zone = sessionZone(jdbc);
    }

    private static java.time.ZoneId sessionZone(JdbcTemplate jdbc) {
        try {
            return java.time.ZoneId.of(jdbc.queryForObject("SELECT current_setting('TimeZone')", String.class));
        } catch (RuntimeException error) {
            return java.time.ZoneId.systemDefault();
        }
    }

    public DataSource dataSource() {
        return dataSource;
    }

    /** postgresql://user:password@host:port/db?… (libpq 모양) 이나 jdbc:postgresql://… 를 받는다 */
    static HikariConfig config(String dbUrl) {
        HikariConfig config = new HikariConfig();
        config.setPoolName("meta");
        config.setConnectionTimeout(10_000);
        if (dbUrl.startsWith("jdbc:")) {
            config.setJdbcUrl(withParam(dbUrl, "stringtype=unspecified"));
            return config;
        }
        URI uri = URI.create(dbUrl.replaceFirst("^postgres(ql)?(\\+\\w+)?://", "postgresql://"));
        String userInfo = uri.getRawUserInfo();
        if (userInfo != null) {
            int colon = userInfo.indexOf(':');
            config.setUsername(decode(colon < 0 ? userInfo : userInfo.substring(0, colon)));
            if (colon >= 0) {
                config.setPassword(decode(userInfo.substring(colon + 1)));
            }
        }
        String host = uri.getHost() == null ? "localhost" : uri.getHost();
        int port = uri.getPort() < 0 ? 5432 : uri.getPort();
        String jdbcUrl = "jdbc:postgresql://" + host + ":" + port + uri.getRawPath();
        if (uri.getRawQuery() != null) {
            jdbcUrl += "?" + uri.getRawQuery();
        }
        config.setJdbcUrl(withParam(jdbcUrl, "stringtype=unspecified"));
        return config;
    }

    private static String withParam(String url, String param) {
        return url + (url.contains("?") ? "&" : "?") + param;
    }

    private static String decode(String text) {
        return URLDecoder.decode(text, StandardCharsets.UTF_8);
    }

    // ── 조회 ────────────────────────────────────────────────────────────────

    /** 한 행 (없으면 null) */
    public Map<String, Object> one(String sql, Object... args) {
        List<Map<String, Object>> rows = query(sql, args);
        return rows.isEmpty() ? null : rows.get(0);
    }

    /** 여러 행 */
    public List<Map<String, Object>> query(String sql, Object... args) {
        long started = System.nanoTime();
        try {
            return jdbc.query(sql, rowMapper, args);
        } finally {
            logged(sql, args, started);
        }
    }

    /** INSERT · UPDATE · DELETE → 바뀐 행 수 */
    public int update(String sql, Object... args) {
        long started = System.nanoTime();
        try {
            return jdbc.update(sql, args);
        } finally {
            logged(sql, args, started);
        }
    }

    /** 같은 문장을 여러 값으로 */
    public void batch(String sql, List<Object[]> rows) {
        if (rows.isEmpty()) {
            return;
        }
        long started = System.nanoTime();
        try {
            jdbc.batchUpdate(sql, rows);
        } finally {
            if (sqlLog) {
                log.info("{}ms ({}건) {}", (System.nanoTime() - started) / 1_000_000, rows.size(), squash(sql));
            }
        }
    }

    /** 트랜잭션 하나. 블록 안의 one · query · update · batch 가 같은 트랜잭션에 든다. 예외가 나면 되돌린다. */
    public void transaction(Consumer<MetaDb> work) {
        tx.executeWithoutResult(status -> work.accept(this));
    }

    /** SQL 스크립트 (schema.sql) 를 한 번에 */
    public void script(String sql) {
        jdbc.execute(sql);
    }

    public void close() {
        dataSource.close();
    }

    /** text[] 파라미터 (값을 따옴표로 감싼 배열 글) */
    public static String array(List<String> values) {
        StringBuilder out = new StringBuilder("{");
        for (int i = 0; i < values.size(); i++) {
            if (i > 0) {
                out.append(',');
            }
            out.append('"').append(values.get(i).replace("\\", "\\\\").replace("\"", "\\\"")).append('"');
        }
        return out.append('}').toString();
    }

    private void logged(String sql, Object[] args, long started) {
        if (sqlLog) {
            log.info("{}ms {} -- params={}", (System.nanoTime() - started) / 1_000_000, squash(sql),
                    java.util.Arrays.toString(args));
        }
    }

    private static String squash(String sql) {
        return sql.strip().replaceAll("\\s+", " ");
    }

    private final RowMapper<Map<String, Object>> rowMapper = this::row;

    private Map<String, Object> row(ResultSet rs, int rowNum) throws SQLException {
        ResultSetMetaData meta = rs.getMetaData();
        Map<String, Object> out = new LinkedHashMap<>();
        for (int i = 1; i <= meta.getColumnCount(); i++) {
            Object value = value(rs, i, meta.getColumnTypeName(i));
            if (value instanceof OffsetDateTime t) {
                value = t.atZoneSameInstant(zone).toOffsetDateTime();
            }
            out.put(meta.getColumnLabel(i), value);
        }
        return out;
    }

    private static Object value(ResultSet rs, int i, String type) throws SQLException {
        if ("timestamptz".equals(type)) {
            return rs.getObject(i, OffsetDateTime.class);
        }
        Object value = rs.getObject(i);
        if (value == null) {
            return null;
        }
        if (value instanceof PGobject pg && ("json".equals(pg.getType()) || "jsonb".equals(pg.getType()))) {
            return pg.getValue() == null ? null : Json.parse(pg.getValue());
        }
        if (value instanceof Array array) {
            List<Object> out = new ArrayList<>();
            for (Object item : (Object[]) array.getArray()) {
                out.add(item);
            }
            return out;
        }
        return value;
    }
}
