import type { Metric, MetricInput, SchemaTable } from "../api";

export const AGG_FUNCTIONS = ["SUM", "COUNT", "AVG", "MIN", "MAX"];

export const OPERATORS: { value: string; label: string; symbol: string }[] = [
  { value: "equals", label: "같음", symbol: "=" },
  { value: "not_equals", label: "다름", symbol: "<>" },
  { value: "greater_than", label: "보다 큼", symbol: ">" },
  { value: "greater_or_equal", label: "이상", symbol: ">=" },
  { value: "less_than", label: "보다 작음", symbol: "<" },
  { value: "less_or_equal", label: "이하", symbol: "<=" },
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
  kind: "aggregate",
  table_name: "",
  agg_field: "*",
  agg_function: "COUNT",
  select_columns: [],
  fixed_filters: [],
};

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

function filterToSql(f: Record<string, any>): string {
  const src = f.source ?? "literal";
  const col = f.field || "?";
  const op = operatorSymbol(f.operator ?? "equals");
  if (src === "question") return `${col} ${op} <질문에서>`;
  if (src === "relative") return `${col} ${op} <오늘 ${f.value ?? ""}>`;
  const v = f.value ?? "";
  return `${col} ${op} ${v !== "" && !isNaN(Number(v)) ? v : JSON.stringify(v).replace(/^"|"$/g, "'")}`;
}

/** 미리보기·상세에서 보여줄 정의. 줄 단위로 나눠 둔다. */
export function definitionLines(m: MetricInput | Metric): string {
  const select =
    m.kind === "projection"
      ? m.select_columns.length
        ? m.select_columns.join(", ")
        : "…"
      : `${m.agg_function ?? "COUNT"}(${m.agg_field ?? "*"})`;
  const lines = [`SELECT ${select}`, `FROM ${m.table_name || "…"}`];
  const where = m.fixed_filters.map(filterToSql);
  if (where.length) lines.push("WHERE " + where.join("\n  AND "));
  return lines.join("\n");
}

/** 동기화로 테이블·컬럼이 사라진 지표는 질문에 쓰이지 않는다. */
export function brokenReason(tables: SchemaTable[] | null, m: Metric): string | null {
  if (!tables) return null;
  const table = tables.find((t) => t.name === m.table_name);
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
  if (missing.length > 0) return `고정 필터 컬럼이 스키마에 없습니다: ${missing.join(", ")}`;
  return null;
}
