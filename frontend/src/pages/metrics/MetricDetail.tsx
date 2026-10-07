import { Fragment, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import * as api from "../../api";
import { Alert, Badge, Button, EmptyState, Loading, PageHeader, SqlBlock } from "../../components/ui";
import { useToast } from "../../components/Toast";
import { errorMessage, formatDateTime } from "../../lib/format";
import { brokenReason, definitionLines, KIND_LABEL, OPERATORS, SOURCES, STATUS_LABEL, STATUS_TONE } from "../../lib/metrics";

const ACTION_LABEL = { create: "등록", update: "수정", delete: "삭제", retire: "사용 중지" } as const;

export function MetricDetail() {
  const { dsId = "", metricId = "" } = useParams();
  const [system, setSystem] = useState<api.System | null>(null);
  const [metric, setMetric] = useState<api.Metric | null | undefined>(undefined);
  const [all, setAll] = useState<api.Metric[]>([]);
  const [tables, setTables] = useState<api.SchemaTable[] | null>(null);
  const [history, setHistory] = useState<api.MetricHistoryEntry[]>([]);
  const [openIndex, setOpenIndex] = useState<number | null>(null);
  const toast = useToast();
  const navigate = useNavigate();

  useEffect(() => {
    (async () => {
      try {
        const [ds, list] = await Promise.all([api.getSystem(dsId), api.listMetrics(dsId)]);
        setSystem(ds);
        setAll(list);
        setMetric(list.find((m) => m.id === metricId) ?? null);
        api.metricHistory(dsId, metricId).then(setHistory).catch(() => setHistory([]));
        if (ds.synced_at) {
          try {
            setTables((await api.getSchema(dsId)).tables);
          } catch {
            setTables(null);
          }
        }
      } catch (err) {
        toast("지표를 불러오지 못했습니다: " + errorMessage(err), "error");
        setMetric(null);
      }
    })();
  }, [dsId, metricId]);

  async function handleDelete() {
    if (!metric || !confirm(`지표 '${metric.name}'을(를) 삭제하시겠습니까? 정의는 이력에 남습니다.`)) return;
    try {
      await api.deleteMetric(dsId, metric.id);
      toast("삭제했습니다", "ok");
      navigate(`/metrics?ds=${dsId}`);
    } catch (err) {
      toast("삭제 실패: " + errorMessage(err), "error");
    }
  }

  if (metric === undefined) {
    return (
      <div className="page narrow">
        <Loading />
      </div>
    );
  }

  if (metric === null) {
    return (
      <div className="page narrow">
        <PageHeader back={{ to: "/metrics", label: "지표 목록" }} title="지표를 찾을 수 없습니다" />
        <div className="card">
          <EmptyState title="삭제되었거나 존재하지 않는 지표입니다" />
        </div>
      </div>
    );
  }

  const broken = brokenReason(tables, metric, all);

  return (
    <div className="page narrow">
      <PageHeader
        back={{ to: `/metrics?ds=${dsId}`, label: "지표 목록" }}
        title={metric.name}
        desc={metric.description || "설명이 없습니다 — 질문이 이 지표에 잘 걸리지 않습니다."}
        actions={
          <>
            <Button onClick={() => navigate(`/metrics/${dsId}/${metric.id}/edit`)}>수정</Button>
            <Button variant="danger" onClick={handleDelete}>
              삭제
            </Button>
          </>
        }
      />

      <div className="stack">
        {(metric.broken_reason || broken) && (
          <Alert tone="warn">스키마와 맞지 않아 질문에 쓰이지 않습니다 — {metric.broken_reason || broken}</Alert>
        )}
        {metric.status === "draft" && (
          <Alert tone="info">
            검수 대기 — 승인된 예시 질문이 있어야 질문에 쓰입니다.{" "}
            {metric.draft_example_count > 0 ? (
              <Link to={`/review?ds=${dsId}`}>LLM 이 만든 예시 {metric.draft_example_count}개를 검수하세요</Link>
            ) : (
              "예시 질문을 직접 추가하거나 보강 화면에서 초안을 만드세요."
            )}
          </Alert>
        )}
        {metric.synonyms.length > 0 && (
          <div className="faint">같은 말: {metric.synonyms.join(", ")}</div>
        )}

        <div className="card">
          <div className="card-head">
            <span className="card-title">정의</span>
            <Badge tone="primary">{KIND_LABEL[metric.kind]}</Badge>
            <Badge tone={STATUS_TONE[metric.status]}>{STATUS_LABEL[metric.status]}</Badge>
            <span className="faint">v{metric.version}</span>
          </div>
          <div className="card-pad">
            <SqlBlock sql={definitionLines(metric)} />
          </div>
        </div>

        <div className="card">
          <div className="card-head">
            <span className="card-title">고정 필터</span>
            <span className="faint">{metric.fixed_filters.length}개</span>
          </div>
          {metric.fixed_filters.length === 0 ? (
            <div className="card-pad muted">고정 필터가 없습니다.</div>
          ) : (
            <table className="table">
              <tbody>
                {metric.fixed_filters.map((f, i) => {
                  const src = f.source ?? "literal";
                  const op = OPERATORS.find((o) => o.value === f.operator);
                  return (
                    <tr key={i}>
                      <td>
                        <code>{f.field}</code>
                      </td>
                      <td>{op?.label ?? f.operator}</td>
                      <td>
                        {op?.noValue ? (
                          <span className="faint">-</span>
                        ) : src === "question" ? (
                          <span className="faint">질문에서</span>
                        ) : (
                          <code>{String(f.value)}</code>
                        )}
                      </td>
                      <td style={{ textAlign: "right" }}>
                        <Badge tone={src === "literal" ? "info" : "primary"}>
                          {SOURCES.find((s) => s.value === src)?.label}
                        </Badge>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>

        <div className="card">
          <div className="card-head">
            <span className="card-title">예시 질문</span>
            <span className="faint">{metric.examples.length}개</span>
          </div>
          {metric.examples.length === 0 ? (
            <div className="card-pad muted">예시가 없습니다. 수정에서 추가하면 검색과 정확도가 좋아집니다.</div>
          ) : (
            <div className="card-pad stack" style={{ gap: 10 }}>
              {metric.examples.map((e, i) => (
                <div key={i}>
                  <div className="cell-title">{e.question}</div>
                  {e.ast ? (
                    <pre className="sql">{JSON.stringify(e.ast)}</pre>
                  ) : (
                    <div className="cell-sub">검색용 (AST 없음)</div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="card">
          <div className="card-head">
            <span className="card-title">변경 이력</span>
            <span className="faint">{history.length}건</span>
          </div>
          {history.length === 0 ? (
            <div className="card-pad muted">이력이 없습니다.</div>
          ) : (
            <table className="table">
              <tbody>
                {history.map((h, i) => (
                  <Fragment key={i}>
                    <tr
                      className="clickable"
                      onClick={() => setOpenIndex(openIndex === i ? null : i)}
                    >
                      <td>v{h.version}</td>
                      <td>
                        <Badge tone={h.action === "delete" ? "danger" : h.action === "create" ? "ok" : "info"}>
                          {ACTION_LABEL[h.action]}
                        </Badge>
                      </td>
                      <td>{h.snapshot.name}</td>
                      <td className="faint" style={{ textAlign: "right" }}>
                        {formatDateTime(h.created_at)}
                      </td>
                    </tr>
                    {openIndex === i && (
                      <tr>
                        <td colSpan={4}>
                          <SqlBlock sql={definitionLines(h.snapshot)} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="card card-pad">
          <dl className="kv" style={{ margin: 0 }}>
            <dt>시스템</dt>
            <dd>{system?.name}</dd>
            <dt>테이블</dt>
            <dd>
              <code>{[metric.table_name, ...metric.joins.map((j) => j.table)].join(", ")}</code>
            </dd>
            {metric.updated_at && (
              <>
                <dt>마지막 수정</dt>
                <dd>{formatDateTime(metric.updated_at)}</dd>
              </>
            )}
          </dl>
        </div>
      </div>
    </div>
  );
}
