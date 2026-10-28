package com.nl2sql.common;

/** 서비스 계층의 오류. 처리기가 {"detail": 메시지} 와 상태 코드로 돌려준다. */
public class ApiException extends RuntimeException {

    private final int status;
    private final String detail;

    public ApiException(int status, String detail) {
        super(detail);
        this.status = status;
        this.detail = detail;
    }

    public int status() {
        return status;
    }

    public String detail() {
        return detail;
    }
}
