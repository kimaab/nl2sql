import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import * as api from "../../api";
import { Badge, Button, EmptyState, Loading, PageHeader } from "../../components/ui";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/format";
import { dimensionLabel, STATUS_LABEL, STATUS_TONE } from "../../lib/metrics";

interface Loaded {
  system: api.System;
  metrics: api.Metric[];
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
      const systems = await api.listSystems();
      const loaded = await Promise.all(
        systems.map(async (system) => ({ system, metrics: await api.listMetrics(system.id) }))
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

  const visible = filter ? rows.filter((r) => r.system.id === filter) : rows;
  const flat = visible.flatMap((r) => r.metrics.map((m) => ({ row: r, metric: m })));
  const brokenCount = flat.filter((f) => f.metric.status === "broken").length;
  const draftCount = flat.filter((f) => f.metric.status === "draft").length;

  return (
    <div className="page wide">
      <PageHeader
        title="지표"
        desc="지표는 테이블 · 디멘젼 · 허용 계산식입니다. 질문이 지표에 걸리면 그 테이블과 고정 필터·계산식 안에서 SQL 이 만들어집니다."
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
          <option value="">전체 시스템</option>
          {rows.map((r) => (
            <option key={r.system.id} value={r.system.id}>
              {r.system.name}
            </option>
          ))}
        </select>
        <span className="muted">지표 {flat.length}개</span>
        {brokenCount > 0 && <Badge tone="danger">깨진 지표 {brokenCount}개</Badge>}
        {draftCount > 0 && (
          <span title="저장하면 사용 중으로 바뀝니다">
            <Badge tone="warn">검수 대기 {draftCount}개</Badge>
          </span>
        )}
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
                ? "먼저 시스템을 등록하고 스키마를 동기화하세요."
                : "질문에 쓸 테이블을 지표로 등록하세요. 지표가 없는 테이블은 질문에 쓰이지 않습니다."
            }
            action={
              <Button
                variant="primary"
                onClick={() => navigate(rows.length === 0 ? "/systems/new" : "/metrics/new")}
              >
                {rows.length === 0 ? "시스템 등록" : "지표 등록"}
              </Button>
            }
          />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: "26%" }}>이름</th>
                  <th>시스템</th>
                  <th>테이블</th>
                  <th>디멘젼</th>
                  <th>계산식</th>
                  <th>상태</th>
                </tr>
              </thead>
              <tbody>
                {flat.map(({ row, metric: m }) => (
                  <tr
                    key={m.id}
                    className="clickable"
                    onClick={() => navigate(`/metrics/${row.system.id}/${m.id}`)}
                  >
                    <td>
                      <div className="cell-title">{m.name}</div>
                      <div className="cell-sub">{m.description || "설명 없음"}</div>
                    </td>
                    <td>{row.system.name}</td>
                    <td>
                      <code>{[m.table_name, ...m.joins.map((j) => j.table)].join(", ")}</code>
                    </td>
                    <td className="muted">{m.dimensions.map(dimensionLabel).join(", ") || "없음"}</td>
                    <td className="muted">{m.formulas.join(", ") || "없음"}</td>
                    <td>
                      <span title={m.broken_reason ?? ""}>
                        <Badge tone={STATUS_TONE[m.status]}>{STATUS_LABEL[m.status]}</Badge>
                      </span>
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
