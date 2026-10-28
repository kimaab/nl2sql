package com.nl2sql.db;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.util.List;
import java.util.Map;
import java.util.Properties;

import org.springframework.stereotype.Component;

import com.nl2sql.common.ApiException;
import com.nl2sql.common.Env;
import com.nl2sql.common.Py;

/**
 * 대상 DB 접속. 접속 정보는 meta_system 행 (SystemService.connectionRow — 비밀번호를 푼 것).
 *
 * readOnly 는 읽기 전용 트랜잭션 하나를 준다 (파이썬 metrics.read_only). 쿼리 하나가 timeoutS 를 넘으면 끊는다.
 * PostgreSQL 은 REPEATABLE READ, MySQL 은 CONSISTENT SNAPSHOT 이라 트랜잭션 안의 쿼리가 같은 스냅샷을 본다.
 */
@Component
public class TargetDb {

    public static final int CONNECT_TIMEOUT_S = 10;

    private final boolean sqlLog;

    public TargetDb(Env env) {
        this.sqlLog = env.flag("SQL_LOG");
    }

    /** 읽기 전용 트랜잭션. 닫으면 되돌리고 접속을 끊는다. */
    public TargetSession readOnly(Map<String, Object> row, int timeoutS) {
        String driver = (String) row.get("driver");
        Connection conn;
        try {
            conn = connect(row, timeoutS);
        } catch (SQLException error) {
            throw new ApiException(502, row.get("name") + " 에 접속하지 못했습니다: " + Py.firstLine(error));
        }
        TargetSession session = new TargetSession(conn, driver, timeoutS, sqlLog);
        try {
            conn.setAutoCommit(false);
            switch (driver) {
                case "postgresql" -> {
                    conn.setTransactionIsolation(Connection.TRANSACTION_REPEATABLE_READ);
                    conn.setReadOnly(true);
                    session.execute("SET LOCAL statement_timeout = '" + timeoutS + "s'");
                    // 기본 스키마의 테이블은 이름만 쓴다. 다른 스키마의 테이블은 '스키마.테이블' 로 감싸 나간다
                    if (hasSchemas(row)) {
                        session.rows("SELECT set_config('search_path', ?, true)", Catalog.schemas(row).get(0));
                    }
                }
                case "mysql" -> session.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY");
                default -> {
                    if (hasSchemas(row)) {  // 스키마 이름은 SystemInput 이 따옴표를 막는다
                        session.execute("ALTER SESSION SET CURRENT_SCHEMA = \"" + Catalog.schemas(row).get(0) + "\"");
                    }
                    session.execute("SET TRANSACTION READ ONLY");
                }
            }
        } catch (SQLException | RuntimeException error) {
            session.close();
            throw new ApiException(502,
                    row.get("name") + " 에서 읽기 전용 트랜잭션을 열지 못했습니다: " + Py.firstLine(error));
        }
        return session;
    }

    /** 카탈로그 읽기용 접속 (트랜잭션 없이). 실패하면 그대로 SQLException. */
    public TargetSession plain(Map<String, Object> row, int timeoutS) throws SQLException {
        Connection conn = connect(row, timeoutS);
        conn.setReadOnly(true);
        return new TargetSession(conn, (String) row.get("driver"), timeoutS, sqlLog);
    }

    private static boolean hasSchemas(Map<String, Object> row) {
        Object schemas = row.get("db_schemas");
        return schemas instanceof List<?> list && !list.isEmpty();
    }

    private static Connection connect(Map<String, Object> row, int timeoutS) throws SQLException {
        String driver = (String) row.get("driver");
        String host = (String) row.get("host");
        Object port = row.get("port");
        String db = (String) row.get("db_name");
        Properties props = new Properties();
        String user = (String) row.get("username");
        String password = (String) row.get("password");
        if (user != null && !user.isEmpty()) {
            props.setProperty("user", user);
        }
        if (password != null && !password.isEmpty()) {
            props.setProperty("password", password);
        }
        String url;
        switch (driver) {
            case "mysql" -> {
                url = "jdbc:mysql://" + host + ":" + port + "/" + db;
                props.setProperty("connectTimeout", String.valueOf(CONNECT_TIMEOUT_S * 1000));
                props.setProperty("socketTimeout", String.valueOf(timeoutS * 1000));
                props.setProperty("characterEncoding", "UTF-8");
            }
            case "oracle" -> {
                url = "jdbc:oracle:thin:@//" + host + ":" + port + "/" + db;
                props.setProperty("oracle.net.CONNECT_TIMEOUT", String.valueOf(CONNECT_TIMEOUT_S * 1000));
            }
            default -> {
                url = "jdbc:postgresql://" + host + ":" + port + "/" + db;
                props.setProperty("connectTimeout", String.valueOf(CONNECT_TIMEOUT_S));
            }
        }
        return DriverManager.getConnection(url, props);
    }
}
