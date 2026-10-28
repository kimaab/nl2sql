package com.nl2sql.llm;

import java.util.ArrayList;
import java.util.List;

/** 질문 처리 단계 하나 (ask_trace 한 행). LLM 을 부를 때마다 토큰 · 시간을 더한다. */
public class Step {

    public final String stage;  // table | sql | draft | 지표 자동 등록 | 지표 검증
    public List<String> candidates = new ArrayList<>();
    public List<String> selected = new ArrayList<>();
    public String reason = "";
    public int promptTokens;
    public int completionTokens;
    public int elapsedMs;
    public final String model;

    public Step(String stage, String model) {
        this.stage = stage;
        this.model = model == null ? "" : model;
    }

    public Step(String stage, List<String> candidates, String model) {
        this(stage, model);
        this.candidates = candidates;
    }
}
