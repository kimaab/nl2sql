package com.nl2sql.model;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

import com.nl2sql.common.ApiException;

/** 시스템 · 동기화 · 테이블 정보 (models.py) */
public final class SystemModels {

    private SystemModels() {
    }

    private static final Pattern CODE = Pattern.compile("[A-Za-z0-9_-]{1,40}");
    private static final Pattern BAD_SCHEMA = Pattern.compile("[.,\"`\\x00]");

    public static int defaultPort(String driver) {
        return switch (driver) {
            case "mysql" -> 3306;
            case "oracle" -> 1521;
            default -> 5432;
        };
    }

    /** 시스템 등록 화면이 보내는 것. 수정할 때 password 를 비우면 기존 비밀번호를 둔다. */
    public record SystemInput(
            String code,
            String name,
            String domainDesc,
            String driver,
            String host,
            Integer port,
            String dbName,
            List<String> dbSchemas,  // 첫 스키마가 기본 스키마. 비우면 PostgreSQL 은 public, Oracle 은 계정
            String username,
            String password) {

        /** 검증하고 다듬은 값 (pydantic 의 validator 와 같은 규칙). 틀리면 422. */
        public SystemInput validated() {
            String cleanName = name == null ? "" : name.strip();
            if (cleanName.isEmpty()) {
                throw invalid("name: must not be blank");
            }
            String cleanCode = code == null ? "" : code.strip();
            if (!CODE.matcher(cleanCode).matches()) {
                throw invalid("코드는 영문·숫자·_·- 로 40자 이내여야 합니다");
            }
            if (driver == null || !List.of("mysql", "postgresql", "oracle").contains(driver)) {
                throw invalid("driver 는 mysql, postgresql, oracle 중 하나입니다");
            }
            if (host == null || host.isBlank()) {
                throw invalid("host: must not be blank");
            }
            if (dbName == null || dbName.isBlank()) {
                throw invalid("db_name: must not be blank");
            }
            List<String> schemas = cleanNames(dbSchemas);
            for (String s : schemas) {
                if (BAD_SCHEMA.matcher(s).find()) {
                    throw invalid("스키마 이름에 . , \" ` 를 쓸 수 없습니다 -> " + s);
                }
            }
            int p = port == null || port == 0 ? defaultPort(driver) : port;
            if (p < 1 || p > 65535) {
                throw invalid("port must be between 1 and 65535");
            }
            return new SystemInput(cleanCode, cleanName, domainDesc == null ? "" : domainDesc, driver, host.strip(), p,
                    dbName.strip(), schemas, username == null ? "" : username, password == null ? "" : password);
        }
    }

    /** 저장된 시스템 (비밀번호는 내보내지 않는다) */
    public record SystemInfo(
            String id,
            String code,
            String name,
            String domainDesc,
            String driver,
            String host,
            int port,
            String dbName,
            List<String> dbSchemas,
            String username,
            String syncedAt,
            int tableCount,
            int metricCount,
            int activeMetricCount,
            Integer supersetDatabaseId) {
    }

    /** 동기화 결과. 병합이라 추가·변경·삭제 건수로 말한다. */
    public record SyncResult(
            int tableCount,
            int columnCount,
            int tablesAdded,
            int tablesChanged,
            int tablesRemoved,
            String syncedAt,
            Integer relationCount,
            List<Map<String, Object>> brokenMetrics) {

        public SyncResult withBroken(List<Map<String, Object>> broken) {
            return new SyncResult(tableCount, columnCount, tablesAdded, tablesChanged, tablesRemoved, syncedAt,
                    relationCount, broken);
        }
    }

    public record SyncLog(
            long id,
            String systemId,
            String systemName,
            String trigger,
            String status,
            Integer tablesAdded,
            Integer tablesChanged,
            Integer tablesRemoved,
            List<Object> brokenMetrics,
            String error,
            String startedAt,
            String finishedAt) {
    }

    /** 시각을 포함한 날짜 컬럼과 UTC 보정값 (null = 보정 없음) */
    public record TimeColumn(String name, String dataType, Integer utcOffsetHours) {
    }

    /** 컬럼과 '계산 필요' 표시. 계산 필요 컬럼을 계산해야 답할 수 있는 질문은 지표가 필요하다. */
    public record ColumnInfo(
            String name,
            String dataType,
            String comment,
            String aiComment,  // 코멘트가 없을 때 추론한 설명 (DescribeService)
            String note,       // 사람이 적은 설명. 코멘트와 추론한 설명보다 먼저 쓴다
            boolean needsCalc) {
    }

    /** 테이블 목록 화면용 */
    public record TableInfo(
            String id,
            String name,
            String comment,
            String aiComment,
            String purpose,
            int columnCount,
            int metricCount,
            List<TimeColumn> timeColumns,
            List<ColumnInfo> columns) {
    }

    /** 시각 컬럼의 UTC 보정값. DB 가 UTC 로 저장할 때 현지 시차(예: KST = 9)를 적는다. 0 은 보정 없음. */
    public record UtcOffsetInput(String column, Integer utcOffsetHours) {

        public Integer hours() {
            if (utcOffsetHours == null || utcOffsetHours == 0) {
                return null;
            }
            if (utcOffsetHours < -12 || utcOffsetHours > 14) {
                throw invalid("UTC 보정값은 -12 ~ +14 시간입니다");
            }
            return utcOffsetHours;
        }
    }

    /** 컬럼의 '계산 필요' 표시 (예: 누적값 — 그대로 집계하면 틀린다) */
    public record NeedsCalcInput(String column, boolean needsCalc) {
    }

    /** 사람이 적는 컬럼 설명. 비우면 대상 DB 코멘트나 추론한 설명으로 돌아간다. */
    public record ColumnNoteInput(String column, String note) {

        public String cleaned() {
            String text = note == null ? "" : note;
            if (text.length() > 2000) {
                throw invalid("note: String should have at most 2000 characters");
            }
            return text.strip();
        }
    }

    /** 사람이 적는 테이블 용도. 테이블 추론에 쓰인다. */
    public record TablePurposeInput(String purpose) {
    }

    /** 이름 목록 정리: 앞뒤 공백을 떼고, 빈 것과 대소문자만 다른 중복을 뺀다 */
    public static List<String> cleanNames(List<String> values) {
        Set<String> seen = new HashSet<>();
        List<String> out = new ArrayList<>();
        if (values == null) {
            return out;
        }
        for (String v : values) {
            String text = v == null ? "" : v.strip();
            if (!text.isEmpty() && seen.add(text.toLowerCase())) {
                out.add(text);
            }
        }
        return out;
    }

    public static ApiException invalid(String message) {
        return new ApiException(422, message);
    }
}
