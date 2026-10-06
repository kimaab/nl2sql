import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import * as api from "../../api";
import { Alert, Badge, Button, EmptyState, Loading, PageHeader, SqlBlock } from "../../components/ui";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/format";
import { brokenReason, definitionLines, OPERATORS, SOURCES } from "../../lib/metrics";

export function MetricDetail() {
  const { dsId = "", metricId = "" } = useParams();
  const [datasource, setDatasource] = useState<api.Datasource | null>(null);
  const [metric, setMetric] = useState<api.Metric | null | undefined>(undefined);
  const [tables, setTables] = useState<api.SchemaTable[] | null>(null);
  const toast = useToast();
  const navigate = useNavigate();

  useEffect(() => {
    (async () => {
      try {
        const [ds, list] = await Promise.all([api.getDatasource(dsId), api.listMetrics(dsId)]);
        setDatasource(ds);
        setMetric(list.find((m) => m.id === metricId) ?? null);
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
    if (!metric || !confirm(`지표 '${metric.name}'을(를) 삭제하시겠습니까?`)) return;
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

  const broken = brokenReason(tables, metric);

  return (
    <div className="page narrow">
      <PageHeader
        back={{ to: `/metrics?ds=${dsId}`, label: "지표 목록" }}
        title={metric.name}
        desc={metric.description || "설명이 없습니다 — 질문이 이 지표에 잘 걸리지 않습니다."}
        actions={
          <Button variant="danger" onClick={handleDelete}>
            삭제
          </Button>
        }
      />

      <div className="stack">
        {broken && <Alert tone="warn">스키마와 맞지 않아 질문에 쓰이지 않습니다 — {broken}</Alert>}

        <div className="card">
          <div className="card-head">
            <span className="card-title">정의</span>
            <Badge tone="primary">{metric.kind === "projection" ? "조회" : "집계"}</Badge>
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
                      <td>{src === "question" ? <span className="faint">질문에서</span> : <code>{String(f.value)}</code>}</td>
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

        <div className="card card-pad">
          <dl className="kv" style={{ margin: 0 }}>
            <dt>데이터소스</dt>
            <dd>{datasource?.name}</dd>
            <dt>테이블</dt>
            <dd>
              <code>{metric.table_name}</code>
            </dd>
          </dl>
        </div>
      </div>
    </div>
  );
}
