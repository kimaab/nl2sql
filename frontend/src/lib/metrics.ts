import type { ByItem, Metric, MetricInput, SchemaTable } from "../api";
import { GRAIN_LABEL } from "./measures";

export const OPERATORS: { value: string; label: string; symbol: string; noValue?: boolean }[] = [
  { value: "equals", label: "같음", symbol: "=" },
  { value: "not_equals", label: "다름", symbol: "<>" },
  { value: "greater_than", label: "보다 큼", symbol: ">" },
  { value: "greater_or_equal", label: "이상", symbol: ">=" },
  { value: "less_than", label: "보다 작음", symbol: "<" },
  { value: "less_or_equal", label: "이하", symbol: "<=" },
  { value: "contains", label: "포함", symbol: "LIKE %…%" },
  { value: "starts_with", label: "시작", symbol: "LIKE …%" },
  { value: "is_null", label: "비어 있음", symbol: "IS NULL", noValue: true },
  { value: "is_not_null", label: "값 있음", symbol: "IS NOT NULL", noValue: true },
];

export const SOURCES: { value: string; label: string; hint: string }[] = [
  { value: "literal", label: "고정", hint: "질문과 무관하게 항상 적용됩니다" },
  {
    value: "relative",
    label: "상대 기간",
    hint: "오늘 기준으로 계산합니다. 질문이 같은 컬럼을 지정하면 물러납니다",
  },
  {
    value: "question",
    label: "질문에서",
    hint: "값을 질문에서 받습니다. 질문이 지정하지 않으면 거부합니다",
  },
];

export const EMPTY_METRIC: MetricInput = {
  name: "",
  description: "",
  table_name: "",
  joins: [],
  dimensions: [],
  formulas: [],
  fixed_filters: [],
  synonyms: [],
};

export const STATUS_LABEL: Record<string, string> = {
  draft: "검수 대기",
  active: "사용 중",
  broken: "깨짐",
  retired: "사용 안 함",
};
export const STATUS_TONE: Record<string, "ok" | "warn" | "danger" | undefined> = {
  draft: "warn",
  active: "ok",
  broken: "danger",
  retired: undefined,
};

/** 저장된 지표를 수정 폼의 입력으로 */
export function toInput(m: Metric): MetricInput {
  return {
    name: m.name,
    description: m.description,
    table_name: m.table_name,
    joins: m.joins ?? [],
    dimensions: m.dimensions ?? [],
    formulas: m.formulas ?? [],
    fixed_filters: m.fixed_filters,
    synonyms: m.synonyms ?? [],
  };
}

/** 디멘젼 하나를 읽을 수 있게 — collect_datetime (일) */
export const dimensionLabel = (d: ByItem) =>
  typeof d === "string" ? d : `${d.field} (${GRAIN_LABEL[d.grain] ?? d.grain})`;

/** 날짜 컬럼인지만 본다. 시각 포함 여부는 백엔드가 드라이버를 보고 판정한다. */
export function isTemporal(type: string): boolean {
  const t = (type || "").trim().toLowerCase();
  if (!t) return false;
  if (t.startsWith("timestamp") || t.startsWith("datetime")) return true;
  return t.split("(")[0].trim() === "date";
}

/** 날짜 컬럼이면 상대 기간을 기본으로 준다 — 날짜를 고정값으로 박아두면
 *  다음 주에 그 지표가 지난주를 가리키게 된다. */
export function newFilter(field: string, type: string) {
  return isTemporal(type)
    ? { field, operator: "greater_or_equal", source: "relative", value: "-7d" }
    : { field, operator: "equals", source: "literal", value: "" };
}

export function operatorSymbol(op: string): string {
  return OPERATORS.find((o) => o.value === op)?.symbol ?? op;
}

export function isNoValue(op: string | undefined): boolean {
  return Boolean(OPERATORS.find((o) => o.value === op)?.noValue);
}

function filterToSql(f: Record<string, any>): string {
  const src = f.source ?? "literal";
  const col = f.field || "?";
  const op = operatorSymbol(f.operator ?? "equals");
  if (isNoValue(f.operator)) return `${col} ${op}`;
  if (src === "question") return `${col} ${op} <질문에서>`;
  if (src === "relative") return `${col} ${op} <오늘 ${f.value ?? ""}>`;
  const v = f.value ?? "";
  return `${col} ${op} ${v !== "" && !isNaN(Number(v)) ? v : JSON.stringify(v).replace(/^"|"$/g, "'")}`;
}

/** 미리보기·상세·이력에서 보여줄 정의. 줄 단위로 나눠 둔다. */
export function definitionLines(m: Partial<MetricInput>): string {
  const lines = [`FROM ${m.table_name || "…"}`];
  for (const j of m.joins ?? []) {
    const on = j.on.map((p) => `${p.left} = ${p.right}`).join(" AND ") || "…";
    lines.push(`${j.type === "left" ? "LEFT" : "INNER"} JOIN ${j.table || "…"} ON ${on}`);
  }
  const where = (m.fixed_filters ?? []).map(filterToSql);
  if (where.length) lines.push("WHERE " + where.join("\n  AND "));
  lines.push(`-- 디멘젼: ${(m.dimensions ?? []).map(dimensionLabel).join(", ") || "없음"}`);
  lines.push(`-- 계산식: ${(m.formulas ?? []).join(", ") || "없음"}`);
  return lines.join("\n");
}

const same = (a: string, b: string) => a.toLowerCase() === b.toLowerCase();

/** 지표가 쓸 수 있는 테이블 범위. 조인 테이블을 찾지 못하면 이유를 돌려준다. */
export function metricScope(tables: SchemaTable[], m: Pick<MetricInput, "table_name" | "joins">) {
  const table = tables.find((t) => same(t.name, m.table_name));
  if (!table) return { error: `테이블 ${m.table_name}이(가) 스키마에 없습니다`, scope: [] as SchemaTable[] };
  const scope = [table];
  for (const j of m.joins ?? []) {
    const joined = tables.find((t) => same(t.name, j.table));
    if (!joined) return { error: `조인 테이블 ${j.table}이(가) 스키마에 없습니다`, scope };
    scope.push(joined);
  }
  return { error: null, scope };
}
