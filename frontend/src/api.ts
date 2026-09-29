const API_BASE = "http://localhost:8000/api";

export interface Datasource {
  id: string;
  name: string;
  description: string;
  driver: "mysql" | "postgresql" | "oracle";
  host: string;
  port: number;
  db_name: string;
  db_schema: string;
  username: string;
  synced_at: string | null;
  table_count: number;
  metric_count: number;
}

export interface DatasourceInput {
  name: string;
  description: string;
  driver: "mysql" | "postgresql" | "oracle";
  host: string;
  port: number;
  db_name: string;
  db_schema: string;
  username: string;
  password: string;
}

export type MetricKind = "aggregate" | "projection";

export interface JoinOn {
  left: string;
  right: string;
}

export interface JoinSpec {
  table: string;
  type: "inner" | "left";
  on: JoinOn[];
}

export interface Metric {
  id: string;
  name: string;
  description: string;
  kind: MetricKind;
  table_name: string;
  joins: JoinSpec[];
  agg_field: string | null;
  agg_function: string | null;
  select_columns: string[];
  fixed_filters: Record<string, any>[];
}

export interface MetricInput {
  name: string;
  description: string;
  kind: MetricKind;
  table_name: string;
  joins: JoinSpec[];
  agg_field: string | null;
  agg_function: string | null;
  select_columns: string[];
  fixed_filters: Record<string, any>[];
}

export interface SchemaColumn {
  name: string;
  type: string;
  description: string;
}

export interface SchemaTable {
  name: string;
  description: string;
  columns: SchemaColumn[];
}

export interface AskResponse {
  sql: string | null;
  error: string | null;
  attempts: number;
}

export interface SyncResult {
  table_count: number;
  column_count: number;
  synced_at: string;
}

// 데이터소스 API
export async function listDatasources(): Promise<Datasource[]> {
  const res = await fetch(`${API_BASE}/datasources`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function getDatasource(id: string): Promise<Datasource> {
  const res = await fetch(`${API_BASE}/datasources/${id}`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function createDatasource(
  input: DatasourceInput
): Promise<Datasource> {
  const res = await fetch(`${API_BASE}/datasources`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function updateDatasource(
  id: string,
  input: DatasourceInput
): Promise<Datasource> {
  const res = await fetch(`${API_BASE}/datasources/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function deleteDatasource(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/datasources/${id}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(await res.text());
}

export async function syncDatasource(id: string): Promise<SyncResult> {
  const res = await fetch(`${API_BASE}/datasources/${id}/sync`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function getSchema(id: string): Promise<{ tables: SchemaTable[] }> {
  const res = await fetch(`${API_BASE}/datasources/${id}/schema`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

// 지표 API
export async function listMetrics(datasourceId: string): Promise<Metric[]> {
  const res = await fetch(`${API_BASE}/datasources/${datasourceId}/metrics`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function createMetric(
  datasourceId: string,
  input: MetricInput
): Promise<Metric> {
  const res = await fetch(`${API_BASE}/datasources/${datasourceId}/metrics`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function deleteMetric(
  datasourceId: string,
  metricId: string
): Promise<void> {
  const res = await fetch(
    `${API_BASE}/datasources/${datasourceId}/metrics/${metricId}`,
    { method: "DELETE" }
  );
  if (!res.ok) throw new Error(await res.text());
}

// 질문 API
export async function ask(
  datasourceId: string,
  question: string
): Promise<AskResponse> {
  const res = await fetch(`${API_BASE}/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      datasource_id: datasourceId,
      question: question,
    }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

/** " INNER JOIN T ON a = b ..." — 조인이 없으면 빈 문자열. */
export function joinClause(m: { joins?: JoinSpec[] }): string {
  return (m.joins ?? [])
    .map(
      (j) =>
        ` ${j.type === "left" ? "LEFT" : "INNER"} JOIN ${j.table} ON ` +
        j.on.map((p) => `${p.left} = ${p.right}`).join(" AND ")
    )
    .join("");
}

/** 동기화로 테이블·컬럼이 사라진 지표를 가려낸다. 백엔드 contract._broken_reason 과 같은 규칙. */
export function metricBrokenReason(tables: SchemaTable[], m: Metric): string | null {
  const find = (name: string) => tables.find((t) => t.name.toLowerCase() === name.toLowerCase());
  const scope: SchemaTable[] = [];
  for (const name of [m.table_name, ...(m.joins ?? []).map((j) => j.table)]) {
    const t = find(name);
    if (!t) return `테이블 ${name}이(가) 스키마에 없습니다`;
    scope.push(t);
  }
  const has = (t: SchemaTable, c: string) => t.columns.some((x) => x.name.toLowerCase() === c.toLowerCase());
  const resolves = (ref: string | null | undefined) => {
    if (!ref) return false;
    const dot = ref.indexOf(".");
    if (dot >= 0) {
      const t = scope.find((x) => x.name.toLowerCase() === ref.slice(0, dot).toLowerCase());
      return !!t && has(t, ref.slice(dot + 1));
    }
    return has(scope[0], ref) || scope.slice(1).filter((t) => has(t, ref)).length === 1;
  };

  const onRefs = (m.joins ?? []).flatMap((j) => j.on.flatMap((p) => [p.left, p.right]));
  const badOn = onRefs.filter((r) => !resolves(r));
  if (badOn.length > 0) return `조인 조건 컬럼이 스키마에 없습니다: ${badOn.join(", ")}`;

  if (m.kind === "projection") {
    if (m.select_columns.length === 0) return "조회 컬럼이 비어 있습니다";
    const gone = m.select_columns.filter((c) => !resolves(c));
    if (gone.length > 0) return `조회 컬럼이 스키마에 없습니다: ${gone.join(", ")}`;
  } else if (m.agg_field !== "*" && !resolves(m.agg_field)) {
    return `집계 컬럼 ${m.agg_field}이(가) 스키마에 없습니다`;
  }

  const missing = m.fixed_filters.map((f) => f.field).filter((f) => f && !resolves(f));
  if (missing.length > 0) return `고정 필터 컬럼이 스키마에 없습니다: ${missing.join(", ")}`;
  return null;
}
