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

export interface Metric {
  id: string;
  name: string;
  description: string;
  kind: MetricKind;
  table_name: string;
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
