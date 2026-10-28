package com.nl2sql;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/** nl2sql Studio 백엔드. backend/ 의 파이썬(FastAPI) API 를 같은 경로 · 같은 응답 모양으로 옮긴 것이다. */
@SpringBootApplication
public class Nl2sqlApplication {

    public static void main(String[] args) {
        SpringApplication.run(Nl2sqlApplication.class, args);
    }
}
