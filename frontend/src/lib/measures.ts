import type { Measure, MeasureNode, MetricInput } from "../api";

/** 측정값 식에 쓸 수 있는 함수. 백엔드 compiler 의 MEASURE_* 와 같다. */
export type FnGroup = "집계" | "계산" | "함수" | "직전 행";

export interface FnSpec {
  fn: string;
  label: string;
  group: FnGroup;
  min: number;
  max: number;
  hint: string;
}

export const MEASURE_FUNCTIONS: FnSpec[] = [
  { fn: "SUM", label: "합계 SUM", group: "집계", min: 1, max: 1, hint: "값을 모두 더합니다" },
  { fn: "AVG", label: "평균 AVG", group: "집계", min: 1, max: 1, hint: "평균" },
  { fn: "MIN", label: "최소 MIN", group: "집계", min: 1, max: 1, hint: "가장 작은 값" },
  { fn: "MAX", label: "최대 MAX", group: "집계", min: 1, max: 1, hint: "가장 큰 값" },
  { fn: "COUNT", label: "건수 COUNT", group: "집계", min: 0, max: 1, hint: "행 수. 값을 비우면 COUNT(*)" },
  { fn: "COUNT_DISTINCT", label: "고유 건수 COUNT DISTINCT", group: "집계", min: 1, max: 1, hint: "서로 다른 값의 수" },
  {
    fn: "DELTA_SUM",
    label: "누적값 증가분 합 DELTA_SUM",
    group: "집계",
    min: 1,
    max: 1,
    hint: "누적 카운터의 사용량. 구분별로 직전 행보다 늘어난 만큼만 더합니다 (집계형 전용)",
  },
  {
    fn: "CHANGE_COUNT",
    label: "상태 발생 횟수 CHANGE_COUNT",
    group: "집계",
    min: 1,
    max: 1,
    hint: "정상값에서 다른 값으로 바뀐 순간만 셉니다 (집계형 전용)",
  },
  { fn: "ADD", label: "＋ 더하기", group: "계산", min: 2, max: 2, hint: "a + b" },
  { fn: "SUB", label: "－ 빼기", group: "계산", min: 2, max: 2, hint: "a - b" },
  { fn: "MUL", label: "× 곱하기", group: "계산", min: 2, max: 2, hint: "a × b" },
  { fn: "DIV", label: "÷ 나누기", group: "계산", min: 2, max: 2, hint: "a ÷ b. 0으로 나누면 비웁니다(NULL)" },
  { fn: "ROUND", label: "반올림 ROUND", group: "함수", min: 1, max: 2, hint: "두 번째 값은 소수 자릿수 (0이면 정수)" },
  { fn: "ABS", label: "절댓값 ABS", group: "함수", min: 1, max: 1, hint: "부호를 뗍니다 — 증감의 크기" },
  { fn: "COALESCE", label: "빈값 대체 COALESCE", group: "함수", min: 2, max: 8, hint: "앞 값이 비어 있으면 다음 값" },
  { fn: "GREATEST", label: "큰 값 GREATEST", group: "함수", min: 2, max: 8, hint: "여러 값 중 가장 큰 값" },
  { fn: "LEAST", label: "작은 값 LEAST", group: "함수", min: 2, max: 8, hint: "여러 값 중 가장 작은 값" },
  { fn: "PREV", label: "직전 행 값 PREV", group: "직전 행", min: 1, max: 1, hint: "구분별 순서상 바로 앞 행의 값" },
  { fn: "DELTA", label: "직전 대비 차이 DELTA", group: "직전 행", min: 1, max: 1, hint: "현재 값 − 직전 행 값" },
];

export const FN = Object.fromEntries(MEASURE_FUNCTIONS.map((f) => [f.fn, f])) as Record<string, FnSpec>;
export const FN_GROUPS: FnGroup[] = ["집계", "계산", "함수", "직전 행"];
const ARITH: Record<string, string> = { ADD: "+", SUB: "-", MUL: "*", DIV: "/" };
const SERIES_FNS = ["PREV", "DELTA", "DELTA_SUM", "CHANGE_COUNT"];
const AGGREGATES = ["SUM", "AVG", "MIN", "MAX", "COUNT", "COUNT_DISTINCT", "DELTA_SUM", "CHANGE_COUNT"];

export const isCol = (n: MeasureNode): n is { col: string } => "col" in n;
export const isNum = (n: MeasureNode): n is { num: number } => "num" in n;
export const isFn = (n: MeasureNode): n is { fn: string; args: MeasureNode[]; by?: string[] } => "fn" in n;
export const isAggregate = (fn: string) => AGGREGATES.includes(fn);
/** 컬럼 하나만 받는 함수 (직전 행 값·시계열 집계) */
export const needsColumn = (fn: string) => SERIES_FNS.includes(fn);

/** 사람이 읽는 식. 백엔드 measure_text 와 같은 모양이다. */
export function exprText(n: MeasureNode | undefined): string {
  if (!n) return "?";
  if (isCol(n)) return n.col || "?";
  if (isNum(n)) return String(n.num);
  const args = n.args ?? [];
  if (ARITH[n.fn]) {
    const side = (c: MeasureNode) => (isFn(c) && ARITH[c.fn] ? `(${exprText(c)})` : exprText(c));
    return args.map(side).join(` ${ARITH[n.fn]} `) || "?";
  }
  const inner = args.map(exprText).join(", ") || (n.fn === "COUNT" ? "*" : "");
  const by = n.by?.length ? ` BY ${n.by.join(", ")}` : "";
  return `${n.fn}(${inner}${by})`;
}

export function functionsOf(n: MeasureNode): Set<string> {
  const out = new Set<string>();
  const walk = (x: MeasureNode) => {
    if (!isFn(x)) return;
    out.add(x.fn);
    x.args.forEach(walk);
  };
  walk(n);
  return out;
}

/** 직전 행 비교를 쓰는가 — 구분·순서 컬럼(series)이 필요하다 */
export function usesSeries(measures: Measure[]): Set<string> {
  const used = new Set<string>();
  for (const m of measures) for (const f of functionsOf(m.expr)) if (SERIES_FNS.includes(f)) used.add(f);
  return used;
}

/** 빈 칸 없이 다 채웠는가 */
export function exprComplete(n: MeasureNode): boolean {
  if (isCol(n)) return Boolean(n.col);
  if (isNum(n)) return Number.isFinite(n.num);
  const spec = FN[n.fn];
  if (!spec || n.args.length < spec.min || n.args.length > spec.max) return false;
  return n.args.every(exprComplete) && (n.by ?? []).every(Boolean);
}

/** 컬럼 참조를 모두 바꾼다 (조인이 생기거나 사라질 때). null 을 돌려주면 그 컬럼은 빈 칸이 된다. */
export function mapColumns(n: MeasureNode, fix: (ref: string) => string | null): MeasureNode {
  if (isCol(n)) return { col: fix(n.col) ?? "" };
  if (isNum(n)) return n;
  return {
    ...n,
    args: n.args.map((a) => mapColumns(a, fix)),
    ...(n.by ? { by: n.by.map((b) => fix(b) ?? "").filter(Boolean) } : {}),
  };
}

/** 노드를 함수로 감싼다. 두 번째 이후 값은 그럴듯한 기본값으로 채운다. */
export function wrap(n: MeasureNode, fn: string): MeasureNode {
  const spec = FN[fn];
  const args: MeasureNode[] = spec.min === 0 && fn === "COUNT" && isNum(n) ? [] : [n];
  while (args.length < spec.min) args.push({ num: fn === "MUL" || fn === "DIV" ? 1 : 0 });
  return { fn, args };
}

/** 같은 자리에서 함수만 바꾼다. 값 개수를 새 함수에 맞춘다. */
export function changeFn(n: { fn: string; args: MeasureNode[]; by?: string[] }, fn: string): MeasureNode {
  const spec = FN[fn];
  const args = n.args.slice(0, spec.max);
  while (args.length < spec.min) args.push({ num: 0 });
  return isAggregate(fn) && n.by ? { fn, args, by: n.by } : { fn, args };
}

/** 측정값 이름. 비워 두면 백엔드가 채우는 이름과 같다. */
export function measureLabel(m: Measure, metricName: string, count: number): string {
  if (m.name.trim()) return m.name.trim();
  if (isCol(m.expr)) return m.expr.col.split(".").pop() || "?";
  return count === 1 ? metricName || "(지표 이름)" : "(이름 필요)";
}

/** 예전 정의(agg_field·select_columns)를 측정값으로. 수정 화면은 측정값으로만 다룬다. */
export function legacyMeasures(m: MetricInput): Measure[] {
  if (m.measures?.length) return m.measures;
  if (m.kind === "aggregate" && m.agg_function) {
    const args: MeasureNode[] = m.agg_field && m.agg_field !== "*" ? [{ col: m.agg_field }] : [];
    return [{ name: "", expr: { fn: m.agg_function, args } }];
  }
  if (m.kind === "projection") return m.select_columns.map((c) => ({ name: "", expr: { col: c } }));
  return [];
}

export const toList = (v: string | string[] | null | undefined): string[] =>
  v == null ? [] : (Array.isArray(v) ? v : [v]).filter(Boolean);
