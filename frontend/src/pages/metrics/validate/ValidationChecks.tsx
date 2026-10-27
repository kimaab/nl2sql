import * as api from "../../../api";
import { Alert, Badge, SqlBlock } from "../../../components/ui";
import { formatSql } from "../../../lib/metrics";
import { VERDICT_LABEL, VERDICT_TONE } from "../../../lib/metricAuto";

/** 지표 하나의 점검 목록. 점검마다 판정, 이유, 근거(SQL, 결과 표본, LLM 지적)를 보여 준다 */
export function ValidationChecks({ item }: { item: api.ValidationItem }) {
  return (
    <div className="stack" style={{ gap: 10, padding: "8px 0" }}>
      <div className="faint">
        v{item.metric_version} 기준, {(item.elapsed_ms / 1000).toFixed(1)}초
      </div>
      {item.checks.map((c) => (
        <div key={c.key} className="stack" style={{ gap: 6 }}>
          <div className="row" style={{ gap: 8, alignItems: "baseline" }}>
            <Badge tone={VERDICT_TONE[c.status]}>{VERDICT_LABEL[c.status]}</Badge>
            <b style={{ minWidth: 80 }}>{c.label}</b>
            <span className={c.status === "skip" ? "faint" : ""}>{c.message}</span>
          </div>
          <CheckDetail check={c} />
        </div>
      ))}
    </div>
  );
}

function CheckDetail({ check }: { check: api.ValidationCheck }) {
  const d = check.detail ?? {};
  switch (check.key) {
    case "compile":
      return d.sql ? (
        <details style={{ marginLeft: 16 }}>
          <summary className="faint">AST 로 만든 SQL</summary>
          <SqlBlock sql={formatSql(d.sql)} />
        </details>
      ) : null;
    case "roundtrip":
      return (
        <div style={{ marginLeft: 16 }} className="stack">
          {(d.warnings ?? []).map((w: string, i: number) => (
            <Alert key={i} tone="warn">
              {w}
            </Alert>
          ))}
          {d.current_ast && (
            <details>
              <summary className="faint">저장된 AST 와 지금 옮긴 AST 비교</summary>
              <div className="row" style={{ alignItems: "flex-start", gap: 8 }}>
                <pre className="sql" style={{ flex: 1 }}>
                  {JSON.stringify(d.stored_ast, null, 2)}
                </pre>
                <pre className="sql" style={{ flex: 1 }}>
                  {JSON.stringify(d.current_ast, null, 2)}
                </pre>
              </div>
            </details>
          )}
        </div>
      );
    case "result":
      return d.columns?.length ? (
        <details style={{ marginLeft: 16 }}>
          <summary className="faint">
            결과 표본 {d.rows.length}행 (전체 {Number(d.total).toLocaleString()}행)
          </summary>
          <div className="table-wrap">
            <table className="table sample-table">
              <thead>
                <tr>
                  {d.columns.map((c: string) => (
                    <th key={c}>
                      {c}
                      {d.nulls?.[c] ? <span className="faint"> (NULL {d.nulls[c].toLocaleString()})</span> : null}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {d.rows.map((r: string[], i: number) => (
                  <tr key={i}>
                    {r.map((v, j) => (
                      <td key={j}>{v}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      ) : null;
    case "llm":
      return d.issues?.length ? (
        <div style={{ marginLeft: 16 }} className="stack">
          {d.issues.map((issue: { severity: string; message: string }, i: number) => (
            <Alert key={i} tone={issue.severity === "fail" ? "danger" : "warn"}>
              {issue.message}
            </Alert>
          ))}
        </div>
      ) : null;
    default:
      return null;
  }
}
