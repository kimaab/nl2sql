package com.nl2sql.web;

import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.web.bind.MissingServletRequestParameterException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.method.annotation.MethodArgumentTypeMismatchException;

import com.nl2sql.common.ApiException;

/** 오류를 FastAPI 와 같은 모양 {"detail": 메시지} 로 돌려준다. 입력 검증 실패는 422. */
@RestControllerAdvice
public class ApiExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger("nl2sql");

    /** 입력 검증 실패(422)는 FastAPI 처럼 [{type, loc, msg}] 목록으로, 그 밖은 글로 */
    @ExceptionHandler(ApiException.class)
    public ResponseEntity<Map<String, Object>> api(ApiException error) {
        if (error.status() == 422) {
            return invalid(error.detail());
        }
        return ResponseEntity.status(error.status()).body(Map.of("detail", error.detail()));
    }

    private static ResponseEntity<Map<String, Object>> invalid(String message) {
        Map<String, Object> item = new java.util.LinkedHashMap<>();
        item.put("type", "value_error");
        item.put("loc", java.util.List.of());
        item.put("msg", "Value error, " + message);
        return ResponseEntity.status(422).body(Map.of("detail", java.util.List.of(item)));
    }

    /** 본문 JSON 을 읽지 못했다. 레코드 생성자의 검증(ApiException)이면 그 상태와 메시지를 쓴다. */
    @ExceptionHandler(HttpMessageNotReadableException.class)
    public ResponseEntity<Map<String, Object>> unreadable(HttpMessageNotReadableException error) {
        for (Throwable cause = error; cause != null; cause = cause.getCause()) {
            if (cause instanceof ApiException api) {
                return api(api);
            }
        }
        String message = error.getMostSpecificCause().getMessage();
        return invalid("요청 본문을 읽지 못했습니다: " + firstLine(message));
    }

    @ExceptionHandler({MethodArgumentTypeMismatchException.class, MissingServletRequestParameterException.class})
    public ResponseEntity<Map<String, Object>> badParameter(Exception error) {
        return invalid(firstLine(error.getMessage()));
    }

    /** 설정 오류 (키 · 접속 정보 등). 파이썬은 500 으로 끝난다 — 이유는 로그와 응답에 남긴다. */
    @ExceptionHandler(IllegalStateException.class)
    public ResponseEntity<Map<String, Object>> config(IllegalStateException error) {
        log.error("요청 처리 실패", error);
        return ResponseEntity.status(500).body(Map.of("detail", firstLine(error.getMessage())));
    }

    private static String firstLine(String text) {
        if (text == null) {
            return "";
        }
        String[] lines = text.strip().split("\\R");
        return lines.length == 0 ? "" : lines[0];
    }
}
