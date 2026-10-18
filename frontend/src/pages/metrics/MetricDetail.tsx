import { Fragment, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import * as api from "../../api";
import { Alert, Badge, Button, EmptyState, Loading, PageHeader, SqlBlock } from "../../components/ui";
import { useToast } from "../../components/Toast";
import { errorMessage, formatDateTime } from "../../lib/format";
import { formatSql, STATUS_LABEL, STATUS_TONE } from "../../lib/metrics";

const ACTION_LABEL = { create: "등록", update: "수정", delete: "삭제" } as const;

export function MetricDetail() {
  const { dsId = "", metricId = "" } = useParams();
  const [system, setSystem] = useState<api.System | null>(null);
  const [metric, setMetric] = useState<api.Metric | null | undefined>(undefined);
  const [history, setHistory] = useState<api.MetricHistoryEntry[]>([]);
  const [openIndex, setOpenIndex] = useState<number | null>(null);
  const toast = useToast();
  const navigate = useNavigate();

  useEffect(() => {
    (async () => {
      try {
        const [ds, list] = await Promise.all([api.getSystem(dsId), api.listMetrics(dsId)]);
        setSystem(ds);
        setMetric(list.find((m) => m.id === metricId) ?? null);
        api.metricHistory(dsId, metricId).then(setHistory).catch(() => setHistory([]));
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

  return (
    <div className="page narrow">
      <PageHeader
        back={{ to: `/metrics?ds=${dsId}`, label: "지표 목록" }}
        title={metric.name}
        desc={metric.description || "설명이 없습니다 — LLM 이 이 지표를 고르기 어렵습니다."}
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
        {metric.broken_reason && (
          <Alert tone="warn">질문에 쓰이지 않습니다 — {metric.broken_reason}</Alert>
        )}
        {metric.synonyms.length > 0 && <div className="faint">같은 말: {metric.synonyms.join(", ")}</div>}

        <div className="card">
          <div className="card-head">
            <span className="card-title">등록한 SQL</span>
            <Badge tone={STATUS_TONE[metric.status]}>{STATUS_LABEL[metric.status]}</Badge>
            <span className="faint">v{metric.version}</span>
          </div>
          <div className="card-pad">
            <SqlBlock sql={metric.sql} />
          </div>
        </div>

        <div className="card">
          <div className="card-head">
            <span className="card-title">조회 명세(AST)에서 만든 SQL</span>
          </div>
          <div className="card-pad stack" style={{ gap: 10 }}>
            {metric.compiled_sql ? (
              <SqlBlock sql={formatSql(metric.compiled_sql)} />
            ) : (
              <span className="muted">지금 스키마로 컴파일되지 않습니다.</span>
            )}
            <details>
              <summary className="faint">AST 보기</summary>
              <pre className="sql">{JSON.stringify(metric.ast, null, 2)}</pre>
            </details>
          </div>
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
                    <tr className="clickable" onClick={() => setOpenIndex(openIndex === i ? null : i)}>
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
                          {h.snapshot.sql ? (
                            <SqlBlock sql={h.snapshot.sql} />
                          ) : (
                            <pre className="sql">{JSON.stringify(h.snapshot, null, 2)}</pre>
                          )}
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
              <code>{metric.tables.join(", ") || "-"}</code>
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
