package com.nl2sql.model;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/** 지표 · 관계 · 질문 · 기록 (models.py) */
public final class MetricModels {

    private MetricModels() {
    }

    /** 업무 지표 = 이름 · 설명 · 같은 말 + SQL. SQL 은 AST 로 옮겨 저장한다. */
    public record MetricInput(String name, String description, List<String> synonyms, String sql) {

        public MetricInput validated() {
            String cleanName = name == null ? "" : name.strip();
            String cleanSql = sql == null ? "" : sql.strip();
            if (cleanName.isEmpty()) {
                throw SystemModels.invalid("name: must not be blank");
            }
            if (cleanSql.isEmpty()) {
                throw SystemModels.invalid("sql: must not be blank");
            }
            return new MetricInput(cleanName, description == null ? "" : description,
                    SystemModels.cleanNames(synonyms), cleanSql);
        }
    }

    /** 업무 지표. compiled_sql 은 저장된 AST 를 지금 스키마로 컴파일한 것 (깨졌으면 null). */
    public record Metric(
            String id,
            String name,
            String description,
            List<Object> synonyms,
            String sql,
            Object ast,
            String compiledSql,
            List<Object> tables,
            String status,
            String brokenReason,
            int version,
            String updatedAt) {
    }

    public record MetricCheckRequest(String sql) {
    }

    /** 등록 전 확인 결과. stage: 실패한 단계 (parse = AST 변환, compile = 컴파일, guard = 실행 전 확인, verify = 결과 비교) */
    public static final class MetricCheck {
        public Map<String, Object> ast;
        public String sql;
        public List<String> tables = new ArrayList<>();
        public List<String> warnings = new ArrayList<>();
        public Integer rows;
        public String stage;
        public String error;

        public static MetricCheck failed(String stage, String error) {
            MetricCheck out = new MetricCheck();
            out.stage = stage;
            out.error = error;
            return out;
        }
    }

    /** 지표가 없는 계산 하나. 테이블 선택 단계가 적는다 — 지표 초안의 출발점이다. */
    public record MissingMetric(String name, String table, String column) {

        public MissingMetric {
            column = column == null ? "" : column;
        }
    }

    public record MetricDraftRequest(String question, MissingMetric missing) {
    }

    /**
     * 대상 컬럼의 실제 값 샘플. 인덱스를 타는 조회로만 뽑는다 — 쓸 인덱스가 없으면 rows 가 비고 note 에 이유가 있다.
     * queries: 실제로 실행한 조회, latest: 데이터의 가장 최근 시각 (초안의 예시 기간 기준)
     */
    public static final class ValueSample {
        public String index;
        public List<String> queries = new ArrayList<>();
        public List<String> columns = new ArrayList<>();
        public List<List<String>> rows = new ArrayList<>();
        public String latest;
        public String note = "";

        public static ValueSample note(String note) {
            ValueSample out = new ValueSample();
            out.note = note;
            return out;
        }
    }

    /**
     * LLM 이 쓴 지표 초안. 저장하지 않는다 — 사람이 고치고 확인한 뒤 지표 등록으로 저장한다.
     * unsure: 계산 방식을 정하지 못한 이유 (그때 sql 은 비어 있다). check: 초안 SQL 의 등록 확인 결과
     * full_scans: 초안 SQL 의 실행 계획이 전체를 훑는 큰 테이블 (인덱스를 타지 못한다)
     */
    public static final class MetricDraft {
        public String name = "";
        public String description = "";
        public List<String> synonyms = new ArrayList<>();
        public String sql = "";
        public List<String> assumptions = new ArrayList<>();
        public String unsure;
        public int attempts;
        public ValueSample sample;
        public MetricCheck check;
        public List<String> fullScans = new ArrayList<>();
    }

    /** 지표 정의의 한 시점 */
    public record MetricHistoryEntry(int version, String action, Object snapshot, String createdAt) {
    }

    /** 테이블 관계. left 가 참조하는 쪽(FK), right 가 참조되는 쪽이다. */
    public record RelationInput(String leftTable, String leftColumn, String rightTable, String rightColumn,
            String constraintName) {

        public RelationInput validated() {
            return new RelationInput(required("left_table", leftTable), required("left_column", leftColumn),
                    required("right_table", rightTable), required("right_column", rightColumn),
                    constraintName == null ? "" : constraintName);
        }

        private static String required(String field, String value) {
            String cleaned = value == null ? "" : value.strip();
            if (cleaned.isEmpty()) {
                throw SystemModels.invalid(field + ": must not be blank");
            }
            return cleaned;
        }
    }

    /** source: fk | manual | auto (auto: 컬럼 이름 · 타입 · PK 로 추론) */
    public record Relation(String leftTable, String leftColumn, String rightTable, String rightColumn,
            String constraintName, String id, String source) {
    }

    /** 질문 요청. user_id: 질문한 사용자 (로그인 아이디). 질문의 '나', '내 권한' 이 이 사용자다 */
    public record AskRequest(String systemId, String question, String userId) {
    }

    /** 질문 처리의 한 단계 (ask_trace 한 행). 화면이 '왜 이 테이블·지표인가' 를 보여준다. */
    public record AskStep(String stage, List<Object> candidates, List<Object> selected, String reason,
            int promptTokens, int completionTokens, int elapsedMs) {
    }

    /**
     * 질문 응답. clarification 이 있으면 모델이 되물은 것이다 — sql 은 없다.
     * needs_metric 이면 지표가 필요한 질문인데 맞는 지표가 없다는 뜻이다 — error 에 이유가, missing 에 없는 계산이 있다.
     */
    public record AskResponse(String id, String sql, String error, int attempts, String clarification,
            List<String> selectedTables, List<String> selectedMetrics, boolean needsMetric,
            List<Map<String, Object>> missing, List<AskStep> steps) {
    }

    public record SqlLabLink(String url) {
    }

    /** Superset 에 만든 차트. url 은 차트 편집 화면(Explore), viz_type 은 고른 차트 종류. */
    public record SupersetChart(String url, String vizType, long datasetId, long chartId) {
    }

    public record CompileRequest(Map<String, Object> ast) {
    }

    public record CompileResponse(String sql, String error) {
    }

    public record HistoryEntry(
            String id,
            String systemId,
            String systemName,
            String question,
            List<Object> selectedTables,
            List<Object> selectedMetrics,
            int totalTokens,
            String sql,
            Object ast,
            String error,
            String clarification,
            int attempts,
            int elapsedMs,
            boolean favorite,
            String feedback,
            String feedbackNote,
            String createdAt) {
    }

    /** 바꿀 것만 보낸다. feedback 을 지우려면 빈 문자열을 보낸다. */
    public record HistoryPatch(Boolean favorite, String feedback, String feedbackNote) {

        public HistoryPatch {
            if (feedback != null && !List.of("up", "down", "").contains(feedback)) {
                throw SystemModels.invalid("feedback 은 up, down, 빈 문자열 중 하나입니다");
            }
        }
    }
}
