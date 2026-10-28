package com.nl2sql.web;

import java.util.List;

import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import com.nl2sql.llm.LlmFactory;
import com.nl2sql.model.RunModels.AutoRun;
import com.nl2sql.model.RunModels.AutoRunDetail;
import com.nl2sql.model.RunModels.AutoRunRequest;
import com.nl2sql.model.RunModels.DescribeRequest;
import com.nl2sql.model.RunModels.DescribeRun;
import com.nl2sql.model.RunModels.ValidationItem;
import com.nl2sql.model.RunModels.ValidationRequest;
import com.nl2sql.model.RunModels.ValidationRun;
import com.nl2sql.model.RunModels.ValidationRunDetail;
import com.nl2sql.service.DescribeService;
import com.nl2sql.service.MetricAutoService;
import com.nl2sql.service.MetricValidateService;
import com.nl2sql.service.SystemService;

/**
 * 오래 걸리는 실행: 지표 자동 등록 · 지표 검증 · 설명 자동 채우기 (metric_auto_routes.py, describe_routes.py).
 * 스레드에서 돌고, 화면은 실행 기록을 다시 읽어 진행을 본다.
 */
@RestController
@RequestMapping("/api/systems/{systemId}")
public class RunController {

    private final SystemService systems;
    private final MetricAutoService auto;
    private final MetricValidateService validate;
    private final DescribeService describe;
    private final LlmFactory llm;

    public RunController(SystemService systems, MetricAutoService auto, MetricValidateService validate, DescribeService describe,
            LlmFactory llm) {
        this.systems = systems;
        this.auto = auto;
        this.validate = validate;
        this.describe = describe;
        this.llm = llm;
    }

    // ── 지표 자동 등록 ──────────────────────────────────────────────────

    @PostMapping("/metric-auto/runs")
    @ResponseStatus(HttpStatus.ACCEPTED)
    public AutoRun postAutoRun(@PathVariable String systemId, @RequestBody AutoRunRequest req) {
        systems.getSystem(Params.uuid(systemId));
        return auto.startRun(systemId, req, llm.make());
    }

    @GetMapping("/metric-auto/runs")
    public List<AutoRun> getAutoRuns(@PathVariable String systemId, @RequestParam(defaultValue = "50") int limit) {
        systems.getSystem(Params.uuid(systemId));
        return auto.listRuns(systemId, Params.range("limit", limit, 1, 200));
    }

    @GetMapping("/metric-auto/runs/{runId}")
    public AutoRunDetail getAutoRun(@PathVariable String systemId, @PathVariable String runId) {
        return auto.getRun(Params.uuid(systemId), Params.uuid(runId));
    }

    // ── 지표 검증 ───────────────────────────────────────────────────────

    @PostMapping("/metric-validations")
    @ResponseStatus(HttpStatus.ACCEPTED)
    public ValidationRun postValidation(@PathVariable String systemId, @RequestBody ValidationRequest req) {
        systems.getSystem(Params.uuid(systemId));
        return validate.startValidation(systemId, req, req.useLlm() ? llm.make() : null);
    }

    @GetMapping("/metric-validations")
    public List<ValidationRun> getValidations(@PathVariable String systemId, @RequestParam(defaultValue = "50") int limit) {
        systems.getSystem(Params.uuid(systemId));
        return validate.listValidations(systemId, Params.range("limit", limit, 1, 200));
    }

    @GetMapping("/metric-validations/{runId}")
    public ValidationRunDetail getValidationDetail(@PathVariable String systemId, @PathVariable String runId) {
        return validate.getValidation(Params.uuid(systemId), Params.uuid(runId));
    }

    @GetMapping("/metrics/{metricId}/validations")
    public List<ValidationItem> getMetricValidations(@PathVariable String systemId, @PathVariable String metricId,
            @RequestParam(defaultValue = "20") int limit) {
        systems.getSystem(Params.uuid(systemId));
        return validate.metricValidations(systemId, Params.uuid(metricId), Params.range("limit", limit, 1, 100));
    }

    // ── 설명 자동 채우기 · 관계 추론 ──────────────────────────────────────

    @PostMapping("/describe/runs")
    @ResponseStatus(HttpStatus.ACCEPTED)
    public DescribeRun postDescribeRun(@PathVariable String systemId, @RequestBody DescribeRequest req) {
        systems.getSystem(Params.uuid(systemId));
        return describe.startDescribe(systemId, req, req.descriptions() ? llm.make() : null);
    }

    @GetMapping("/describe/runs")
    public List<DescribeRun> getDescribeRuns(@PathVariable String systemId, @RequestParam(defaultValue = "50") int limit) {
        systems.getSystem(Params.uuid(systemId));
        return describe.listDescribeRuns(systemId, Params.range("limit", limit, 1, 200));
    }

    @GetMapping("/describe/runs/{runId}")
    public DescribeRun getDescribeRunDetail(@PathVariable String systemId, @PathVariable String runId) {
        return describe.getDescribeRun(Params.uuid(systemId), Params.uuid(runId));
    }
}
