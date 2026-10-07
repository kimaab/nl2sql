import { useEffect, useRef, useState } from "react";
import * as api from "../api";
import { Badge, Button, EmptyState, Loading, PageHeader, Segmented } from "../components/ui";
import { SystemPicker, useSystemParam } from "../components/SystemPicker";
import { useToast } from "../components/Toast";
import { errorMessage, formatDateTime } from "../lib/format";

const KIND_LABEL: Record<api.ReviewKind, string> = {
  table_card: "테이블 카드",
  metric_example: "지표 예시 질문",
  eval_case: "평가 문항",
};

type Edit = { draft: string; line: string; note: string };

/** LLM 이 만든 초안을 사람이 승인해야 질문 처리·평가에 쓰인다. */
export function ReviewPage() {
  const { systems, sysId, setSysId } = useSystemParam();
  const [status, setStatus] = useState<api.EnrichStatus | null>(null);
  const [items, setItems] = useState<api.ReviewItem[] | null>(null);
  const [runs, setRuns] = useState<api.EvalRun[]>([]);
  const [kind, setKind] = useState<api.ReviewKind>("table_card");
  const [edits, setEdits] = useState<Record<string, Edit>>({});
  const [busy, setBusy] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  const toast = useToast();

  async function load(quiet = false) {
    if (!sysId) return;
    try {
      const [s, list, r] = await Promise.all([api.enrichStatus(sysId), api.listReview(sysId), api.listEvalRuns(sysId)]);
      setStatus(s);
      setItems(list);
      setRuns(r);
      // 처리 중이면 몇 초마다 다시 본다
      window.clearTimeout(timer.current);
      if (s.queued + s.running > 0) timer.current = window.setTimeout(() => load(true), 3000);
    } catch (err) {
      if (!quiet) toast("불러오지 못했습니다: " + errorMessage(err), "error");
    }
  }

  useEffect(() => {
    setItems(null);
    setStatus(null);
    setEdits({});
    load();
    return () => window.clearTimeout(timer.current);
  }, [sysId]);

  async function start(enqueueAll: boolean) {
    try {
      setStatus(await api.startEnrich(sysId, enqueueAll));
      toast("보강을 시작했습니다. 초안이 만들어지는 대로 아래에 나타납니다", "ok");
      load(true);
    } catch (err) {
      toast("시작 실패: " + errorMessage(err), "error");
    }
  }

  const keyOf = (i: api.ReviewItem) => `${i.kind}:${i.id}`;
  const editOf = (i: api.ReviewItem): Edit => edits[keyOf(i)] ?? { draft: i.draft, line: i.draft_line, note: "" };
  const setEdit = (i: api.ReviewItem, patch: Partial<Edit>) =>
    setEdits((e) => ({ ...e, [keyOf(i)]: { ...editOf(i), ...patch } }));

  async function decide(i: api.ReviewItem, action: "approve" | "reject") {
    const e = editOf(i);
    const decision: api.ReviewDecision =
      action === "reject"
        ? { action, note: e.note }
        : i.kind === "table_card"
        ? { action, card: e.draft, card_line: e.line }
        : { action, question: e.draft };
    try {
      await api.decideReview(sysId, i.kind, i.id, decision);
      setItems((prev) => (prev ?? []).filter((x) => keyOf(x) !== keyOf(i)));
      setStatus((s) => s && { ...s })
    } catch (err) {
      toast("저장 실패: " + errorMessage(err), "error");
    }
  }

  async function approveAll() {
    const targets = (items ?? []).filter((i) => i.kind === kind);
    if (!confirm(`${KIND_LABEL[kind]} ${targets.length}건을 그대로 승인하시겠습니까?`)) return;
    setBusy(true);
    for (const i of targets) await decide(i, "approve");
    setBusy(false);
    load(true);
  }

  const shown = (items ?? []).filter((i) => i.kind === kind);
  const count = (k: api.ReviewKind) => (items ?? []).filter((i) => i.kind === k).length;

  return (
    <div className="page">
      <PageHeader
        title="보강 · 검수"
        desc="LLM 이 테이블 카드와 지표 예시 질문 초안을 만들고, 승인한 것만 질문 처리에 쓰입니다. 지표는 승인된 예시 질문이 있어야 '사용 중'이 됩니다."
        actions={<SystemPicker systems={systems} value={sysId} onChange={setSysId} />}
      />
      {!sysId ? (
        <div className="card">
          <EmptyState title="시스템을 고르세요" />
        </div>
      ) : status === null ? (
        <Loading />
      ) : (
        <>
          <div className="card card-pad row" style={{ gap: 10, marginBottom: 16, flexWrap: "wrap" }}>
            <strong>보강 대기열</strong>
            <Badge tone={status.queued ? "warn" : "info"}>대기 {status.queued}</Badge>
            <Badge tone={status.running ? "primary" : "info"}>처리 중 {status.running}</Badge>
            <Badge tone={status.failed ? "danger" : "info"}>실패 {status.failed}</Badge>
            <span className="spacer" />
            <Button onClick={() => start(false)} disabled={status.queued === 0}>
              대기열 처리
            </Button>
            <Button variant="primary" onClick={() => start(true)}>
              카드·예시 없는 것 모두 만들기
            </Button>
          </div>

          <div className="row" style={{ marginBottom: 12, gap: 12 }}>
            <Segmented
              options={(Object.keys(KIND_LABEL) as api.ReviewKind[]).map((k) => ({
                value: k,
                label: `${KIND_LABEL[k]} ${count(k)}`,
              }))}
              value={kind}
              onChange={setKind}
            />
            <span className="spacer" />
            {shown.length > 0 && (
              <Button onClick={approveAll} disabled={busy}>
                보이는 것 모두 승인
              </Button>
            )}
          </div>

          <div className="stack" style={{ gap: 12 }}>
            {items === null ? (
              <Loading />
            ) : shown.length === 0 ? (
              <div className="card">
                <EmptyState title="검수할 초안이 없습니다" />
              </div>
            ) : (
              shown.map((i) => {
                const e = editOf(i);
                return (
                  <div key={keyOf(i)} className="card card-pad stack" style={{ gap: 8 }}>
                    <div className="row" style={{ gap: 8 }}>
                      <strong>{i.target_name}</strong>
                      <span className="faint">{i.context}</span>
                    </div>
                    {i.kind === "table_card" && (
                      <input
                        className="input"
                        value={e.line}
                        maxLength={80}
                        onChange={(ev) => setEdit(i, { line: ev.target.value })}
                        title="한 줄 요약 (80자 이내) — 테이블 추론에 쓰입니다"
                      />
                    )}
                    <textarea
                      className="textarea"
                      rows={i.kind === "table_card" ? 6 : 1}
                      value={e.draft}
                      onChange={(ev) => setEdit(i, { draft: ev.target.value })}
                    />
                    <div className="row" style={{ gap: 8 }}>
                      <input
                        className="input"
                        placeholder="반려 사유 (다시 만들 때 LLM 에 전달됩니다)"
                        value={e.note}
                        onChange={(ev) => setEdit(i, { note: ev.target.value })}
                      />
                      <Button variant="danger" onClick={() => decide(i, "reject")}>
                        반려
                      </Button>
                      <Button variant="primary" onClick={() => decide(i, "approve")}>
                        승인
                      </Button>
                    </div>
                  </div>
                );
              })
            )}
          </div>

          <h2 className="section-title" style={{ marginTop: 28 }}>
            평가 실행
          </h2>
          <div className="card">
            {runs.length === 0 ? (
              <EmptyState
                title="평가 실행 기록이 없습니다"
                desc={
                  <>
                    <code>uv run python backend/evaluate.py --system-id {sysId} --generate 3</code> 로 평가 문항을 만들고 여기서
                    승인한 뒤, <code>--selector llm</code> 과 <code>--selector tfidf</code> 로 각각 돌려 비교합니다.
                  </>
                }
              />
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>실행</th>
                      <th>방식</th>
                      <th className="num">문항</th>
                      <th className="num">테이블 재현율</th>
                      <th className="num">지표 정확도</th>
                      <th className="num">SQL 일치</th>
                      <th className="num">평균 토큰</th>
                      <th className="num">평균 시간</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.map((r) => (
                      <tr key={r.id}>
                        <td>
                          <div className="cell-title">{r.label}</div>
                          <div className="cell-sub">{formatDateTime(r.started_at)}</div>
                        </td>
                        <td>{r.config.selector === "tfidf" ? <Badge>TF-IDF</Badge> : <Badge tone="primary">LLM</Badge>}</td>
                        <td className="num">{r.case_count}</td>
                        <td className="num">{pct(r.table_recall)}</td>
                        <td className="num">{pct(r.metric_accuracy)}</td>
                        <td className="num">{pct(r.sql_accuracy)}</td>
                        <td className="num">{r.avg_tokens ?? "-"}</td>
                        <td className="num">{r.avg_elapsed_ms ? `${(r.avg_elapsed_ms / 1000).toFixed(1)}초` : "-"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

const pct = (v: number | null) => (v === null ? "-" : `${Math.round(v * 1000) / 10}%`);
