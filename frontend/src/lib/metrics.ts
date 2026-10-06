import type { Metric, MetricInput, SchemaTable } from "../api";

export const AGG_FUNCTIONS: { value: string; label: string }[] = [
  { value: "SUM", label: "SUM" },
  { value: "COUNT", label: "COUNT" },
  { value: "AVG", label: "AVG" },
  { value: "MIN", label: "MIN" },
  { value: "MAX", label: "MAX" },
  { value: "COUNT_DISTINCT", label: "COUNT_DISTINCT" },
  { value: "DELTA_SUM", label: "누적값 증가분 합" },
  { value: "CHANGE_COUNT", label: "상태 발생 횟수" },
];

/** 행 순서가 필요한 시계열 집계. 백엔드 compiler.SERIES_FUNCTIONS 와 같다. */
export const SERIES_FUNCTIONS: Record<string, string> = {
  DELTA_SUM: "누적 카운터(가동시간·주행거리 등)의 기간 사용량. 구분별로 시간순 직전 행보다 늘어난 만큼만 더하고, 리셋으로 줄면 0으로 봅니다.",
  CHANGE_COUNT: "상태가 정상값에서 다른 값으로 바뀐 순간만 셉니다. 같은 상태가 여러 행 이어져도 1건입니다.",
};

export const isSeries = (fn: string | null | undefined) => Boolean(fn && SERIES_FUNCTIONS[fn]);

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
  agg_field: "*",
  agg_function: "COUNT",
  select_columns: [],
  expression: null,
  series: null,
  fixed_filters: [],
  examples: [],
};

/** 저장된 지표를 수정 폼의 입력으로 */
export function toInput(m: Metric): MetricInput {
  return {
    name: m.name,
    description: m.description,
    kind: m.kind,
    table_name: m.table_name,
    joins: m.joins ?? [],
    agg_field: m.agg_field,
    agg_function: m.agg_function,
    select_columns: m.select_columns,
    expression: m.expression,
    series: m.series ?? null,
    fixed_filters: m.fixed_filters,
    examples: m.examples ?? [],
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
  const select =
    m.kind === "projection"
      ? m.select_columns?.length
        ? m.select_columns.join(", ")
        : "…"
      : m.kind === "derived"
        ? m.expression || "…"
        : isSeries(m.agg_function)
          ? `${m.agg_function}(${m.agg_field ?? "…"}` +
            (m.agg_function === "CHANGE_COUNT" ? `, 정상=${m.series?.baseline ?? "…"}` : "") +
            `) <${m.series?.partition_by || "…"}별, ${m.series?.order_by || "…"} 순>`
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

/** 동기화로 테이블·컬럼이 사라진 지표는 질문에 쓰이지 않는다.
 *  백엔드 contract.broken_reasons 와 같은 규칙: '테이블.컬럼' 은 조인 범위에서, '컬럼' 은 기본 테이블에서 찾고,
 *  파생 지표는 구성 지표가 깨지면 함께 깨진다. */
export function brokenReason(tables: SchemaTable[] | null, m: Metric, all: Metric[] = []): string | null {
  if (!tables) return null;
  const { error, scope } = metricScope(tables, m);
  if (error) return error;
  const table = scope[0];
  const has = (ref: string) => {
    const parts = ref.split(".");
    if (parts.length > 2) return false;
    const owner = parts.length === 2 ? scope.find((t) => same(t.name, parts[0].trim())) : table;
    const col = parts[parts.length - 1].trim();
    return !!owner && owner.columns.some((c) => same(c.name, col));
  };

  if (m.kind === "projection") {
    if (m.select_columns.length === 0) return "조회 컬럼이 비어 있습니다";
    const gone = m.select_columns.filter((c) => !has(c));
    if (gone.length > 0) return `조회 컬럼이 스키마에 없습니다: ${gone.join(", ")}`;
  } else if (m.kind === "aggregate" && m.agg_field !== "*" && !has(m.agg_field ?? "")) {
    return `집계 컬럼 ${m.agg_field}이(가) 스키마에 없습니다`;
  }
  if (m.kind === "aggregate" && m.series) {
    const gone = [m.series.partition_by, m.series.order_by].filter((c) => !has(c));
    if (gone.length > 0) return `구분·순서 컬럼이 스키마에 없습니다: ${gone.join(", ")}`;
  }

  const missing = m.fixed_filters.map((f) => f.field).filter((f) => f && !has(f));
  if (missing.length > 0) return `고정 필터 컬럼이 스키마에 없습니다: ${missing.join(", ")}`;

  if (m.kind === "derived") {
    for (const name of formulaRefs(m.expression)) {
      const comp = all.find((x) => same(x.name, name));
      if (!comp) return `구성 지표 ${name}이(가) 없습니다`;
      if (comp.kind !== "aggregate") return `구성 지표 ${name}은(는) 집계형이 아닙니다`;
      const reason = brokenReason(tables, comp, all);
      if (reason) return `구성 지표 ${name}이(가) 깨졌습니다 (${reason})`;
      if (!scope.some((t) => same(t.name, comp.table_name)))
        return `구성 지표 ${name}의 테이블 ${comp.table_name}이(가) 이 지표의 범위에 없습니다`;
    }
  }
  return null;
}
