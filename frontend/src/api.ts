const API_BASE = "http://localhost:8000/api";

export type Driver = "mysql" | "postgresql" | "oracle";

/** 시스템 = 조회 대상 DB. 질문할 때 먼저 고른다. */
export interface System {
  id: string;
  code: string;
  name: string;
  domain_desc: string;
  driver: Driver;
  host: string;
  port: number;
  db_name: string;
  db_schema: string;
  username: string;
  synced_at: string | null;
  table_count: number;
  metric_count: number;
  active_metric_count: number;
}

export interface SystemInput {
  code: string;
  name: string;
  domain_desc: string;
  driver: Driver;
  host: string;
  port: number;
  db_name: string;
  db_schema: string;
  username: string;
  password: string;
}

export type MetricStatus = "active" | "broken";

/** 지표 = 이름 · 설명 · 같은 말 + SQL. SQL 은 조회 명세(AST)로 옮겨 저장하고, 질문마다 LLM 이 조건만 바꿔 쓴다. */
export interface MetricInput {
  name: string;
  description: string;
  synonyms: string[];
  sql: string;
}

export interface Metric extends MetricInput {
  id: string;
  ast: Record<string, any>;
  /** 저장된 AST 를 지금 스키마로 컴파일한 SQL (깨졌으면 null) */
  compiled_sql: string | null;
  tables: string[];
  status: MetricStatus;
  broken_reason: string | null;
  version: number;
  updated_at: string | null;
}

/** 등록 전 확인: SQL → AST → SQL → 대상 DB 에서 결과 비교. stage 는 실패한 단계 */
export interface MetricCheck {
  ast: Record<string, any> | null;
  sql: string | null;
  tables: string[];
  warnings: string[];
  rows: number | null;
  stage: "parse" | "compile" | "verify" | null;
  error: string | null;
}

export interface MetricHistoryEntry {
  version: number;
  action: "create" | "update" | "delete";
  snapshot: Partial<MetricInput> & { ast?: Record<string, any> };
  created_at: string;
}

export interface SchemaColumn {
  name: string;
  type: string;
  description: string;
}

export interface SchemaTable {
  name: string;
  description: string;
  purpose?: string;
  columns: SchemaColumn[];
}

/** 시각을 포함한 날짜 컬럼. utc_offset_hours 가 있으면 DB 는 UTC 로 저장하고 현지 시각으로 보정한다 */
export interface TimeColumn {
  name: string;
  data_type: string;
  utc_offset_hours: number | null;
}

/** 컬럼과 '계산 필요' 표시. 계산 필요 컬럼을 계산해야 답할 수 있는 질문은 지표가 필요하다 */
export interface ColumnInfo {
  name: string;
  data_type: string;
  comment: string;
  needs_calc: boolean;
}

export interface TableInfo {
  id: string;
  name: string;
  comment: string;
  purpose: string;
  column_count: number;
  metric_count: number;
  time_columns: TimeColumn[];
  columns: ColumnInfo[];
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

export interface AskStep {
  stage: "table" | "metric" | "sql";
  candidates: string[];
  selected: string[];
  reason: string;
  prompt_tokens: number;
  completion_tokens: number;
  elapsed_ms: number;
}

export interface AskResponse {
  id: string | null;
  sql: string | null;
  error: string | null;
  attempts: number;
  clarification: string | null;
  selected_tables: string[];
  selected_metrics: string[];
  /** 지표가 필요한 질문인데 맞는 지표가 없다 — error 에 이유가 있다 */
  needs_metric: boolean;
  steps: AskStep[];
}

export interface BrokenMetric {
  name: string;
  reason: string;
}

export interface SyncResult {
  table_count: number;
  column_count: number;
  tables_added: number;
  tables_changed: number;
  tables_removed: number;
  synced_at: string;
  relation_count: number | null;
  broken_metrics: BrokenMetric[];
}

export interface SyncLog {
  id: number;
  system_id: string;
  system_name: string;
  trigger: "manual" | "auto";
  status: "ok" | "error";
  tables_added: number | null;
  tables_changed: number | null;
  tables_removed: number | null;
  broken_metrics: BrokenMetric[];
  error: string | null;
  started_at: string;
  finished_at: string;
}

export type Feedback = "up" | "down";

export interface HistoryEntry {
  id: string;
  system_id: string;
  system_name: string;
  question: string;
  selected_tables: string[];
  selected_metrics: string[];
  total_tokens: number;
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

// 시스템 · 동기화
export const listSystems = () => request<System[]>("/systems");
export const getSystem = (id: string) => request<System>(`/systems/${id}`);
export const createSystem = (input: SystemInput) => request<System>("/systems", { method: "POST", body: input });
export const updateSystem = (id: string, input: SystemInput) =>
  request<System>(`/systems/${id}`, { method: "PUT", body: input });
export const deleteSystem = (id: string) => request<void>(`/systems/${id}`, { method: "DELETE" });
export const syncSystem = (id: string) => request<SyncResult>(`/systems/${id}/sync`, { method: "POST" });
export const listSyncLogs = (systemId?: string, limit = 100) =>
  request<SyncLog[]>(`/sync-logs${q({ system_id: systemId, limit })}`);
export const getSchema = (id: string) => request<{ tables: SchemaTable[] }>(`/systems/${id}/schema`);
export const listTables = (id: string) => request<TableInfo[]>(`/systems/${id}/tables`);
export const setTablePurpose = (id: string, tableId: string, purpose: string) =>
  request<TableInfo>(`/systems/${id}/tables/${tableId}/purpose`, { method: "PUT", body: { purpose } });
export const setUtcOffset = (id: string, tableId: string, column: string, hours: number | null) =>
  request<TableInfo>(`/systems/${id}/tables/${tableId}/utc-offset`, {
    method: "PUT",
    body: { column, utc_offset_hours: hours },
  });
export const setNeedsCalc = (id: string, tableId: string, column: string, needsCalc: boolean) =>
  request<TableInfo>(`/systems/${id}/tables/${tableId}/needs-calc`, {
    method: "PUT",
    body: { column, needs_calc: needsCalc },
  });

// 관계
export const listRelations = (sysId: string) => request<Relation[]>(`/systems/${sysId}/relations`);
export const createRelation = (sysId: string, input: RelationInput) =>
  request<Relation>(`/systems/${sysId}/relations`, { method: "POST", body: input });
export const deleteRelation = (sysId: string, id: string) =>
  request<void>(`/systems/${sysId}/relations/${id}`, { method: "DELETE" });

// 지표
export const listMetrics = (sysId: string) => request<Metric[]>(`/systems/${sysId}/metrics`);
export const createMetric = (sysId: string, input: MetricInput) =>
  request<Metric>(`/systems/${sysId}/metrics`, { method: "POST", body: input });
export const updateMetric = (sysId: string, id: string, input: MetricInput) =>
  request<Metric>(`/systems/${sysId}/metrics/${id}`, { method: "PUT", body: input });
/** 저장하지 않고 등록 절차만 밟아 본다 (SQL → AST → SQL → 결과 비교) */
export const checkMetric = (sysId: string, sql: string) =>
  request<MetricCheck>(`/systems/${sysId}/metrics/check`, { method: "POST", body: { sql } });
export const deleteMetric = (sysId: string, id: string) =>
  request<void>(`/systems/${sysId}/metrics/${id}`, { method: "DELETE" });
export const metricHistory = (sysId: string, id: string) =>
  request<MetricHistoryEntry[]>(`/systems/${sysId}/metrics/${id}/history`);

// 질문
export const ask = (systemId: string, question: string) =>
  request<AskResponse>("/ask", { method: "POST", body: { system_id: systemId, question } });

// 질문 기록
export const listHistory = (params: { systemId?: string; favorite?: boolean; feedback?: Feedback; limit?: number; offset?: number }) =>
  request<HistoryEntry[]>(
    `/history${q({ system_id: params.systemId, favorite: params.favorite, feedback: params.feedback, limit: params.limit, offset: params.offset })}`
  );
export const historyTrace = (id: string) => request<AskStep[]>(`/history/${id}/trace`);
export const patchHistory = (id: string, patch: HistoryPatch) =>
  request<HistoryEntry>(`/history/${id}`, { method: "PATCH", body: patch });
export const deleteHistory = (id: string) => request<void>(`/history/${id}`, { method: "DELETE" });
export const exportHistory = (sysId: string) =>
  request<{ system: string; cases: unknown[] }>(`/history/export${q({ system_id: sysId })}`);
