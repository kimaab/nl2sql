import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import * as api from "../../api";
import { Badge, Button, EmptyState, Loading, PageHeader } from "../../components/ui";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/format";
import { STATUS_LABEL, STATUS_TONE } from "../../lib/metrics";

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

  return (
    <div className="page wide">
      <PageHeader
        title="지표"
        desc="지표는 계산이 필요한 숫자를 SQL 로 등록한 것입니다. 지표가 필요한 질문이면 LLM 이 그 지표의 조회 명세에서 기간·조건만 바꿔 SQL 을 만듭니다."
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
                : "계산이 필요한 숫자(예: 누적값의 하루 증가분)를 SQL 로 등록하세요. 단순 조회는 지표 없이도 답합니다."
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
                      <code>{m.tables.join(", ") || "-"}</code>
                    </td>
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
