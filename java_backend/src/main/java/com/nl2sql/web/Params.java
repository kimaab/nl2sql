package com.nl2sql.web;

import com.nl2sql.common.ApiException;
import com.nl2sql.service.SystemService;

/** 쿼리 · 경로 파라미터 검증 (FastAPI 의 Query(ge, le) · UUID 와 같은 규칙, 틀리면 422) */
final class Params {

    private Params() {
    }

    static int range(String name, int value, int min, int max) {
        if (value < min || value > max) {
            throw new ApiException(422, name + " 는 " + min + " ~ " + max + " 입니다");
        }
        return value;
    }

    static String uuid(String value) {
        return SystemService.uuid(value);
    }

    static String optionalUuid(String value) {
        return value == null || value.isEmpty() ? null : SystemService.uuid(value);
    }
}
