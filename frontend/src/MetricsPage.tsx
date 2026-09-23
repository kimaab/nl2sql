import { useState, useEffect } from "react";
import * as api from "./api";

interface Loaded {
  datasource: api.Datasource;
  metrics: api.Metric[];
  tables: api.SchemaTable[] | null;
}

export function MetricsPage() {
  const [rows, setRows] = useState<Loaded[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState("");

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
      alert("지표를 불러오지 못했습니다: " + err);
    } finally {
      setLoading(false);
    }
  }

  /** 동기화로 테이블·컬럼이 사라진 지표는 질문에 쓰이지 않는다. */
  function brokenReason(row: Loaded, m: api.Metric): string | null {
    if (!row.tables) return null;
    const table = row.tables.find((t) => t.name === m.table_name);
    if (!table) return `테이블 ${m.table_name}이(가) 스키마에 없습니다`;
    const has = (name: string) => table.columns.some((c) => c.name === name);

    if (m.kind === "projection") {
      if (m.select_columns.length === 0) return "조회 컬럼이 비어 있습니다";
      const gone = m.select_columns.filter((c) => !has(c));
      if (gone.length > 0) return `조회 컬럼이 스키마에 없습니다: ${gone.join(", ")}`;
    } else if (m.agg_field !== "*" && !has(m.agg_field ?? "")) {
      return `집계 컬럼 ${m.table_name}.${m.agg_field}이(가) 스키마에 없습니다`;
    }

    const missing = m.fixed_filters.map((f) => f.field).filter((f) => f && !has(f));
    if (missing.length > 0) {
      return `고정 필터 컬럼이 스키마에 없습니다: ${missing.join(", ")}`;
    }
    return null;
  }

  function definitionOf(m: api.Metric): string {
    const where = m.fixed_filters
      .map((f) => {
        const src = f.source ?? "literal";
        if (src === "question") return `${f.field} ${f.operator} <질문에서>`;
        if (src === "relative") return `${f.field} ${f.operator} <오늘 ${f.value}>`;
        return `${f.field} ${f.operator} ${JSON.stringify(f.value)}`;
      })
      .join(" AND ");
    const select =
      m.kind === "projection"
        ? m.select_columns.join(", ")
        : `${m.agg_function}(${m.agg_field})`;
    return `SELECT ${select} FROM ${m.table_name}` + (where ? ` WHERE ${where}` : "");
  }

  if (loading) return <div style={{ padding: "20px" }}>불러오는 중...</div>;

  const visible = filter ? rows.filter((r) => r.datasource.id === filter) : rows;
  const total = visible.reduce((n, r) => n + r.metrics.length, 0);
  const brokenCount = visible.reduce(
    (n, r) => n + r.metrics.filter((m) => brokenReason(r, m)).length,
    0
  );

  return (
    <div style={{ padding: "20px" }}>
      <h2>등록된 지표</h2>
      <p style={{ color: "#666", fontSize: "0.9em", marginBottom: "15px" }}>
        지표는 스키마에서 뽑을 수 없는 업무 개념입니다. 질문이 지표에 걸리면 집계식과
        고정 필터가 자동으로 적용됩니다. 추가·삭제는 데이터소스 화면에서 합니다.
      </p>

      <div style={{ display: "flex", alignItems: "center", gap: "10px", marginBottom: "20px" }}>
        <label>데이터소스</label>
        <select value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="">전체</option>
          {rows.map((r) => (
            <option key={r.datasource.id} value={r.datasource.id}>
              {r.datasource.name}
            </option>
          ))}
        </select>
        <span style={{ color: "#666" }}>
          지표 {total}개
          {brokenCount > 0 && (
            <strong style={{ color: "#e65100" }}> · 깨진 지표 {brokenCount}개</strong>
          )}
        </span>
        <button onClick={load}>새로고침</button>
      </div>

      {visible.length === 0 && <p>등록된 데이터소스가 없습니다.</p>}

      {visible.map((row) => (
        <div
          key={row.datasource.id}
          style={{ marginBottom: "20px", border: "1px solid #ddd", background: "white" }}
        >
          <div style={{ padding: "12px 15px", borderBottom: "1px solid #eee", background: "#fafafa" }}>
            <strong>{row.datasource.name}</strong>{" "}
            <span style={{ color: "#666" }}>
              {row.datasource.driver} · 지표 {row.metrics.length}개
            </span>
            {!row.datasource.synced_at && (
              <span style={{ color: "#c62828", marginLeft: "8px" }}>
                스키마 없음 — 조회 불가
              </span>
            )}
          </div>

          {row.metrics.length === 0 ? (
            <p style={{ padding: "15px", color: "#666", margin: 0 }}>
              등록된 지표가 없습니다.
            </p>
          ) : (
            <table style={{ boxShadow: "none" }}>
              <thead>
                <tr>
                  <th style={{ width: "18%" }}>이름</th>
                  <th style={{ width: "8%" }}>종류</th>
                  <th style={{ width: "26%" }}>설명</th>
                  <th>정의</th>
                </tr>
              </thead>
              <tbody>
                {row.metrics.map((m) => {
                  const broken = brokenReason(row, m);
                  return (
                    <tr key={m.id} style={broken ? { background: "#fff3e0" } : undefined}>
                      <td>
                        <strong>{m.name}</strong>
                      </td>
                      <td style={{ color: "#555", whiteSpace: "nowrap" }}>
                        {m.kind === "projection" ? "조회" : "집계"}
                      </td>
                      <td style={{ color: m.description ? "#333" : "#999" }}>
                        {m.description || "(설명 없음 — 질문이 이 지표에 잘 걸리지 않습니다)"}
                      </td>
                      <td>
                        <code style={{ fontSize: "0.85em" }}>{definitionOf(m)}</code>
                        {broken && (
                          <div style={{ color: "#e65100", marginTop: "4px" }}>
                            스키마와 맞지 않음 — {broken}
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      ))}
    </div>
  );
}
