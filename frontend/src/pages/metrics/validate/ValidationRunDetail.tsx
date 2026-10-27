import { Fragment, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import * as api from "../../../api";
import { Alert, Badge, Button, EmptyState, Loading, PageHeader, Segmented } from "../../../components/ui";
import { useToast } from "../../../components/Toast";
import { errorMessage, formatDateTime } from "../../../lib/format";
import { RUN_STATUS_LABEL, RUN_STATUS_TONE, usePolling, VERDICT_LABEL, VERDICT_TONE } from "../../../lib/metricAuto";
import { ValidationChecks } from "./ValidationChecks";

type Filter = "all" | "pass" | "warn" | "fail";

export function ValidationRunDetail() {
  const { dsId = "", runId = "" } = useParams();
  const [run, setRun] = useState<api.ValidationRunDetail | null | undefined>(undefined);
  const [open, setOpen] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  /** 행에서 '검증' 을 눌러 다시 돌리는 중인 지표와 그 실행 (시스템마다 검증은 하나씩만 돈다) */
  const [pending, setPending] = useState<{ metricId: string; runId: string } | null>(null);
  /** 다시 검증한 결과. 이 실행의 원래 결과 대신 보여 준다 */
  const [fresh, setFresh] = useState<Record<string, api.ValidationItem>>({});
  /** 이 화면에서 지운 지표. 검증 기록은 남으므로 행은 두고 '삭제됨' 으로 보인다 */
  const [deleted, setDeleted] = useState<string[]>([]);
  const toast = useToast();
  const navigate = useNavigate();

  function load() {
    api
      .getValidation(dsId, runId)
      .then(setRun)
      .catch((err) => {
        toast("검증 기록을 불러오지 못했습니다: " + errorMessage(err), "error");
        setRun(null);
      });
  }

  useEffect(load, [dsId, runId]);
  usePolling(run?.status === "running", load);
  usePolling(pending !== null, checkPending);

  async function revalidate(metricId: string) {
    if (!run) return;
    try {
      const started = await api.startValidation(dsId, {
        metric_ids: [metricId],
        run_sql: Boolean(run.options.run_sql),
        use_llm: Boolean(run.options.use_llm),
        allow_full_scan: Boolean(run.options.allow_full_scan),
      });
      setPending({ metricId, runId: started.id });
    } catch (err) {
      toast("검증을 시작하지 못했습니다: " + errorMessage(err), "error");
    }
  }

  async function remove(item: api.ValidationItem) {
    if (!confirm(`지표 '${item.metric_name}'을(를) 삭제하시겠습니까? 정의는 이력에 남습니다.`)) return;
    try {
      await api.deleteMetric(dsId, item.metric_id);
      setDeleted((list) => [...list, item.metric_id]);
      toast("삭제했습니다", "ok");
    } catch (err) {
      toast("삭제 실패: " + errorMessage(err), "error");
    }
  }

  async function checkPending() {
    if (!pending) return;
    try {
      const result = await api.getValidation(dsId, pending.runId);
      if (result.status === "running") return;
      const item = result.items.find((i) => i.metric_id === pending.metricId);
      if (item) {
        setFresh((prev) => ({ ...prev, [pending.metricId]: item }));
        setOpen(pending.metricId);
        toast(`다시 검증했습니다: ${VERDICT_LABEL[item.verdict]}`, item.verdict === "fail" ? "error" : "ok");
      } else {
        toast("검증이 끝나지 못했습니다: " + (result.error ?? RUN_STATUS_LABEL[result.status]), "error");
      }
    } catch (err) {
      toast("검증 결과를 불러오지 못했습니다: " + errorMessage(err), "error");
    }
    setPending(null);
  }

  if (run === undefined) {
    return (
      <div className="page">
        <Loading />
      </div>
    );
  }
  if (run === null) {
    return (
      <div className="page">
        <PageHeader back={{ to: "/metrics/validate", label: "지표 검증" }} title="검증 기록을 찾을 수 없습니다" />
      </div>
    );
  }

  const shown = run.items.map((item) => fresh[item.metric_id] ?? item);
  const items = filter === "all" ? shown : shown.filter((i) => i.verdict === filter);

  return (
    <div className="page wide">
      <PageHeader back={{ to: `/metrics/validate?ds=${dsId}`, label: "지표 검증" }} title={`지표 검증 ${formatDateTime(run.started_at)}`} />

      <div className="stack">
        <div className="card card-pad">
          <div className="row wrap" style={{ gap: 16 }}>
            <Badge tone={RUN_STATUS_TONE[run.status]}>{RUN_STATUS_LABEL[run.status]}</Badge>
            <span>
              진행 {run.done} / {run.total}
            </span>
            <span>통과 {run.passed}</span>
            <span>주의 {run.warned}</span>
            <span>실패 {run.failed}</span>
            <span className="faint">
              대상 DB {run.options.run_sql ? "점검" : "끔"}, LLM {run.options.use_llm ? "검토" : "끔"}, 전체 훑기{" "}
              {run.options.allow_full_scan ? "허용" : "막음"}
            </span>
            {run.options.use_llm && (
              <span className="faint">
                토큰 {run.prompt_tokens.toLocaleString()} + {run.completion_tokens.toLocaleString()}
              </span>
            )}
            {run.finished_at && <span className="faint">끝: {formatDateTime(run.finished_at)}</span>}
          </div>
          {Object.keys(fresh).length > 0 && (
            <div className="faint" style={{ marginTop: 8 }}>
              위 건수는 이 실행의 원래 결과입니다. 다시 검증한 지표 {Object.keys(fresh).length}개는 목록에 새 결과로 보입니다.
            </div>
          )}
          {run.status === "running" && (
            <div className="faint" style={{ marginTop: 8 }}>
              진행 중입니다. 2초마다 다시 읽습니다.
            </div>
          )}
          {run.status === "interrupted" && (
            <div style={{ marginTop: 8 }}>
              <Alert tone="warn">서버가 재시작돼 검증이 멈췄습니다. 다시 실행하십시오.</Alert>
            </div>
          )}
          {run.error && (
            <div style={{ marginTop: 8 }}>
              <Alert tone="danger">{run.error}</Alert>
            </div>
          )}
        </div>

        <div className="card">
          <div className="card-head">
            <span className="card-title">지표별 결과</span>
            <span className="faint">{items.length}건</span>
            <span className="spacer" />
            <Segmented<Filter>
              value={filter}
              onChange={setFilter}
              options={[
                { value: "all", label: "전체" },
                { value: "fail", label: "실패" },
                { value: "warn", label: "주의" },
                { value: "pass", label: "통과" },
              ]}
            />
          </div>
          {items.length === 0 ? (
            <EmptyState title={run.status === "running" ? "검증하는 중입니다" : "결과가 없습니다"} />
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th style={{ width: "22%" }}>지표</th>
                    <th>판정</th>
                    <th>사유</th>
                    <th>점검</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {items.map((item) => {
                    const busy = pending?.metricId === item.metric_id;
                    const again = fresh[item.metric_id] !== undefined;
                    const gone = deleted.includes(item.metric_id);
                    return (
                      <Fragment key={item.metric_id}>
                        <tr className="clickable" onClick={() => setOpen(open === item.metric_id ? null : item.metric_id)}>
                          <td>
                            <div className="cell-title">{item.metric_name}</div>
                            <div className="cell-sub">
                              <Link to={`/metrics/${dsId}/${item.metric_id}`} onClick={(e) => e.stopPropagation()}>
                                지표 상세
                              </Link>{" "}
                              <Link
                                to={`/metrics/validate/${dsId}/metric/${item.metric_id}`}
                                onClick={(e) => e.stopPropagation()}
                              >
                                검증 이력
                              </Link>
                            </div>
                          </td>
                          <td>
                            <Badge tone={VERDICT_TONE[item.verdict]}>{VERDICT_LABEL[item.verdict]}</Badge>
                            {again && (
                              <div className="faint" style={{ marginTop: 4 }}>
                                다시 검증 {formatDateTime(item.created_at)}
                              </div>
                            )}
                          </td>
                          <td className="muted">{item.summary}</td>
                          <td>
                            <div className="row wrap" style={{ gap: 4 }}>
                              {item.checks.map((c) => (
                                <span key={c.key} title={c.message}>
                                  <Badge tone={VERDICT_TONE[c.status]}>{c.label}</Badge>
                                </span>
                              ))}
                            </div>
                          </td>
                          <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                            {gone ? (
                              <Badge tone="danger">삭제됨</Badge>
                            ) : (
                              <>
                                <Button
                                  size="sm"
                                  disabled={pending !== null || run.status === "running"}
                                  title="이 지표만 지금 정의로 다시 검증합니다 (이 실행과 같은 옵션)"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    revalidate(item.metric_id);
                                  }}
                                >
                                  {busy ? "검증 중..." : "검증"}
                                </Button>
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    navigate(`/metrics/${dsId}/${item.metric_id}/edit`);
                                  }}
                                >
                                  수정
                                </Button>
                                <Button
                                  size="sm"
                                  variant="danger"
                                  disabled={busy || run.status === "running"}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    remove(item);
                                  }}
                                >
                                  삭제
                                </Button>
                              </>
                            )}
                          </td>
                        </tr>
                        {open === item.metric_id && (
                          <tr>
                            <td colSpan={5}>
                              <ValidationChecks item={item} />
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
