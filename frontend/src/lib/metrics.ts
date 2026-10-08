import type { Metric, MetricInput, SchemaTable } from "../api";
import { exprText, legacyMeasures, measureLabel, toList } from "./measures";

/** 예전 정의의 시계열 집계 (이력·목록 표시용). 새 정의는 측정값 식으로 쓴다. */
const isSeries = (fn: string | null | undefined) => fn === "DELTA_SUM" || fn === "CHANGE_COUNT";

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

export const KIND_LABEL: Record<string, string> = { aggregate: "집계", projection: "조회", derived: "파생" };

export const EMPTY_METRIC: MetricInput = {
  name: "",
  description: "",
  kind: "aggregate",
  table_name: "",
  joins: [],
  agg_field: null,
  agg_function: null,
  select_columns: [],
  expression: null,
  series: null,
  measures: [{ name: "", expr: { fn: "COUNT", args: [] } }],
  fixed_filters: [],
  examples: [],
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

/** 저장된 지표를 수정 폼의 입력으로. 예전 정의(agg_field·select_columns)는 측정값으로 바꿔 다룬다. */
export function toInput(m: Metric): MetricInput {
  const measures = m.kind === "derived" ? [] : legacyMeasures(m);
  return {
    name: m.name,
    description: m.description,
    kind: m.kind,
    table_name: m.table_name,
    joins: m.joins ?? [],
    agg_field: measures.length ? null : m.agg_field,
    agg_function: measures.length ? null : m.agg_function,
    select_columns: measures.length ? [] : m.select_columns,
    expression: m.expression,
    series: m.series
      ? { ...m.series, partition_by: toList(m.series.partition_by), order_by: toList(m.series.order_by) }
      : null,
    measures,
    fixed_filters: m.fixed_filters,
    examples: m.examples ?? [],
    synonyms: m.synonyms ?? [],
  };
}

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
  const measures = m.kind === "derived" ? [] : m.measures ?? [];
  const seriesNote = m.series
    ? ` <${toList(m.series.partition_by).join("·") || "…"}별, ${toList(m.series.order_by).join("·") || "…"} 순>`
    : "";
  const select = measures.length
    ? measures
        .map((x) => `${exprText(x.expr)} AS ${measureLabel(x, m.name ?? "", measures.length)}`)
        .join(",\n       ") + seriesNote
    : m.kind === "projection"
      ? m.select_columns?.length
        ? m.select_columns.join(", ")
        : "…"
      : m.kind === "derived"
        ? m.expression || "…"
        : isSeries(m.agg_function)
          ? `${m.agg_function}(${m.agg_field ?? "…"}` +
            (m.agg_function === "CHANGE_COUNT" ? `, 정상=${m.series?.baseline ?? "…"}` : "") +
            (m.agg_function === "DELTA_SUM" && m.series?.max_step ? `, 한 행 최대 +${m.series.max_step}` : "") +
            `)${seriesNote}`
          : `${m.agg_function ?? "COUNT"}(${m.agg_field ?? "*"})`;
  const lines = [`SELECT ${select}`, `FROM ${m.table_name || "…"}`];
  for (const j of m.joins ?? []) {
    const on = j.on.map((p) => `${p.left} = ${p.right}`).join(" AND ") || "…";
    lines.push(`${j.type === "left" ? "LEFT" : "INNER"} JOIN ${j.table || "…"} ON ${on}`);
  }
  const where = (m.fixed_filters ?? []).map(filterToSql);
  if (where.length) lines.push("WHERE " + where.join("\n  AND "));
  return lines.join("\n");
}

/** 파생 지표 수식에서 참조한 지표 이름들 */
export function formulaRefs(expression: string | null | undefined): string[] {
  return [...(expression ?? "").matchAll(/\[([^\]]+)\]/g)].map((m) => m[1].trim());
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
