package com.nl2sql.service;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import com.nl2sql.common.ApiException;
import com.nl2sql.common.Dyn;
import com.nl2sql.common.Json;
import com.nl2sql.common.Py;
import com.nl2sql.common.Times;
import com.nl2sql.db.MetaDb;
import com.nl2sql.model.MetricModels.AskStep;
import com.nl2sql.model.MetricModels.HistoryEntry;
import com.nl2sql.model.MetricModels.HistoryPatch;

/** 질문 기록 (history.py) */
@Service
public class HistoryService {

    private static final Logger log = LoggerFactory.getLogger("nl2sql.history");

    private final MetaDb db;

    public HistoryService(MetaDb db) {
        this.db = db;
    }

    /** 질문 결과와 단계별 기록(ask_trace)을 남긴다. 기록이 실패해도 답은 돌려준다 — 기록은 부가 기능이다. */
    public String recordAsk(String systemId, String question, Map<String, Object> outcome, double elapsedMs) {
        String entryId = UUID.randomUUID().toString();
        try {
            db.transaction(tx -> {
                tx.update("""
                        INSERT INTO ask_log (id, system_id, question, selected_tables, selected_metrics, ast, sql, error,
                                             clarification, attempts, total_tokens, elapsed_ms)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        entryId, systemId, question,
                        Json.dumps(outcome.getOrDefault("tables", List.of())),
                        Json.dumps(outcome.getOrDefault("metrics", List.of())),
                        outcome.get("ast") != null ? Json.dumps(outcome.get("ast")) : null,
                        outcome.get("sql"), outcome.get("error"), outcome.get("clarification"),
                        Dyn.integer(outcome, "attempts"), Dyn.integer(outcome, "total_tokens"), (int) elapsedMs);
                List<Object[]> traceRows = new ArrayList<>();
                for (Map<String, Object> s : Dyn.maps(outcome.get("steps"))) {
                    traceRows.add(new Object[] {entryId, s.get("stage"), Json.dumps(s.get("candidates")),
                            Json.dumps(s.get("selected")), s.get("reason"), s.get("prompt_tokens"),
                            s.get("completion_tokens"), s.get("elapsed_ms"), s.get("model")});
                }
                tx.batch("""
                        INSERT INTO ask_trace (ask_id, stage, candidates, selected, reason, prompt_tokens,
                                               completion_tokens, elapsed_ms, model)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, traceRows);
            });
        } catch (RuntimeException error) {
            log.error("질문 기록 실패: {}", question, error);
            return null;
        }
        return entryId;
    }

    public List<HistoryEntry> listHistory(String systemId, Boolean favorite, String feedback, int limit, int offset) {
        List<HistoryEntry> entries = new ArrayList<>();
        for (Map<String, Object> r : db.query("""
                SELECT a.*, s.name AS system_name
                  FROM ask_log a JOIN meta_system s ON s.id = a.system_id
                 WHERE (?::uuid IS NULL OR a.system_id = ?::uuid)
                   AND (?::boolean IS NULL OR a.favorite = ?::boolean)
                   AND (?::text IS NULL OR a.feedback = ?::text)
                 ORDER BY a.created_at DESC
                 LIMIT ? OFFSET ?
                """, systemId, systemId, favorite, favorite, feedback, feedback,
                Math.min(Math.max(limit, 1), 500), Math.max(offset, 0))) {
            entries.add(toEntry(r));
        }
        return entries;
    }

    public HistoryEntry getHistory(String entryId) {
        Map<String, Object> row = db.one(
                "SELECT a.*, s.name AS system_name FROM ask_log a JOIN meta_system s ON s.id = a.system_id WHERE a.id = ?",
                entryId);
        if (row == null) {
            throw new ApiException(404, "질문 기록을 찾을 수 없습니다");
        }
        return toEntry(row);
    }

    /** 질문 하나의 단계별 기록 — 어느 단계에서 틀렸는지 가르는 근거 */
    public List<AskStep> getTrace(String entryId) {
        getHistory(entryId);
        List<AskStep> steps = new ArrayList<>();
        for (Map<String, Object> r : db.query("SELECT * FROM ask_trace WHERE ask_id = ? ORDER BY id", entryId)) {
            steps.add(new AskStep((String) r.get("stage"), Dyn.items(r.get("candidates")), Dyn.items(r.get("selected")),
                    (String) r.get("reason"), Dyn.integer(r, "prompt_tokens"), Dyn.integer(r, "completion_tokens"),
                    Dyn.integer(r, "elapsed_ms")));
        }
        return steps;
    }

    public HistoryEntry patchHistory(String entryId, HistoryPatch patch) {
        HistoryEntry current = getHistory(entryId);
        boolean favorite = patch.favorite() == null ? current.favorite() : patch.favorite();
        String feedback = patch.feedback() == null ? current.feedback() : (patch.feedback().isEmpty() ? null : patch.feedback());
        String note = patch.feedbackNote() == null ? current.feedbackNote() : patch.feedbackNote().strip();
        if ("up".equals(feedback) && !Py.truthy(current.sql())) {
            throw new ApiException(400, "SQL 이 없는 기록은 '맞음'으로 표시할 수 없습니다");
        }
        db.update("UPDATE ask_log SET favorite = ?, feedback = ?, feedback_note = ? WHERE id = ?", favorite, feedback, note, entryId);
        return getHistory(entryId);
    }

    public void deleteHistory(String entryId) {
        db.update("DELETE FROM ask_log WHERE id = ?", entryId);
    }

    /** '맞음' 피드백을 받은 기록을 평가셋 파일 형식으로. backend/evaluate.py --cases 가 그대로 읽는다. */
    public Map<String, Object> exportEvalCases(String systemId) {
        Map<String, Object> system = db.one("SELECT name FROM meta_system WHERE id = ?", systemId);
        if (system == null) {
            throw new ApiException(404, "시스템을 찾을 수 없습니다");
        }
        List<Object> cases = new ArrayList<>();
        for (Map<String, Object> r : db.query("""
                SELECT DISTINCT ON (lower(question)) question, ast, sql, feedback_note, created_at
                  FROM ask_log
                 WHERE system_id = ? AND feedback = 'up' AND sql IS NOT NULL
                 ORDER BY lower(question), created_at DESC
                """, systemId)) {
            Map<String, Object> c = Dyn.of("question", r.get("question"), "ast", r.get("ast"), "expected_sql", r.get("sql"));
            if (Py.truthy(r.get("feedback_note"))) {
                c.put("note", r.get("feedback_note"));
            }
            cases.add(c);
        }
        return Dyn.of("system", system.get("name"), "cases", cases);
    }

    private static HistoryEntry toEntry(Map<String, Object> row) {
        return new HistoryEntry(
                String.valueOf(row.get("id")),
                String.valueOf(row.get("system_id")),
                (String) row.get("system_name"),
                (String) row.get("question"),
                Dyn.items(row.get("selected_tables")),
                Dyn.items(row.get("selected_metrics")),
                Dyn.integer(row, "total_tokens"),
                (String) row.get("sql"),
                row.get("ast"),
                (String) row.get("error"),
                (String) row.get("clarification"),
                Dyn.integer(row, "attempts"),
                Dyn.integer(row, "elapsed_ms"),
                Boolean.TRUE.equals(row.get("favorite")),
                (String) row.get("feedback"),
                (String) row.get("feedback_note"),
                Times.iso((OffsetDateTime) row.get("created_at")));
    }
}
