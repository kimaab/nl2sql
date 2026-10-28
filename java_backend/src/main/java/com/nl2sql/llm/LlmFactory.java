package com.nl2sql.llm;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import com.nl2sql.common.Env;

/**
 * OpenAI 호환 LLM (LLM_BASE_URL · LLM_API_KEY · LLM_MODEL). 파이썬 llm.make_model 자리.
 *
 * LLM_PROVIDER 로 구현을 고른다: spring-ai (기본) | langchain4j. 둘 다 같은 엔드포인트를 같은 방식으로 부른다.
 */
@Component
public class LlmFactory {

    private static final Logger log = LoggerFactory.getLogger("nl2sql.llm");

    /** 응답 하나를 기다리는 시간과 재시도 수 — 파이썬 ChatOpenAI(openai SDK) 기본값과 같다. 생각이 긴 모델은 몇 분 걸린다. */
    static final java.time.Duration TIMEOUT = java.time.Duration.ofMinutes(10);
    static final int MAX_RETRIES = 2;

    private final Env env;

    public LlmFactory(Env env) {
        this.env = env;
    }

    public Llm make() {
        return make(0.0);
    }

    public Llm make(double temperature) {
        String baseUrl = env.get("LLM_BASE_URL");
        String apiKey = env.get("LLM_API_KEY", "not-needed");
        String model = env.get("LLM_MODEL", "gpt-3.5-turbo");
        String provider = env.get("LLM_PROVIDER", "spring-ai").toLowerCase();
        log.info("LLM url: {} 모델: {} ({})", baseUrl, model, provider);
        if (provider.equals("langchain4j")) {
            return new LangChain4jLlm(baseUrl, apiKey.isEmpty() ? "not-needed" : apiKey, model, temperature);
        }
        return new SpringAiLlm(baseUrl, apiKey.isEmpty() ? "not-needed" : apiKey, model, temperature);
    }
}
