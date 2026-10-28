package com.nl2sql.web;

import java.util.List;
import java.util.Map;

import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import com.nl2sql.compiler.Compiler;
import com.nl2sql.llm.LlmFactory;
import com.nl2sql.model.MetricModels.CompileRequest;
import com.nl2sql.model.MetricModels.CompileResponse;
import com.nl2sql.model.MetricModels.Metric;
import com.nl2sql.model.MetricModels.MetricCheck;
import com.nl2sql.model.MetricModels.MetricCheckRequest;
import com.nl2sql.model.MetricModels.MetricDraft;
import com.nl2sql.model.MetricModels.MetricDraftRequest;
import com.nl2sql.model.MetricModels.MetricHistoryEntry;
import com.nl2sql.model.MetricModels.MetricInput;
import com.nl2sql.service.Connections;
import com.nl2sql.service.ContractService;
import com.nl2sql.service.MetricDraftService;
import com.nl2sql.service.MetricService;
import com.nl2sql.service.SystemService;

/** 지표 · 컴파일 */
@RestController
public class MetricController {

    private final SystemService systems;
    private final MetricService metrics;
    private final MetricDraftService drafts;
    private final ContractService contracts;
    private final Connections connections;
    private final LlmFactory llm;

    public MetricController(SystemService systems, MetricService metrics, MetricDraftService drafts, ContractService contracts,
            Connections connections, LlmFactory llm) {
        this.systems = systems;
        this.metrics = metrics;
        this.drafts = drafts;
        this.contracts = contracts;
        this.connections = connections;
        this.llm = llm;
    }

    @GetMapping("/api/systems/{systemId}/metrics")
    public List<Metric> getMetrics(@PathVariable String systemId) {
        systems.getSystem(Params.uuid(systemId));
        return metrics.listMetrics(systemId);
    }

    @PostMapping("/api/systems/{systemId}/metrics")
    public Metric postMetric(@PathVariable String systemId, @RequestBody MetricInput req) {
        systems.getSystem(Params.uuid(systemId));
        return metrics.createMetric(systemId, req.validated());
    }

    @PutMapping("/api/systems/{systemId}/metrics/{metricId}")
    public Metric putMetric(@PathVariable String systemId, @PathVariable String metricId, @RequestBody MetricInput req) {
        systems.getSystem(Params.uuid(systemId));
        return metrics.updateMetric(systemId, Params.uuid(metricId), req.validated());
    }

    /** 저장하지 않고 등록 절차만 밟아 본다: SQL → AST → SQL → 대상 DB 에서 결과 비교 */
    @PostMapping("/api/systems/{systemId}/metrics/check")
    public MetricCheck postMetricCheck(@PathVariable String systemId, @RequestBody MetricCheckRequest req) {
        systems.getSystem(Params.uuid(systemId));
        return metrics.checkMetric(systemId, req.sql() == null ? "" : req.sql());
    }

    /** 지표가 없어 멈춘 질문에서 지표 SQL 초안을 쓴다 (저장하지 않는다). 값 샘플은 인덱스를 타는 조회로만 읽는다. */
    @PostMapping("/api/systems/{systemId}/metrics/draft")
    public MetricDraft postMetricDraft(@PathVariable String systemId, @RequestBody MetricDraftRequest req) {
        String id = Params.uuid(systemId);
        return drafts.draftMetric(id, req, llm.make(), connections.row(id));
    }

    @GetMapping("/api/systems/{systemId}/metrics/{metricId}/history")
    public List<MetricHistoryEntry> getMetricHistory(@PathVariable String systemId, @PathVariable String metricId) {
        return metrics.listMetricHistory(Params.uuid(systemId), Params.uuid(metricId));
    }

    @DeleteMapping("/api/systems/{systemId}/metrics/{metricId}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void deleteMetricDetail(@PathVariable String systemId, @PathVariable String metricId) {
        metrics.deleteMetric(Params.uuid(systemId), Params.uuid(metricId));
    }

    /** 모델 없이 AST 를 컴파일해 본다 */
    @PostMapping("/api/systems/{systemId}/compile")
    public CompileResponse postCompile(@PathVariable String systemId, @RequestBody CompileRequest req) {
        Map<String, Object> contract = contracts.loadContract(Params.uuid(systemId));
        try {
            return new CompileResponse(new Compiler(contract).compile(req.ast()), null);
        } catch (IllegalArgumentException error) {
            return new CompileResponse(null, error.getMessage());
        }
    }
}
