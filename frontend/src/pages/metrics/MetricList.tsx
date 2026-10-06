import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import * as api from "../../api";
import { Badge, Button, EmptyState, Loading, PageHeader } from "../../components/ui";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/format";
import { brokenReason, definitionLines, KIND_LABEL } from "../../lib/metrics";

interface Loaded {
  datasource: api.Datasource;
  metrics: api.Metric[];
  tables: api.SchemaTable[] | null;
}

export function MetricList() {
  const [rows, setRows] = useState<Loaded[] | null>(null);
  const [params, setParams] = useSearchParams();
  const toast = useToast();
  const navigate = useNavigate();
  const filter = params.get("ds") ?? "";

  useEffect(() => {
    load();
  }, []);

  async function load() {
    try {
      const datasources = await api.listDatasources();
      const loaded = await Promise.all(
        datasources.map(async (datasource) => {
          const metrics = await api.listMetrics(datasource.id);
          // 스키마는 깨진 지표를 가려내는 데만 쓴다. 지표가 없으면 읽을 이유가 없다.
          let tables: api.SchemaTable[] | null = null;
          if (metrics.length > 0 && datasource.synced_at) {
            try {
              tables = (await api.getSchema(datasource.id)).tables;
            } catch {
              tables = null;
            }
          }
          return { datasource, metrics, tables };
        })
      );
      setRows(loaded);
    } catch (err) {
      toast("지표를 불러오지 못했습니다: " + errorMessage(err), "error");
      setRows([]);
    }
  }

  if (rows === null) {
    return (
      <div className="page">
        <Loading />
      </div>
    );
  }

  const visible = filter ? rows.filter((r) => r.datasource.id === filter) : rows;
  const flat = visible.flatMap((r) =>
    r.metrics.map((m) => ({ row: r, metric: m, broken: brokenReason(r.tables, m, r.metrics) }))
  );
  const brokenCount = flat.filter((f) => f.broken).length;

  return (
    <div className="page wide">
      <PageHeader
        title="지표"
        desc="스키마에서 뽑을 수 없는 업무 개념입니다. 질문이 지표에 걸리면 집계식과 고정 필터가 자동으로 적용됩니다."
        actions={
          <Button
            variant="primary"
            onClick={() => navigate(filter ? `/metrics/new?ds=${filter}` : "/metrics/new")}
          >
            + 새 지표
          </Button>
        }
      />

      <div className="row" style={{ marginBottom: 16 }}>
        <select
          className="select"
          style={{ width: 240 }}
          value={filter}
          onChange={(e) => setParams(e.target.value ? { ds: e.target.value } : {})}
        >
          <option value="">전체 데이터소스</option>
          {rows.map((r) => (
            <option key={r.datasource.id} value={r.datasource.id}>
              {r.datasource.name}
            </option>
          ))}
        </select>
        <span className="muted">지표 {flat.length}개</span>
        {brokenCount > 0 && <Badge tone="warn">깨진 지표 {brokenCount}개</Badge>}
        <span className="spacer" />
        <Button size="sm" variant="ghost" onClick={load}>
          새로고침
        </Button>
      </div>

      <div className="card">
        {flat.length === 0 ? (
          <EmptyState
            title="등록된 지표가 없습니다"
            desc={
              rows.length === 0
                ? "먼저 데이터소스를 등록하고 스키마를 동기화하세요."
                : "자주 쓰는 집계나 조회를 지표로 등록해 두면 질문 정확도가 올라갑니다."
            }
            action={
              <Button
                variant="primary"
                onClick={() => navigate(rows.length === 0 ? "/datasources/new" : "/metrics/new")}
              >
                {rows.length === 0 ? "데이터소스 등록" : "지표 등록"}
              </Button>
            }
          />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: "26%" }}>이름</th>
                  <th>종류</th>
                  <th>데이터소스</th>
                  <th>정의</th>
                  <th>상태</th>
                </tr>
              </thead>
              <tbody>
                {flat.map(({ row, metric: m, broken }) => (
                  <tr
                    key={m.id}
                    className="clickable"
                    onClick={() => navigate(`/metrics/${row.datasource.id}/${m.id}`)}
                  >
                    <td>
                      <div className="cell-title">{m.name}</div>
                      <div className="cell-sub">{m.description || "설명 없음"}</div>
                    </td>
                    <td>
                      <Badge tone="primary">{KIND_LABEL[m.kind]}</Badge>
                    </td>
                    <td>{row.datasource.name}</td>
                    <td>
                      <code className="muted">{definitionLines(m).replace(/\n\s*/g, " ")}</code>
                    </td>
                    <td>
                      {broken ? (
                        <span title={broken}>
                          <Badge tone="warn">스키마 불일치</Badge>
                        </span>
                      ) : (
                        <Badge tone="ok">정상</Badge>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
