const API_BASE = "/api"; // vite.config 의 proxy 가 백엔드(localhost:8000)로 넘긴다

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
  db_schemas: string[];
  username: string;
  synced_at: string | null;
  table_count: number;
  metric_count: number;
  active_metric_count: number;
  /** Superset 에 등록한 DB 연결 id (없으면 미연결) */
  superset_database_id: number | null;
}

export interface SystemInput {
  code: string;
  name: string;
  domain_desc: string;
  driver: Driver;
  host: string;
  port: number;
  db_name: string;
  db_schemas: string[];
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

/** 등록 전 확인: SQL → AST → SQL → (초안이면 실행 전 확인) → 대상 DB 에서 결과 비교. stage 는 실패한 단계 */
export interface MetricCheck {
  ast: Record<string, any> | null;
  sql: string | null;
  tables: string[];
  warnings: string[];
  rows: number | null;
  stage: "parse" | "compile" | "guard" | "verify" | null;
  error: string | null;
}

/** 지표가 없는 계산 하나. 테이블 선택 단계가 적고, 지표 초안의 출발점이 된다 */
export interface MissingMetric {
  name: string;
  table: string;
  column: string;
}

/** 대상 컬럼의 실제 값 샘플. 인덱스를 타는 조회로만 뽑는다 — 못 뽑으면 rows 가 비고 note 에 이유가 있다 */
export interface ValueSample {
  index: string | null;
  queries: string[];
  columns: string[];
  rows: string[][];
  latest: string | null;
  note: string;
}

/** LLM 이 쓴 지표 초안 (저장되지 않음). full_scans: 초안 SQL 의 실행 계획이 전체를 훑는 큰 테이블 */
export interface MetricDraft {
  name: string;
  description: string;
  synonyms: string[];
  sql: string;
  assumptions: string[];
  unsure: string | null;
  attempts: number;
  sample: ValueSample;
  check: MetricCheck | null;
  full_scans: string[];
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
  /** 행마다 시차를 담은 컬럼 (예: 차량별 timezone). 비면 utc_offset_hours 만 쓴다 */
  utc_offset_column: string;
}

/** 컬럼과 '계산 필요' 표시. 계산 필요 컬럼을 계산해야 답할 수 있는 질문은 지표가 필요하다 */
export interface ColumnInfo {
  name: string;
  data_type: string;
  comment: string;
  /** 코멘트가 없을 때 추론한 설명 */
  ai_comment: string;
  /** 사람이 적은 설명. 코멘트와 추론한 설명보다 먼저 쓴다 */
  note: string;
  needs_calc: boolean;
}

export interface TableInfo {
  id: string;
  name: string;
  comment: string;
  /** 코멘트가 없을 때 추론한 설명 */
  ai_comment: string;
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
  /** auto: 컬럼 이름, 타입, PK 로 추론한 관계 (설명 자동 채우기가 만든다) */
  source: "fk" | "manual" | "auto";
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

/** 질문 분석의 항목 하나. column 은 '테이블.컬럼', '테이블.*' 또는 빈 값 */
export interface AskPart {
  name: string;
  column: string;
  value?: string | null;
}

export interface AskAnalysis {
  items: AskPart[];
  grouping: boolean;
  dimensions: AskPart[];
  conditions: AskPart[];
  ambiguous: { part: string; question: string }[];
}

/** 조회 항목이 '전체' 인 테이블의 컬럼 목록. text 를 고쳐 질문에 붙인다 */
export interface ColumnChoice {
  table: string;
  description: string;
  columns: { name: string; label: string }[];
  text: string;
}

export interface AskResponse {
  id: string | null;
  sql: string | null;
  error: string | null;
  attempts: number;
  clarification: string | null;
  columns: ColumnChoice[];
  analysis: AskAnalysis | null;
  selected_tables: string[];
  selected_metrics: string[];
  /** 지표가 필요한 질문인데 맞는 지표가 없다 — error 에 이유가 있다 */
  needs_metric: boolean;
  missing: MissingMetric[];
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
/** 시스템을 Superset DB 연결로 등록 (이미 있으면 접속 정보 갱신) */
export const linkSuperset = (id: string) => request<System>(`/systems/${id}/superset`, { method: "POST" });
/** 질문 기록의 생성 SQL 을 Superset 으로 연다. SQL 은 보내지 않는다 — 백엔드가 기록에서 꺼낸다 */
export const openInSqlLab = (entryId: string) =>
  request<{ url: string }>(`/history/${entryId}/superset/sqllab`, { method: "POST" });
export const createSupersetChart = (entryId: string) =>
  request<{ url: string; viz_type: string; dataset_id: number; chart_id: number }>(
    `/history/${entryId}/superset/chart`,
    { method: "POST" }
  );
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
export const setUtcOffsetColumn = (id: string, tableId: string, column: string, offsetColumn: string) =>
  request<TableInfo>(`/systems/${id}/tables/${tableId}/utc-offset-column`, {
    method: "PUT",
    body: { column, utc_offset_column: offsetColumn },
  });
export const setColumnNote = (id: string, tableId: string, column: string, note: string) =>
  request<TableInfo>(`/systems/${id}/tables/${tableId}/column-note`, { method: "PUT", body: { column, note } });
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
export const draftMetric = (sysId: string, question: string, missing: MissingMetric) =>
  request<MetricDraft>(`/systems/${sysId}/metrics/draft`, { method: "POST", body: { question, missing } });
export const deleteMetric = (sysId: string, id: string) =>
  request<void>(`/systems/${sysId}/metrics/${id}`, { method: "DELETE" });
export const metricHistory = (sysId: string, id: string) =>
  request<MetricHistoryEntry[]>(`/systems/${sysId}/metrics/${id}/history`);

// 질문
/** userId: 질문한 사용자. 질문의 '나', '내 권한' 이 이 사용자다 (비우면 사용자 없이 묻는다) */
export const ask = (systemId: string, question: string, userId = "") =>
  request<AskResponse>("/ask", { method: "POST", body: { system_id: systemId, question, user_id: userId } });

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

// 지표 자동 등록 · 지표 검증 (스레드에서 돌고, 실행 기록을 다시 읽어 진행을 본다)
export type RunStatus = "running" | "done" | "error" | "interrupted";

export interface AutoRunRequest {
  /** 비우면 지표가 없는 테이블 중 관계(FK)가 많은 것부터 max_tables 개 */
  tables: string[];
  max_tables: number;
  per_table: number;
  /** 실행 계획이 큰 테이블 전체를 훑어도 결과 비교를 돌린다 (테스트 DB 용) */
  allow_full_scan: boolean;
}

export interface AutoTry {
  sql: string;
  stage: string | null;
  error: string | null;
}

/** 자동 등록 후보 하나. stage: 멈춘 단계 (llm | duplicate | simple | parse | compile | guard | verify | save) */
export interface AutoItem {
  id: number;
  table_name: string;
  name: string;
  description: string;
  synonyms: string[];
  sql: string;
  rationale: string;
  status: "registered" | "failed" | "skipped";
  stage: string | null;
  reason: string;
  attempts: number;
  tries: AutoTry[];
  check: MetricCheck | null;
  full_scans: string[];
  metric_id: string | null;
  created_at: string;
}

export interface AutoRun {
  id: string;
  system_id: string;
  status: RunStatus;
  options: Record<string, any>;
  tables: string[];
  tables_done: number;
  proposed: number;
  registered: number;
  failed: number;
  skipped: number;
  prompt_tokens: number;
  completion_tokens: number;
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface AutoRunDetail extends AutoRun {
  items: AutoItem[];
}

export interface ValidationRequest {
  /** 고른 지표만 검증한다 (비우면 서버가 거절한다) */
  metric_ids: string[];
  run_sql: boolean;
  use_llm: boolean;
  allow_full_scan: boolean;
}

export type CheckStatus = "pass" | "warn" | "fail" | "skip";

export interface ValidationCheck {
  key: string;
  label: string;
  status: CheckStatus;
  message: string;
  detail: Record<string, any>;
}

export interface ValidationItem {
  id: number;
  run_id: string;
  metric_id: string;
  metric_name: string;
  metric_version: number;
  verdict: "pass" | "warn" | "fail";
  summary: string;
  checks: ValidationCheck[];
  elapsed_ms: number;
  created_at: string;
}

export interface ValidationRun {
  id: string;
  system_id: string;
  status: RunStatus;
  options: Record<string, any>;
  total: number;
  done: number;
  passed: number;
  warned: number;
  failed: number;
  prompt_tokens: number;
  completion_tokens: number;
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface ValidationRunDetail extends ValidationRun {
  items: ValidationItem[];
}

export const startAutoRun = (sysId: string, input: AutoRunRequest) =>
  request<AutoRun>(`/systems/${sysId}/metric-auto/runs`, { method: "POST", body: input });
export const listAutoRuns = (sysId: string) => request<AutoRun[]>(`/systems/${sysId}/metric-auto/runs`);
export const getAutoRun = (sysId: string, runId: string) =>
  request<AutoRunDetail>(`/systems/${sysId}/metric-auto/runs/${runId}`);
export const startValidation = (sysId: string, input: ValidationRequest) =>
  request<ValidationRun>(`/systems/${sysId}/metric-validations`, { method: "POST", body: input });
export const listValidations = (sysId: string) => request<ValidationRun[]>(`/systems/${sysId}/metric-validations`);
export const getValidation = (sysId: string, runId: string) =>
  request<ValidationRunDetail>(`/systems/${sysId}/metric-validations/${runId}`);
export const metricValidations = (sysId: string, metricId: string) =>
  request<ValidationItem[]>(`/systems/${sysId}/metrics/${metricId}/validations`);
