package com.nl2sql.service;

import java.util.HashMap;
import java.util.Map;
import java.util.function.Consumer;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

/**
 * 오래 걸리는 작업(지표 자동 등록 · 지표 검증 · 설명 자동 채우기)을 요청과 떼어 스레드에서 돌린다 (background.py).
 *
 * 실행 기록은 메타데이터 DB 에 남고, 화면은 기록을 다시 읽어 진행을 본다.
 * 지금 도는 실행은 이 프로세스의 메모리에만 있다 — 서버가 재시작되면 'running' 으로 남은 기록은 중단된 것이다.
 */
@Component
public class Background {

    private static final Logger log = LoggerFactory.getLogger("nl2sql.background");

    private final Map<String, String> active = new HashMap<>();  // 실행 id → 잠금 키 (작업 종류:시스템 id)

    public synchronized boolean isActive(Object runId) {
        return active.containsKey(String.valueOf(runId));
    }

    /**
     * work 를 스레드에서 돌린다. 같은 key 가 이미 돌면 시작하지 않고 false.
     *
     * work 가 예외로 끝나면 onError(오류) 를 부른다 — 실행 기록을 'error' 로 닫는 데 쓴다.
     */
    public boolean start(String key, Object runId, Runnable work, Consumer<Throwable> onError) {
        String run = String.valueOf(runId);
        synchronized (this) {
            for (String k : active.values()) {
                if (k.equals(key)) {
                    return false;
                }
            }
            active.put(run, key);
        }
        Thread thread = new Thread(() -> {
            try {
                work.run();
            } catch (Throwable error) {
                log.error("{} 실행 {} 실패", key, run, error);
                try {
                    onError.accept(error);
                } catch (Throwable inner) {
                    log.error("{} 실행 {} 의 실패 기록을 남기지 못했습니다", key, run, inner);
                }
            } finally {
                synchronized (this) {
                    active.remove(run);
                }
            }
        }, key.split(":")[0] + "-" + run.substring(0, Math.min(8, run.length())));
        thread.setDaemon(true);
        thread.start();
        return true;
    }
}
