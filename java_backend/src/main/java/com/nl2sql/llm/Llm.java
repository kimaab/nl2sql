package com.nl2sql.llm;

import java.util.List;

/**
 * OpenAI 호환 LLM 하나 (파이썬의 ChatOpenAI 자리). 구현은 Spring AI 와 LangChain4j 둘이고 LLM_PROVIDER 로 고른다.
 * 둘 다 같은 대화(시스템 · 사용자 · 응답 메시지 목록)를 받아 응답 글과 토큰 수를 돌려준다.
 */
public interface Llm {

    /** 대화 메시지 하나. role: system | user | assistant */
    record Message(String role, String content) {

        public static Message system(String content) {
            return new Message("system", content);
        }

        public static Message user(String content) {
            return new Message("user", content);
        }

        public static Message assistant(String content) {
            return new Message("assistant", content);
        }
    }

    /** 응답 글과 토큰 수 */
    record Reply(String content, int promptTokens, int completionTokens) {
    }

    Reply invoke(List<Message> messages);

    String modelName();
}
