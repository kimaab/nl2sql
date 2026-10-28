package com.nl2sql.web;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import com.nl2sql.common.ApiException;
import com.nl2sql.common.Dyn;
import com.nl2sql.common.Py;
import com.nl2sql.llm.LlmFactory;
import com.nl2sql.model.MetricModels.AskRequest;
import com.nl2sql.model.MetricModels.AskResponse;
import com.nl2sql.model.MetricModels.AskStep;
import com.nl2sql.model.MetricModels.HistoryEntry;
import com.nl2sql.model.MetricModels.HistoryPatch;
import com.nl2sql.model.MetricModels.SqlLabLink;
import com.nl2sql.model.MetricModels.SupersetChart;
import com.nl2sql.service.AskService;
import com.nl2sql.service.ContractService;
import com.nl2sql.service.HistoryService;
import com.nl2sql.service.SupersetService;

/** 질문 · 질문 기록 · 기록의 SQL 을 Superset 으로 */
@RestController
public class AskController {

    private static final Logger logRequest = LoggerFactory.getLogger("nl2sql.request");

    private final ContractService contracts;
    private final AskService ask;
    private final HistoryService history;
    private final SupersetService superset;
    private final LlmFactory llm;

    public AskController(ContractService contracts, AskService ask, HistoryService history, SupersetService superset,
            LlmFactory llm) {
        this.contracts = contracts;
        this.ask = ask;
        this.history = history;
        this.superset = superset;
        this.llm = llm;
    }

    /** 질문에서 SQL 생성: 테이블 선택(지표 필요 판단) → 조회 명세 → 컴파일 */
    @PostMapping("/api/ask")
    public AskResponse postAsk(@RequestBody AskRequest req) {
        String systemId = Params.uuid(req.systemId() == null ? "" : req.systemId());
        String question = req.question() == null ? "" : req.question();
        long started = System.nanoTime();
        Map<String, Object> contract = contracts.loadContract(systemId, true);

        Map<String, Object> outcome = ask.runQuestion(contract, question, llm.make(), req.userId() == null ? "" : req.userId());
        double elapsedMs = (System.nanoTime() - started) / 1_000_000.0;

        String result = outcome.get("sql") != null ? "success"
                : outcome.get("clarification") != null ? "clarify"
                : Py.truthy(outcome.get("needs_metric")) ? "needs_metric" : "fail";
        logRequest.info("system={} question='{}' tables={} metrics={} attempts={} tokens={} outcome={} elapsed_ms={}{}",
                contract.get("code"), question, outcome.get("tables"), outcome.get("metrics"), outcome.get("attempts"),
                outcome.get("total_tokens"), result, Math.round(elapsedMs),
                result.equals("fail") || result.equals("needs_metric") ? " detail=" + outcome.get("error") : "");
        String entryId = history.recordAsk(systemId, question, outcome, elapsedMs);
        List<AskStep> steps = new ArrayList<>();
        for (Map<String, Object> s : Dyn.maps(outcome.get("steps"))) {
            steps.add(new AskStep((String) s.get("stage"), Dyn.items(s.get("candidates")), Dyn.items(s.get("selected")),
                    (String) s.get("reason"), Dyn.integer(s, "prompt_tokens"), Dyn.integer(s, "completion_tokens"),
                    Dyn.integer(s, "elapsed_ms")));
        }
        return new AskResponse(entryId, (String) outcome.get("sql"), (String) outcome.get("error"), Dyn.integer(outcome, "attempts"),
                (String) outcome.get("clarification"), Dyn.strings(outcome.get("tables")), Dyn.strings(outcome.get("metrics")),
                Py.truthy(outcome.get("needs_metric")), Dyn.maps(outcome.get("missing")), steps);
    }

    // ── 질문 기록 ──────────────────────────────────────────────────────

    @GetMapping("/api/history")
    public List<HistoryEntry> getHistoryList(@RequestParam(name = "system_id", required = false) String systemId,
            @RequestParam(required = false) Boolean favorite, @RequestParam(required = false) String feedback,
            @RequestParam(defaultValue = "50") int limit, @RequestParam(defaultValue = "0") int offset) {
        if (feedback != null && !feedback.equals("up") && !feedback.equals("down")) {
            throw new ApiException(422, "feedback 은 up 이나 down 입니다");
        }
        if (offset < 0) {
            throw new ApiException(422, "offset 은 0 이상입니다");
        }
        return history.listHistory(Params.optionalUuid(systemId), favorite, feedback, Params.range("limit", limit, 1, 500), offset);
    }

    @GetMapping("/api/history/export")
    public Map<String, Object> getHistoryExport(@RequestParam(name = "system_id") String systemId) {
        return history.exportEvalCases(Params.uuid(systemId));
    }

    @GetMapping("/api/history/{entryId}/trace")
    public List<AskStep> getHistoryTrace(@PathVariable String entryId) {
        return history.getTrace(Params.uuid(entryId));
    }

    @PatchMapping("/api/history/{entryId}")
    public HistoryEntry patchHistoryEntry(@PathVariable String entryId, @RequestBody HistoryPatch req) {
        return history.patchHistory(Params.uuid(entryId), req);
    }

    @DeleteMapping("/api/history/{entryId}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void deleteHistoryEntry(@PathVariable String entryId) {
        history.deleteHistory(Params.uuid(entryId));
    }

    // ── Superset ──────────────────────────────────────────────────────
    // Superset 으로 보내는 SQL 은 화면에서 받지 않는다 — 질문 기록에 남은 생성 SQL 만 쓴다.
    // 화면이 보낸 SQL 을 받으면 API 를 직접 불러 아무 SQL 이나 대상 DB 에 실행시킬 수 있다.

    private HistoryEntry generatedSql(String entryId) {
        HistoryEntry entry = history.getHistory(Params.uuid(entryId));
        if (!Py.truthy(entry.sql())) {
            throw new ApiException(400, "SQL 을 만들지 못한 질문입니다");
        }
        return entry;
    }

    /** 생성한 SQL 을 Superset SQL Lab 에 열어 두는 링크. 실행은 SQL Lab 에서 사람이 한다. */
    @PostMapping("/api/history/{entryId}/superset/sqllab")
    public SqlLabLink postSupersetSqllab(@PathVariable String entryId) {
        HistoryEntry entry = generatedSql(entryId);
        return new SqlLabLink(superset.sqllabLink(entry.systemId(), entry.sql(), entry.question()));
    }

    /** 생성한 SQL 로 Superset 차트를 만든다. 결과 컬럼 모양으로 차트 종류를 정하고, 같은 SQL 이면 전에 만든 차트를 연다. */
    @PostMapping("/api/history/{entryId}/superset/chart")
    public SupersetChart postSupersetChart(@PathVariable String entryId) {
        HistoryEntry entry = generatedSql(entryId);
        Map<String, Object> chart = superset.createChart(entry.systemId(), entry.sql(), entry.question());
        return new SupersetChart((String) chart.get("url"), (String) chart.get("viz_type"),
                ((Number) chart.get("dataset_id")).longValue(), ((Number) chart.get("chart_id")).longValue());
    }

    @GetMapping("/health")
    public Map<String, Object> healthCheck() {
        return Map.of("status", "ok");
    }
}
