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

export type MetricKind = "aggregate" | "projection" | "derived";
export type MetricStatus = "draft" | "active" | "broken" | "retired";

export interface MetricJoin {
  table: string;
  type: "inner" | "left";
  on: { left: string; right: string }[];
}

/** 시계열 집계(DELTA_SUM, CHANGE_COUNT)의 행 순서 */
export interface SeriesSpec {
  partition_by: string;
  order_by: string;
  baseline: string | null;
  max_step?: number | null;
}

export interface MetricExample {
  question: string;
  ast: Record<string, any> | null;
  origin?: "llm" | "human" | "feedback";
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
  series: SeriesSpec | null;
  fixed_filters: Record<string, any>[];
  examples: MetricExample[];
  synonyms: string[];
}

export interface Metric extends MetricInput {
  id: string;
  status: MetricStatus;
  broken_reason: string | null;
  draft_example_count: number;
  version: number;
  updated_at: string | null;
}

export interface MetricHistoryEntry {
  version: number;
  action: "create" | "update" | "delete" | "retire";
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
  purpose?: string;
  columns: SchemaColumn[];
}

export interface TableInfo {
  id: string;
  name: string;
  comment: string;
  purpose: string;
  card: string;
  card_line: string;
  card_status: "none" | "draft" | "approved";
  column_count: number;
  metric_count: number;
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

export interface GlossaryInput {
  term: string;
  synonyms: string[];
  meaning: string;
  maps_to: Record<string, string>[];
  status: "draft" | "approved";
}

export interface GlossaryTerm extends GlossaryInput {
  id: number;
  system_id: string | null;
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

export interface EnrichStatus {
  queued: number;
  running: number;
  failed: number;
  draft_cards: number;
  draft_examples: number;
  draft_eval_cases: number;
}

export type ReviewKind = "table_card" | "metric_example" | "eval_case";

export interface ReviewItem {
  kind: ReviewKind;
  id: string;
  target_name: string;
  context: string;
  draft: string;
  draft_line: string;
}

export interface ReviewDecision {
  action: "approve" | "reject";
  note?: string;
  card?: string;
  card_line?: string;
  question?: string;
}

export interface EvalRun {
  id: string;
  label: string;
  config: Record<string, any>;
  case_count: number;
  table_recall: number | null;
  metric_accuracy: number | null;
  sql_accuracy: number | null;
  avg_tokens: number | null;
  avg_elapsed_ms: number | null;
  started_at: string;
  finished_at: string | null;
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

// 관계
export const listRelations = (sysId: string) => request<Relation[]>(`/systems/${sysId}/relations`);
export const createRelation = (sysId: string, input: RelationInput) =>
  request<Relation>(`/systems/${sysId}/relations`, { method: "POST", body: input });
export const deleteRelation = (sysId: string, id: string) =>
  request<void>(`/systems/${sysId}/relations/${id}`, { method: "DELETE" });

// 컬럼 사전 (동의어 · 코드값)
export const listAnnotations = (sysId: string) => request<Annotation[]>(`/systems/${sysId}/annotations`);
export const putAnnotation = (sysId: string, table: string, column: string, body: { synonyms: string[]; codes: CodeValue[] }) =>
  request<Annotation>(
    `/systems/${sysId}/annotations/${encodeURIComponent(table)}/${encodeURIComponent(column)}`,
    { method: "PUT", body }
  );

// 업무 용어 (sysId 가 없으면 전사 공통)
const glossaryBase = (sysId: string | null) => (sysId ? `/systems/${sysId}/glossary` : "/glossary");
export const listGlossary = (sysId: string | null) => request<GlossaryTerm[]>(glossaryBase(sysId));
export const createGlossary = (sysId: string | null, input: GlossaryInput) =>
  request<GlossaryTerm>(glossaryBase(sysId), { method: "POST", body: input });
export const updateGlossary = (sysId: string | null, id: number, input: GlossaryInput) =>
  request<GlossaryTerm>(`${glossaryBase(sysId)}/${id}`, { method: "PUT", body: input });
export const deleteGlossary = (sysId: string | null, id: number) =>
  request<void>(`${glossaryBase(sysId)}/${id}`, { method: "DELETE" });

// 지표
export const listMetrics = (sysId: string) => request<Metric[]>(`/systems/${sysId}/metrics`);
export const createMetric = (sysId: string, input: MetricInput) =>
  request<Metric>(`/systems/${sysId}/metrics`, { method: "POST", body: input });
export const updateMetric = (sysId: string, id: string, input: MetricInput) =>
  request<Metric>(`/systems/${sysId}/metrics/${id}`, { method: "PUT", body: input });
export const deleteMetric = (sysId: string, id: string) =>
  request<void>(`/systems/${sysId}/metrics/${id}`, { method: "DELETE" });
export const metricHistory = (sysId: string, id: string) =>
  request<MetricHistoryEntry[]>(`/systems/${sysId}/metrics/${id}/history`);

// 보강 · 검수 · 평가
export const enrichStatus = (sysId: string) => request<EnrichStatus>(`/systems/${sysId}/enrich`);
export const startEnrich = (sysId: string, enqueueAll: boolean) =>
  request<EnrichStatus>(`/systems/${sysId}/enrich`, { method: "POST", body: { enqueue_all: enqueueAll } });
export const listReview = (sysId: string) => request<ReviewItem[]>(`/systems/${sysId}/review`);
export const decideReview = (sysId: string, kind: ReviewKind, id: string, decision: ReviewDecision) =>
  request<void>(`/systems/${sysId}/review/${kind}/${encodeURIComponent(id)}`, { method: "POST", body: decision });
export const listEvalRuns = (sysId: string) => request<EvalRun[]>(`/systems/${sysId}/eval-runs`);

// 질문
export const ask = (systemId: string, question: string) =>
  request<AskResponse>("/ask", { method: "POST", body: { system_id: systemId, question } });

// 질문 기록
export const listHistory = (params: { systemId?: string; favorite?: boolean; feedback?: Feedback; limit?: number; offset?: number }) =>
  request<HistoryEntry[]>(
    `/history${q({ system_id: params.systemId, favorite: params.favorite, feedback: params.feedback, limit: params.limit, offset: params.offset })}`
  );
export const historyTrace = (id: string) => request<AskStep[]>(`/history/${id}/trace`);
export const promoteToEvalCase = (id: string) => request<{ id: number }>(`/history/${id}/eval-case`, { method: "POST" });
export const patchHistory = (id: string, patch: HistoryPatch) =>
  request<HistoryEntry>(`/history/${id}`, { method: "PATCH", body: patch });
export const deleteHistory = (id: string) => request<void>(`/history/${id}`, { method: "DELETE" });
export const exportHistory = (sysId: string) =>
  request<{ system: string; cases: unknown[] }>(`/history/export${q({ system_id: sysId })}`);
