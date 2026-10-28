package com.nl2sql.service;

import java.time.OffsetDateTime;
import java.util.Map;

import com.nl2sql.common.Times;

/** 실행 기록 행을 읽는 도구 (자동 등록 · 검증 · 설명 채우기 공통) */
final class RunRecords {

    private RunRecords() {
    }

    /** running 인데 이 프로세스에서 돌고 있지 않으면 서버가 재시작돼 멈춘 실행이다 */
    static String status(Map<String, Object> r, Background background) {
        String status = (String) r.get("status");
        if ("running".equals(status) && !background.isActive(r.get("id"))) {
            return "interrupted";
        }
        return status;
    }

    static String started(Map<String, Object> r) {
        return Times.iso((OffsetDateTime) r.get("started_at"));
    }

    static String finished(Map<String, Object> r) {
        return r.get("finished_at") == null ? null : Times.iso((OffsetDateTime) r.get("finished_at"));
    }

    static int integer(Map<String, Object> r, String key) {
        Object value = r.get(key);
        return value == null ? 0 : ((Number) value).intValue();
    }
}
