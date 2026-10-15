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

/** 직전 행 비교(PREV·DELTA·DELTA_SUM·CHANGE_COUNT)의 행 순서. 예전 지표는 컬럼 하나(문자열)다. */
export interface SeriesSpec {
  partition_by: string | string[];
  order_by: string | string[];
  baseline: string | null;
  max_step?: number | null;
}

/** 식 트리. 함수를 겹쳐 값 하나를 만든다 (백엔드 compiler 의 측정값 식). 다른 계산식은 {formula: 이름}. */
/** 묶을 기준 하나. 컬럼 이름이나 날짜 컬럼 + 단위 ({"field": "collect_datetime", "grain": "day"}) */
export type ByItem = string | { field: string; grain: string };

export type MeasureNode =
  | { col: string }
  | { num: number }
  | { formula: string }
  | { fn: string; args: MeasureNode[]; by?: ByItem[] };

export interface Measure {
  name: string;
  expr: MeasureNode;
  /** 기본 기간 단위(일)보다 큰 단위(월)로 물을 때 합치는 방법. NONE = 질문 단위로 다시 계산. 비우면 SUM */
  rollup?: Rollup | null;
}

export type Rollup = "SUM" | "AVG" | "MIN" | "MAX" | "NONE";

/** 지표 = 테이블(조인) + 디멘젼 + 허용 계산식. 무엇을 조회할지는 질문마다 LLM 이 정한다. */
export interface MetricInput {
  name: string;
  description: string;
  table_name: string;
  joins: MetricJoin[];
  /** 집계 계산식이 뜻을 갖는 단위 (예: 장비 × 일) */
  dimensions: ByItem[];
  /** 이 지표가 허용하는 시스템 계산식 이름 */
  formulas: string[];
  fixed_filters: Record<string, any>[];
  synonyms: string[];
}

export interface Metric extends MetricInput {
  id: string;
  status: MetricStatus;
  broken_reason: string | null;
  version: number;
  updated_at: string | null;
}

/** forced: 질문에 '계산된 <이름>' 이 있을 때만 쓴다. organic: LLM 이 맞다고 보면 이름 없이도 쓴다. */
export type FormulaMode = "forced" | "organic";

export interface FormulaInput {
  name: string;
  description: string;
  column_name: string | null;
  mode: FormulaMode;
  expr: MeasureNode | null;
  source_sql: string | null;
  rollup: Rollup | null;
}

export interface Formula {
  id: string;
  name: string;
  description: string;
  column_name: string | null;
  mode: FormulaMode;
  expr: MeasureNode;
  text: string;
  level: "row" | "aggregate";
  source_sql: string | null;
  rollup: Rollup | null;
  refs: string[];
  used_by: string[];
  updated_at: string | null;
}

export interface ParsedFormula {
  name: string | null;
  expr: MeasureNode;
  text: string;
  level: "row" | "aggregate";
}

/** SQL 을 푼 결과. 디멘젼·테이블은 지표 등록에 쓸 후보, outer 는 질문이 정할 바깥 집계 참고 */
export interface FormulaParse {
  formulas: ParsedFormula[];
  dimensions: ByItem[];
  table: string | null;
  outer: string[];
}

export interface MetricHistoryEntry {
  version: number;
  action: "create" | "update" | "delete" | "retire";
  snapshot: Partial<MetricInput>;
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

export interface TableInfo {
  id: string;
  name: string;
  comment: string;
  purpose: string;
  column_count: number;
  metric_count: number;
  time_columns: TimeColumn[];
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
/** 저장하지 않고 정의를 SQL 로. 질문에서 받는 필터는 빠진다. */
export const previewMetric = (sysId: string, input: MetricInput) =>
  request<{ sql: string | null; error: string | null }>(`/systems/${sysId}/metrics/preview`, {
    method: "POST",
    body: input,
  });
export const deleteMetric = (sysId: string, id: string) =>
  request<void>(`/systems/${sysId}/metrics/${id}`, { method: "DELETE" });
export const metricHistory = (sysId: string, id: string) =>
  request<MetricHistoryEntry[]>(`/systems/${sysId}/metrics/${id}/history`);

// 계산식
export const listFormulas = (sysId: string) => request<Formula[]>(`/systems/${sysId}/formulas`);
export const createFormula = (sysId: string, input: FormulaInput) =>
  request<Formula>(`/systems/${sysId}/formulas`, { method: "POST", body: input });
export const updateFormula = (sysId: string, id: string, input: FormulaInput) =>
  request<Formula>(`/systems/${sysId}/formulas/${id}`, { method: "PUT", body: input });
export const deleteFormula = (sysId: string, id: string) =>
  request<void>(`/systems/${sysId}/formulas/${id}`, { method: "DELETE" });
/** SQL(식 하나나 SELECT 전체)을 식 트리로 푼다 */
export const parseFormulaSql = (sysId: string, sql: string) =>
  request<FormulaParse>(`/systems/${sysId}/formulas/parse`, { method: "POST", body: { sql } });

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
