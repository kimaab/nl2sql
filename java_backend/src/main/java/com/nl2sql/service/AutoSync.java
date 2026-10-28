package com.nl2sql.service;

import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.SmartLifecycle;
import org.springframework.stereotype.Component;

import com.nl2sql.common.ApiException;
import com.nl2sql.common.Env;
import com.nl2sql.db.MetaDb;

/**
 * 한 번이라도 동기화한 시스템을 주기적으로 다시 동기화한다 (scheduler.py).
 *
 * AUTO_SYNC_MINUTES 가 0(기본)이면 돌지 않는다. 결과는 동기화 기록에 trigger='auto' 로
 * 남고, 깨진 지표가 생기면 경고 로그와 동기화 현황 화면에 드러난다.
 */
@Component
public class AutoSync implements SmartLifecycle {

    private static final Logger log = LoggerFactory.getLogger("nl2sql.scheduler");

    private final MetaDb db;
    private final SystemService systems;
    private final double minutes;
    private ScheduledExecutorService executor;
    private volatile boolean running;

    public AutoSync(MetaDb db, SystemService systems, Env env) {
        this.db = db;
        this.systems = systems;
        String raw = env.get("AUTO_SYNC_MINUTES", "0");
        try {
            this.minutes = Double.parseDouble(raw.isEmpty() ? "0" : raw);
        } catch (NumberFormatException error) {
            throw new IllegalStateException("AUTO_SYNC_MINUTES 는 숫자여야 합니다 (받은 값: '" + raw + "')");
        }
    }

    public boolean enabled() {
        return minutes > 0;
    }

    @Override
    public void start() {
        running = true;
        if (!enabled()) {
            return;
        }
        long periodMs = (long) (minutes * 60_000);
        executor = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread thread = new Thread(r, "auto-sync");
            thread.setDaemon(true);
            return thread;
        });
        executor.scheduleWithFixedDelay(() -> {
            try {
                runOnce();
            } catch (RuntimeException error) {
                log.error("자동 동기화 반복 중 오류", error);
            }
        }, periodMs, periodMs, TimeUnit.MILLISECONDS);
        log.info("자동 동기화 시작: {}분마다", Math.round(minutes));
    }

    public void runOnce() {
        for (Map<String, Object> row : db.query("SELECT id, name FROM meta_system WHERE synced_at IS NOT NULL ORDER BY name")) {
            if (!running) {
                return;
            }
            try {
                systems.syncSystem(String.valueOf(row.get("id")), "auto");
            } catch (ApiException error) {
                log.warn("자동 동기화 실패: {} — {}", row.get("name"), error.detail());
            } catch (RuntimeException error) {
                log.error("자동 동기화 실패: {}", row.get("name"), error);
            }
        }
    }

    @Override
    public void stop() {
        running = false;
        if (executor != null) {
            executor.shutdownNow();
            try {
                executor.awaitTermination(5, TimeUnit.SECONDS);
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
            }
        }
    }

    @Override
    public boolean isRunning() {
        return running;
    }
}
