import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import * as api from "../../api";
import { Badge, Button, EmptyState, Loading, PageHeader } from "../../components/ui";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/format";
import { definitionLines, KIND_LABEL, STATUS_LABEL, STATUS_TONE } from "../../lib/metrics";

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
          <span title="승인된 예시 질문이 있어야 질문에 쓰입니다">
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
                : "자주 쓰는 집계나 조회를 지표로 등록해 두면 질문 정확도가 올라갑니다."
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
                  <th>종류</th>
                  <th>시스템</th>
                  <th>정의</th>
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
                    <td>
                      <Badge tone="primary">{KIND_LABEL[m.kind]}</Badge>
                    </td>
                    <td>{row.system.name}</td>
                    <td>
                      <code className="muted">{definitionLines(m).replace(/\n\s*/g, " ")}</code>
                    </td>
                    <td>
                      <span title={m.broken_reason ?? (m.status === "draft" ? "승인된 예시 질문이 있어야 질문에 쓰입니다" : "")}>
                        <Badge tone={STATUS_TONE[m.status]}>{STATUS_LABEL[m.status]}</Badge>
                      </span>
                      {m.draft_example_count > 0 && <div className="cell-sub">예시 초안 {m.draft_example_count}개</div>}
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
