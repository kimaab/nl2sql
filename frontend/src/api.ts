const API_BASE = "http://localhost:8000/api";

export type Driver = "mysql" | "postgresql" | "oracle";

export interface Datasource {
  id: string;
  name: string;
  description: string;
  driver: Driver;
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
  driver: Driver;
  host: string;
  port: number;
  db_name: string;
  db_schema: string;
  username: string;
  password: string;
}

export type MetricKind = "aggregate" | "projection" | "derived";

export interface MetricJoin {
  table: string;
  type: "inner" | "left";
  on: { left: string; right: string }[];
}

export interface MetricExample {
  question: string;
  ast: Record<string, any> | null;
}

export interface MetricInput {
  name: string;
  description: string;
  kind: MetricKind;
  table_name: string;
  joins: MetricJoin[];
  agg_field: string | null;
  agg_function: string | null;
  select_columns: string[];
  expression: string | null;
  fixed_filters: Record<string, any>[];
  examples: MetricExample[];
}

export interface Metric extends MetricInput {
  id: string;
  version: number;
  updated_at: string | null;
}

export interface MetricHistoryEntry {
  version: number;
  action: "create" | "update" | "delete";
  snapshot: Partial<MetricInput>;
  created_at: string;
}

export interface CodeValue {
  code: string;
  label: string;
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

export interface RelationInput {
  left_table: string;
  left_column: string;
  right_table: string;
  right_column: string;
  constraint_name: string;
}

export interface Relation extends RelationInput {
  id: string;
  source: "fk" | "manual";
}

export interface Annotation {
  table_name: string;
  column_name: string;
  synonyms: string[];
  codes: CodeValue[];
}

export interface AskResponse {
  id: string | null;
  sql: string | null;
  error: string | null;
  attempts: number;
  clarification: string | null;
}

export interface CompileResponse {
  sql: string | null;
  error: string | null;
}

export interface BrokenMetric {
  name: string;
  reason: string;
}

export interface SyncResult {
  table_count: number;
  column_count: number;
  synced_at: string;
  relation_count: number | null;
  broken_metrics: BrokenMetric[];
}

export interface SyncLog {
  id: number;
  datasource_id: string;
  datasource_name: string;
  trigger: "manual" | "auto";
  status: "ok" | "error";
  table_count: number | null;
  column_count: number | null;
  relation_count: number | null;
  broken_metrics: BrokenMetric[];
  error: string | null;
  started_at: string;
  finished_at: string;
}

export type Feedback = "up" | "down";

export interface HistoryEntry {
  id: string;
  datasource_id: string;
  datasource_name: string;
  question: string;
  sql: string | null;
  ast: Record<string, any> | null;
  error: string | null;
  clarification: string | null;
  attempts: number;
  elapsed_ms: number;
  favorite: boolean;
  feedback: Feedback | null;
  feedback_note: string;
  created_at: string;
}

export interface HistoryPatch {
  favorite?: boolean;
  feedback?: Feedback | "";
  feedback_note?: string;
}

async function request<T>(path: string, init?: { method?: string; body?: unknown }): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: init?.method ?? "GET",
    headers: init?.body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: init?.body !== undefined ? JSON.stringify(init.body) : undefined,
  });
  if (!res.ok) throw new Error(await res.text());
  return (res.status === 204 ? undefined : await res.json()) as T;
}

const q = (params: Record<string, string | number | boolean | null | undefined>) => {
  const s = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== "") s.set(k, String(v));
  const text = s.toString();
  return text ? `?${text}` : "";
};

// 데이터소스
export const listDatasources = () => request<Datasource[]>("/datasources");
export const getDatasource = (id: string) => request<Datasource>(`/datasources/${id}`);
export const createDatasource = (input: DatasourceInput) =>
  request<Datasource>("/datasources", { method: "POST", body: input });
export const updateDatasource = (id: string, input: DatasourceInput) =>
  request<Datasource>(`/datasources/${id}`, { method: "PUT", body: input });
export const deleteDatasource = (id: string) => request<void>(`/datasources/${id}`, { method: "DELETE" });
export const syncDatasource = (id: string) => request<SyncResult>(`/datasources/${id}/sync`, { method: "POST" });
export const listSyncLogs = (datasourceId?: string, limit = 100) =>
  request<SyncLog[]>(`/sync-logs${q({ datasource_id: datasourceId, limit })}`);
export const getSchema = (id: string) => request<{ tables: SchemaTable[] }>(`/datasources/${id}/schema`);

// 관계
export const listRelations = (dsId: string) => request<Relation[]>(`/datasources/${dsId}/relations`);
export const createRelation = (dsId: string, input: RelationInput) =>
  request<Relation>(`/datasources/${dsId}/relations`, { method: "POST", body: input });
export const deleteRelation = (dsId: string, id: string) =>
  request<void>(`/datasources/${dsId}/relations/${id}`, { method: "DELETE" });

// 용어·코드 사전
export const listAnnotations = (dsId: string) => request<Annotation[]>(`/datasources/${dsId}/annotations`);
export const putAnnotation = (dsId: string, table: string, column: string, body: { synonyms: string[]; codes: CodeValue[] }) =>
  request<Annotation>(
    `/datasources/${dsId}/annotations/${encodeURIComponent(table)}/${encodeURIComponent(column)}`,
    { method: "PUT", body }
  );

// 지표
export const listMetrics = (dsId: string) => request<Metric[]>(`/datasources/${dsId}/metrics`);
export const createMetric = (dsId: string, input: MetricInput) =>
  request<Metric>(`/datasources/${dsId}/metrics`, { method: "POST", body: input });
export const updateMetric = (dsId: string, id: string, input: MetricInput) =>
  request<Metric>(`/datasources/${dsId}/metrics/${id}`, { method: "PUT", body: input });
export const deleteMetric = (dsId: string, id: string) =>
  request<void>(`/datasources/${dsId}/metrics/${id}`, { method: "DELETE" });
export const metricHistory = (dsId: string, id: string) =>
  request<MetricHistoryEntry[]>(`/datasources/${dsId}/metrics/${id}/history`);
export const compileAst = (dsId: string, ast: Record<string, any>) =>
  request<CompileResponse>(`/datasources/${dsId}/compile`, { method: "POST", body: { ast } });

// 질문
export const ask = (datasourceId: string, question: string) =>
  request<AskResponse>("/ask", { method: "POST", body: { datasource_id: datasourceId, question } });

// 질문 기록
export const listHistory = (params: { datasourceId?: string; favorite?: boolean; feedback?: Feedback; limit?: number; offset?: number }) =>
  request<HistoryEntry[]>(
    `/history${q({ datasource_id: params.datasourceId, favorite: params.favorite, feedback: params.feedback, limit: params.limit, offset: params.offset })}`
  );
export const patchHistory = (id: string, patch: HistoryPatch) =>
  request<HistoryEntry>(`/history/${id}`, { method: "PATCH", body: patch });
export const deleteHistory = (id: string) => request<void>(`/history/${id}`, { method: "DELETE" });
export const exportHistory = (dsId: string) =>
  request<{ datasource: string; cases: unknown[] }>(`/history/export${q({ datasource_id: dsId })}`);
