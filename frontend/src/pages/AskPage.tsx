import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import * as api from "../api";
import { Badge, Button, Chip, SqlBlock } from "../components/ui";
import { useToast } from "../components/Toast";
import { errorMessage } from "../lib/format";

interface Entry extends api.AskResponse {
  question: string;
  favorite: boolean;
  feedback: api.Feedback | null;
}

const EXAMPLES = ["지난달 매출 합계는?", "매출 상위 10개 고객", "이번 달과 지난달 주문 수 비교"];

const STAGE_LABEL = { table: "테이블 추론", metric: "지표 선택", sql: "조회 명세" } as const;

/** 단계별로 무엇을 골랐고 왜 골랐는지 — 틀렸을 때 어느 단계에서 틀렸는지 보인다. */
export function Steps({ steps }: { steps: api.AskStep[] }) {
  const tokens = steps.reduce((n, s) => n + s.prompt_tokens + s.completion_tokens, 0);
  const ms = steps.reduce((n, s) => n + s.elapsed_ms, 0);
  return (
    <details className="steps">
      <summary>
        선택 과정 · {steps.map((s) => STAGE_LABEL[s.stage]).join(" → ")} · 토큰 {tokens.toLocaleString()} ·{" "}
        {(ms / 1000).toFixed(1)}초
      </summary>
      {steps.map((s, i) => (
        <div key={i} className="step">
          <span className="step-name">{STAGE_LABEL[s.stage]}</span>
          <div>
            {s.stage === "sql" ? (
              <code>{s.selected[0] ?? "-"}</code>
            ) : (
              <>
                {s.selected.length ? s.selected.join(", ") : <span className="faint">없음</span>}
                <span className="faint"> / 후보 {s.candidates.length}개</span>
              </>
            )}
            {s.reason && <div className="step-reason">{s.reason}</div>}
          </div>
          <span className="faint">{(s.prompt_tokens + s.completion_tokens).toLocaleString()} 토큰</span>
        </div>
      ))}
    </details>
  );
}

export function AskPage() {
  const [params] = useSearchParams();
  const [systems, setSystems] = useState<api.System[] | null>(null);
  const [systemId, setSystemId] = useState(params.get("ds") ?? "");
  const [question, setQuestion] = useState(params.get("q") ?? "");
  const [history, setHistory] = useState<Entry[]>([]);
  const [loading, setLoading] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
  const [copiedIndex, setCopiedIndex] = useState<number | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const toast = useToast();

  useEffect(() => {
    api
      .listSystems()
      .then((data) => {
        const ready = data.filter((d) => d.synced_at); // 동기화된 것만
        setSystems(ready);
        if (ready.length === 1 && !params.get("ds")) setSystemId(ready[0].id);
      })
      .catch((err) => {
        toast("시스템 로드 실패: " + errorMessage(err), "error");
        setSystems([]);
      });
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [history.length, pending]);

  async function copySql(index: number, sql: string) {
    try {
      await navigator.clipboard.writeText(sql);
      setCopiedIndex(index);
      // 잠시 뒤 원래 라벨로 돌린다. 계속 "복사됨"이면 다음에 눌렀을 때
      // 눌린 것인지 알 수 없다.
      setTimeout(() => setCopiedIndex((c) => (c === index ? null : c)), 1500);
    } catch {
      toast("클립보드에 쓰지 못했습니다. SQL을 직접 선택해 복사하십시오.", "error");
    }
  }

  async function handleSubmit() {
    const asked = question.trim();
    if (!systemId) {
      toast("시스템을 먼저 선택하세요", "error");
      return;
    }
    if (!asked || loading) return;

    setLoading(true);
    setPending(asked);
    setQuestion("");
    try {
      // 서버는 이전 질문을 모른다 — body 에는 이번 질문 하나만 싣는다.
      const res = await api.ask(systemId, asked);
      setHistory((prev) => [...prev, { question: asked, ...res, favorite: false, feedback: null }]);
      if (res.clarification) {
        // 되물었으면 원래 질문을 돌려놓고, 답을 덧붙여 한 문장으로 다시 보내게 한다
        setQuestion(asked + " ");
        setTimeout(() => inputRef.current?.focus(), 0);
      }
    } catch (err) {
      // 실패한 질문은 입력창에 되돌려서 다시 보낼 수 있게 한다
      setQuestion(asked);
      toast("질문 처리 실패: " + errorMessage(err), "error");
    } finally {
      setPending(null);
      setLoading(false);
    }
  }

  async function update(index: number, patch: api.HistoryPatch) {
    const entry = history[index];
    if (!entry.id) {
      toast("이 답은 기록되지 않아 평가할 수 없습니다", "error");
      return;
    }
    try {
      const saved = await api.patchHistory(entry.id, patch);
      setHistory((prev) =>
        prev.map((e, i) => (i === index ? { ...e, favorite: saved.favorite, feedback: saved.feedback } : e))
      );
    } catch (err) {
      toast("저장 실패: " + errorMessage(err), "error");
    }
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    // 한글 조합 중 Enter는 글자 확정이므로 전송하지 않는다
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSubmit();
    }
  }

  const noSystem = systems !== null && systems.length === 0;

  return (
    <div className="ask">
      <div className="ask-bar">
        <strong>SQL 생성</strong>
        <span className="spacer" />
        <Link to={systemId ? `/history?ds=${systemId}` : "/history"} className="muted">
          질문 기록 →
        </Link>
        <select
          className="select"
          value={systemId}
          onChange={(e) => setSystemId(e.target.value)}
          disabled={loading}
        >
          <option value="">시스템 선택</option>
          {(systems ?? []).map((d) => (
            <option key={d.id} value={d.id}>
              {d.name} ({d.driver})
            </option>
          ))}
        </select>
      </div>

      <div className="ask-scroll">
        <div className="ask-inner">
          {history.length === 0 && !pending && (
            <div className="empty">
              <div className="empty-title">무엇이 궁금하세요?</div>
              {noSystem ? (
                <p className="empty-desc">
                  동기화된 시스템이 없습니다. <Link to="/systems/new">시스템을 등록</Link>하고
                  스키마를 동기화하세요.
                </p>
              ) : (
                <>
                  <p className="empty-desc">자연어로 질문하면 SQL을 만들어 드립니다.</p>
                  <div className="examples">
                    {EXAMPLES.map((ex) => (
                      <Chip key={ex} onClick={() => setQuestion(ex)}>
                        {ex}
                      </Chip>
                    ))}
                  </div>
                </>
              )}
            </div>
          )}

          {history.map((entry, i) => (
            <div key={i} className="stack" style={{ gap: 12 }}>
              <div className="q-bubble">{entry.question}</div>
              <div className="card a-card card-pad">
                <div className="a-meta">
                  {entry.sql ? (
                    <Badge tone="ok">✓ 성공</Badge>
                  ) : entry.clarification ? (
                    <Badge tone="warn">? 확인 필요</Badge>
                  ) : (
                    <Badge tone="danger">✗ 실패</Badge>
                  )}
                  <span className="faint">{entry.attempts}번 시도</span>
                  <span className="spacer" />
                  {entry.id && (
                    <Button
                      size="sm"
                      variant="ghost"
                      title="즐겨찾기"
                      onClick={() => update(i, { favorite: !entry.favorite })}
                    >
                      {entry.favorite ? "★ 즐겨찾기" : "☆ 즐겨찾기"}
                    </Button>
                  )}
                  {entry.sql && entry.id && (
                    <>
                      <Button
                        size="sm"
                        variant={entry.feedback === "up" ? "primary" : "ghost"}
                        onClick={() => update(i, { feedback: entry.feedback === "up" ? "" : "up" })}
                      >
                        맞음
                      </Button>
                      <Button
                        size="sm"
                        variant={entry.feedback === "down" ? "danger" : "ghost"}
                        onClick={() => update(i, { feedback: entry.feedback === "down" ? "" : "down" })}
                      >
                        틀림
                      </Button>
                    </>
                  )}
                  {entry.sql && (
                    <Button size="sm" onClick={() => copySql(i, entry.sql!)}>
                      {copiedIndex === i ? "복사됨" : "SQL 복사"}
                    </Button>
                  )}
                </div>
                {entry.sql ? (
                  <SqlBlock sql={entry.sql} />
                ) : entry.clarification ? (
                  <div className="alert warn">
                    {entry.clarification}
                    <div className="field-hint" style={{ marginTop: 6 }}>
                      원래 질문을 입력창에 돌려놓았습니다. 답을 덧붙여 한 문장으로 다시 보내세요.
                    </div>
                  </div>
                ) : (
                  <div className="alert danger">{entry.error}</div>
                )}
                {entry.steps?.length > 0 && <Steps steps={entry.steps} />}
              </div>
            </div>
          ))}

          {pending && (
            <div className="stack" style={{ gap: 12 }}>
              <div className="q-bubble">{pending}</div>
              <div className="card a-card card-pad muted">SQL을 생성하는 중...</div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      </div>

      <div className="ask-input">
        <div className="ask-input-inner">
          <textarea
            ref={inputRef}
            className="textarea"
            rows={2}
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={onKeyDown}
            disabled={loading}
            placeholder="질문을 입력하세요 (Enter 전송 · Shift+Enter 줄바꿈)"
          />
          <Button variant="primary" onClick={handleSubmit} disabled={loading || !question.trim()}>
            {loading ? "생성 중..." : "전송"}
          </Button>
        </div>
      </div>
    </div>
  );
}
