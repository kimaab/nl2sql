package com.nl2sql.compiler;

import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.Map;

import com.nl2sql.common.Py;

/** 드라이버마다 다른 문법 */
public class Dialect {

    private static final Map<String, String> ORACLE_TRUNC =
            Map.of("hour", "HH24", "day", "DD", "week", "IW", "month", "MM", "year", "YYYY");
    private static final DateTimeFormatter STAMP = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss");

    private final String driver;
    private final String quoteChar;

    public Dialect(String driver) {
        this.driver = driver == null || driver.isEmpty() ? "postgresql" : driver;
        this.quoteChar = this.driver.equals("mysql") ? "`" : "\"";
    }

    public String driver() {
        return driver;
    }

    public String q(String name) {
        if (name.contains(quoteChar) || name.contains("\u0000")) {
            throw new AstError("이름에 " + quoteChar + " 나 NUL 을 쓸 수 없습니다 -> '" + name + "'");
        }
        return quoteChar + name + quoteChar;
    }

    /** 테이블 이름. 기본 스키마가 아닌 테이블(이름이 '스키마.테이블')은 스키마와 테이블을 따로 감싼다. */
    public String table(String name, String schema) {
        if (schema != null && !schema.isEmpty() && name.startsWith(schema + ".")) {
            return q(schema) + "." + q(name.substring(schema.length() + 1));
        }
        return q(name);
    }

    public String literal(Object value) {
        if (value == null) {
            return "NULL";
        }
        if (value instanceof Boolean b) {
            return b ? "TRUE" : "FALSE";
        }
        if (value instanceof Number n) {
            return Py.repr(n);
        }
        String text = String.valueOf(value).replace("'", "''");
        if (driver.equals("mysql")) {
            text = text.replace("\\", "\\\\");
        }
        return "'" + text + "'";
    }

    /** unit: seconds | minutes | hours | days */
    public String addInterval(String sql, double n, String unit) {
        String sign = n >= 0 ? "+" : "-";
        String amount = Py.g(Math.abs(n));
        String word = switch (unit) {
            case "seconds" -> "SECOND";
            case "minutes" -> "MINUTE";
            case "hours" -> "HOUR";
            default -> "DAY";
        };
        String interval;
        if (driver.equals("mysql")) {
            interval = "INTERVAL " + amount + " " + word;
        } else if (driver.equals("oracle")) {
            interval = "INTERVAL '" + amount + "' " + word;
        } else {
            interval = "INTERVAL '" + amount + " " + unit + "'";
        }
        return "(" + sql + " " + sign + " " + interval + ")";
    }

    public String timestamp(LocalDateTime moment) {
        return "TIMESTAMP '" + moment.format(STAMP) + "'";
    }

    public String date(String sql) {
        if (driver.equals("mysql")) {
            return "DATE(" + sql + ")";
        }
        if (driver.equals("oracle")) {
            return "TRUNC(" + sql + ")";
        }
        return "CAST(" + sql + " AS DATE)";
    }

    public String trunc(String unit, String sql) {
        if (driver.equals("oracle")) {
            return "TRUNC(" + sql + ", '" + ORACLE_TRUNC.get(unit) + "')";
        }
        if (driver.equals("mysql")) {
            return switch (unit) {
                case "hour" -> "DATE_FORMAT(" + sql + ", '%Y-%m-%d %H:00:00')";
                case "day" -> "DATE(" + sql + ")";
                case "week" -> "DATE_SUB(DATE(" + sql + "), INTERVAL WEEKDAY(" + sql + ") DAY)";
                case "month" -> "DATE_FORMAT(" + sql + ", '%Y-%m-01')";
                default -> "DATE_FORMAT(" + sql + ", '%Y-01-01')";
            };
        }
        return "DATE_TRUNC('" + unit + "', " + sql + ")";
    }

    public String currentDate() {
        return driver.equals("oracle") ? "TRUNC(SYSDATE)" : "CURRENT_DATE";
    }

    public String limit(int n) {
        return driver.equals("oracle") ? "FETCH FIRST " + n + " ROWS ONLY" : "LIMIT " + n;
    }

    /** 값을 문자로 (타입 갈래가 다른 컬럼끼리 비교할 때) */
    public String text(String sql) {
        if (driver.equals("oracle")) {
            return "TO_CHAR(" + sql + ")";
        }
        return "CAST(" + sql + " AS " + (driver.equals("mysql") ? "CHAR" : "VARCHAR") + ")";
    }

    /** 재귀 WITH 머리. Oracle 은 RECURSIVE 를 쓰지 않는다 */
    public String recursive() {
        return driver.equals("oracle") ? "WITH" : "WITH RECURSIVE";
    }
}
