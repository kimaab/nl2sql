package com.nl2sql.model;

import java.util.List;
import java.util.Map;

import com.nl2sql.model.MetricModels.MetricCheck;

/** 오래 걸리는 실행: 지표 자동 등록 · 지표 검증 · 설명 자동 채우기 (models.py) */
public final class RunModels {

    private RunModels() {
    }

    /**
     * 지표 자동 등록. tables 를 비우면 지표가 없는 테이블 중 관계(FK)가 많은 것부터 max_tables 개를 고른다.
     * allow_full_scan: 실행 계획이 큰 테이블 전체를 훑어도 결과 비교를 돌린다 (테스트 DB 용 — 운영 DB 에는 끈다)
     */
    public record AutoRunRequest(List<String> tables, Integer maxTables, Integer perTable, Boolean allowFullScan) {

        public AutoRunRequest {
            tables = SystemModels.cleanNames(tables);
            maxTables = maxTables == null ? 5 : maxTables;
            perTable = perTable == null ? 3 : perTable;
            allowFullScan = allowFullScan != null && allowFullScan;
            if (maxTables < 1 || maxTables > 50) {
                throw SystemModels.invalid("테이블은 한 번에 1 ~ 50개입니다");
            }
            if (perTable < 1 || perTable > 10) {
                throw SystemModels.invalid("테이블마다 지표 후보는 1 ~ 10개입니다");
            }
        }
    }

    /** 후보 하나의 시도 한 번 */
    public record AutoTry(String sql, String stage, String error) {
    }

    /** 자동 등록 후보 하나. status: registered = 지표로 저장, failed = 확인 실패, skipped = 이름 중복 등으로 건너뜀 */
    public record AutoItem(
            long id,
            String tableName,
            String name,
            String description,
            List<Object> synonyms,
            String sql,
            String rationale,
            String status,
            String stage,
            String reason,
            int attempts,
            List<AutoTry> tries,
            MetricCheck check,
            List<Object> fullScans,
            String metricId,
            String createdAt) {
    }

    public record AutoRun(
            String id,
            String systemId,
            String status,
            Map<String, Object> options,
            List<Object> tables,
            int tablesDone,
            int proposed,
            int registered,
            int failed,
            int skipped,
            int promptTokens,
            int completionTokens,
            String error,
            String startedAt,
            String finishedAt) {
    }

    /** AutoRun 과 같은 필드 + items */
    public record AutoRunDetail(
            String id,
            String systemId,
            String status,
            Map<String, Object> options,
            List<Object> tables,
            int tablesDone,
            int proposed,
            int registered,
            int failed,
            int skipped,
            int promptTokens,
            int completionTokens,
            String error,
            String startedAt,
            String finishedAt,
            List<AutoItem> items) {

        public static AutoRunDetail of(AutoRun r, List<AutoItem> items) {
            return new AutoRunDetail(r.id(), r.systemId(), r.status(), r.options(), r.tables(), r.tablesDone(),
                    r.proposed(), r.registered(), r.failed(), r.skipped(), r.promptTokens(), r.completionTokens(),
                    r.error(), r.startedAt(), r.finishedAt(), items);
        }
    }

    /**
     * 등록된 지표 검증. metric_ids 에 고른 지표만 검증한다 (비우면 돌리지 않는다).
     * run_sql: 대상 DB 에서 결과 비교 · 결과 점검 · 실행 계획을 본다. use_llm: LLM 이 SQL 이 이름 · 설명과 맞는지 검토한다.
     * allow_full_scan: 실행 계획이 큰 테이블 전체를 훑어도 결과 비교 · 결과 점검을 돌린다 (테스트 DB 용)
     */
    public record ValidationRequest(List<String> metricIds, Boolean runSql, Boolean useLlm, Boolean allowFullScan) {

        public ValidationRequest {
            metricIds = metricIds == null ? List.of() : metricIds;
            runSql = runSql == null || runSql;
            useLlm = useLlm == null || useLlm;
            allowFullScan = allowFullScan != null && allowFullScan;
        }
    }

    /** 점검 하나. status: pass | warn | fail | skip (앞 단계가 실패했거나 옵션으로 끈 것) */
    public record ValidationCheck(String key, String label, String status, String message, Map<String, Object> detail) {
    }

    public record ValidationItem(
            long id,
            String runId,
            String metricId,
            String metricName,
            int metricVersion,
            String verdict,
            String summary,
            List<ValidationCheck> checks,
            int elapsedMs,
            String createdAt) {
    }

    public record ValidationRun(
            String id,
            String systemId,
            String status,
            Map<String, Object> options,
            int total,
            int done,
            int passed,
            int warned,
            int failed,
            int promptTokens,
            int completionTokens,
            String error,
            String startedAt,
            String finishedAt) {
    }

    public record ValidationRunDetail(
            String id,
            String systemId,
            String status,
            Map<String, Object> options,
            int total,
            int done,
            int passed,
            int warned,
            int failed,
            int promptTokens,
            int completionTokens,
            String error,
            String startedAt,
            String finishedAt,
            List<ValidationItem> items) {

        public static ValidationRunDetail of(ValidationRun r, List<ValidationItem> items) {
            return new ValidationRunDetail(r.id(), r.systemId(), r.status(), r.options(), r.total(), r.done(),
                    r.passed(), r.warned(), r.failed(), r.promptTokens(), r.completionTokens(), r.error(),
                    r.startedAt(), r.finishedAt(), items);
        }
    }

    /** 테이블 · 컬럼 설명 자동 채우기와 관계 추론 (DescribeService) */
    public record DescribeRequest(List<String> tableIds, Boolean descriptions, Boolean overwrite, Boolean relations) {

        public DescribeRequest {
            tableIds = tableIds == null ? List.of() : tableIds;    // 비우면 설명이 빈 테이블 모두
            descriptions = descriptions == null || descriptions;  // 비어 있는 설명을 LLM 으로 추론한다
            overwrite = overwrite != null && overwrite;           // 전에 추론한 설명도 다시 추론한다
            relations = relations == null || relations;           // 컬럼 이름 · 타입 · PK 로 관계를 추론한다
        }
    }

    public record DescribeRun(
            String id,
            String systemId,
            String status,
            Map<String, Object> options,
            int tablesTotal,
            int tablesDone,
            int tablesFilled,
            int columnsFilled,
            Integer relationsAdded,
            List<Object> failures,
            String error,
            String startedAt,
            String finishedAt) {
    }
}
