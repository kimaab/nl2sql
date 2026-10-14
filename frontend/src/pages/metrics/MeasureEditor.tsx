import { useState } from "react";
import type * as api from "../../api";
import { Button, Chip } from "../../components/ui";
import { isTemporal } from "../../lib/metrics";
import {
  byColumn,
  byKey,
  changeFn,
  exprText,
  FN,
  FN_GROUPS,
  GRAIN_LABEL,
  isAggregate,
  isCol,
  isFn,
  isNum,
  MEASURE_FUNCTIONS,
  measureLabel,
  needsColumn,
  toList,
  wrap,
} from "../../lib/measures";

export type ColumnOption = { value: string; type: string; table: string };

type Ctx = {
  kind: "aggregate" | "projection";
  columns: ColumnOption[];
  /** 기본 테이블 컬럼 — 직전 행 값(PREV·DELTA)과 시계열 집계는 이것만 받는다 */
  baseColumns: string[];
};

const SERIES_AGGREGATES = ["DELTA_SUM", "CHANGE_COUNT"];

/** 이 노드를 감쌀 수 있는 함수인가. aggs = 이 노드를 감싼 집계 함수 수 */
function canWrap(fn: string, node: api.MeasureNode, ctx: Ctx, aggs: number): boolean {
  if (needsColumn(fn)) {
    if (!isCol(node) || !ctx.baseColumns.includes(node.col)) return false;
    if (SERIES_AGGREGATES.includes(fn)) return ctx.kind === "aggregate" && aggs === 0;
    return true;
  }
  if (isAggregate(fn)) {
    // 조회형은 집계를 겹칠 수 없다 (윈도 안의 윈도). 집계형은 한 단계까지 겹친다 (BY) —
    // 바깥 집계 안(aggs 1)에서도 안쪽 질의의 집계(MAX·MIN …)는 더 넣을 수 있다.
    if (ctx.kind === "aggregate" && aggs === 1) return !(isFn(node) && containsAggregate(node));
    if (aggs > 0) return false;
    if (isFn(node) && containsAggregate(node)) return ctx.kind === "aggregate" && !containsBy(node);
    return true;
  }
  return true;
}

function containsAggregate(n: api.MeasureNode): boolean {
  return isFn(n) && (isAggregate(n.fn) || n.args.some(containsAggregate));
}

function containsBy(n: api.MeasureNode): boolean {
  return isFn(n) && (Boolean(n.by?.length) || n.args.some(containsBy));
}

/** 집계를 집계로 감쌀 때 안쪽에 BY 칸을 열어 둔다 — 비워 두면 저장되지 않아 고르게 된다.
 *  감싸는 값이 집계를 품은 식(ABS(MAX − MIN))이면 식 전체에 하나만 연다 — 식이 통째로 안쪽 질의에서 계산된다. */
function openBy(n: api.MeasureNode): api.MeasureNode {
  if (!isFn(n)) return n;
  return { ...n, by: n.by?.length ? n.by : [""] };
}

function WrapSelect({
  node,
  ctx,
  aggs,
  byHere,
  onChange,
}: {
  node: api.MeasureNode;
  ctx: Ctx;
  aggs: number;
  /** 바깥 집계 바로 아래 식 — 감싸면 BY 가 새 바깥 식으로 따라 올라간다 */
  byHere: boolean;
  onChange: (n: api.MeasureNode) => void;
}) {
  return (
    <select
      className="select expr-wrap"
      value=""
      title="지금 값을 함수로 감쌉니다"
      onChange={(e) => {
        const fn = e.target.value;
        if (!fn) return;
        const inner = isAggregate(fn) && ctx.kind === "aggregate" && containsAggregate(node) ? openBy(node) : node;
        const wrapped = wrap(inner, fn);
        if (byHere && !isAggregate(fn) && isFn(inner) && inner.by?.length && isFn(wrapped)) {
          // ABS(…) BY 차량 을 빼기로 감싸면 (ABS(…) - b) BY 차량 — 새로 붙는 b 도 같은 묶음 안에서 계산된다
          const { by, ...plain } = inner;
          onChange({ ...wrapped, args: [plain, ...wrapped.args.slice(1)], by });
          return;
        }
        onChange(wrapped);
      }}
    >
      <option value="">ƒ 감싸기</option>
      {FN_GROUPS.map((g) => {
        const options = MEASURE_FUNCTIONS.filter((f) => f.group === g && canWrap(f.fn, node, ctx, aggs));
        return options.length ? (
          <optgroup key={g} label={g}>
            {options.map((f) => (
              <option key={f.fn} value={f.fn}>
                {f.label}
              </option>
            ))}
          </optgroup>
        ) : null;
      })}
    </select>
  );
}

/** 값을 이어 붙일 수 있는 계산의 '추가' 버튼 이름 */
const ARITH_ADD: Record<string, string> = { ADD: "더할 값", SUB: "뺄 값", MUL: "곱할 값" };

function argLabel(fn: string, i: number): string {
  if (fn === "ADD" || fn === "SUB" || fn === "MUL" || fn === "DIV") return "abcdefgh"[i] ?? "";
  if (fn === "ROUND") return i === 0 ? "값" : "자릿수";
  if (FN[fn].max > 2) return `값 ${i + 1}`;
  return "";
}

function LeafEditor({
  node,
  ctx,
  onlyBase,
  onChange,
}: {
  node: { col: string } | { num: number };
  ctx: Ctx;
  onlyBase: boolean;
  onChange: (n: api.MeasureNode) => void;
}) {
  const options = onlyBase ? ctx.columns.filter((c) => ctx.baseColumns.includes(c.value)) : ctx.columns;
  return (
    <>
      <select
        className="select expr-leaf-select"
        value={isCol(node) ? node.col : "__num"}
        onChange={(e) => onChange(e.target.value === "__num" ? { num: 0 } : { col: e.target.value })}
      >
        <option value="" disabled>
          컬럼 선택
        </option>
        {options.map((c) => (
          <option key={c.value} value={c.value}>
            {c.value} ({c.type})
          </option>
        ))}
        {!onlyBase && <option value="__num">숫자 입력…</option>}
      </select>
      {isNum(node) && (
        <input
          className="input expr-num"
          type="number"
          step="any"
          value={Number.isFinite(node.num) ? node.num : ""}
          onChange={(e) => onChange({ num: e.target.value === "" ? NaN : Number(e.target.value) })}
        />
      )}
    </>
  );
}

/** 묶을 기준 고르기 목록 — 날짜 컬럼은 그대로 묶거나 일·주·월·분기·연 단위로 묶는다 */
function byOptions(ctx: Ctx): { item: api.ByItem; label: string }[] {
  return ctx.columns.flatMap((c) =>
    isTemporal(c.type)
      ? [
          ...Object.entries(GRAIN_LABEL)
            // 시 단위는 시각이 있는 컬럼만 (date 는 시각이 없다)
            .filter(([grain]) => grain !== "hour" || !/^date/i.test(c.type.trim()) || /^datetime/i.test(c.type.trim()))
            .map(([grain, label]) => ({
            item: { field: c.value, grain },
            label: `${c.value} (${label})`,
          })),
          { item: c.value, label: `${c.value} (시각 그대로)` },
        ]
      : [{ item: c.value, label: c.value }]
  );
}

const byLabel = (b: api.ByItem) => (typeof b === "string" ? b : `${b.field} (${GRAIN_LABEL[b.grain] ?? b.grain})`);

function ByEditor({
  by,
  ctx,
  inner,
  onChange,
}: {
  by: api.ByItem[];
  ctx: Ctx;
  /** 바깥 집계 안의 기준인가 — 먼저 묶어 계산한 뒤 바깥 집계로 합친다. 아니면 결과를 이 기준별로 나눈다 */
  inner: boolean;
  onChange: (by: api.ByItem[] | undefined) => void;
}) {
  // 감쌀 때 열어 둔 빈 칸("")은 '아직 고르지 않음' — 고르기 전에는 저장되지 않는다
  const chosen = by.filter((b) => Boolean(byColumn(b)));
  const taken = new Set(chosen.map(byKey));
  const options = byOptions(ctx).filter((o) => !taken.has(byKey(o.item)));
  const meaning =
    ctx.kind === "projection"
      ? "이 기준별로 따로 집계합니다 (윈도)"
      : inner
        ? "먼저 이 기준으로 묶어 계산한 뒤 바깥 집계로 합칩니다"
        : "결과를 이 기준별로 나눠 보여 줍니다";
  return (
    <div className="expr-by">
      <span className="expr-arg-label" title={`${meaning}. 여러 개 고를 수 있습니다`}>
        묶을 기준
      </span>
      {chosen.map((b) => (
        <Chip key={byKey(b)} selected onClick={() => onChange(chosen.filter((x) => byKey(x) !== byKey(b)))}>
          {byLabel(b)} ✕
        </Chip>
      ))}
      <select
        className="select expr-leaf-select"
        value=""
        aria-label="묶을 기준 추가"
        onChange={(e) => {
          const picked = options.find((o) => byKey(o.item) === e.target.value);
          if (picked) onChange([...chosen, picked.item]);
        }}
      >
        <option value="">{chosen.length ? "+ 기준 추가" : "기준 컬럼 고르기"}</option>
        {options.map((o) => (
          <option key={byKey(o.item)} value={byKey(o.item)}>
            {o.label}
          </option>
        ))}
      </select>
      <span className="field-hint">
        {chosen.length === 0 && by.length > 0 ? "기준을 하나 이상 고르세요" : meaning}
      </span>
    </div>
  );
}

/** 식 트리의 노드 하나. 함수 노드는 값(args)마다 자기 자신을 다시 그린다.
 *  aggs: 이 노드를 감싼 집계 함수 수. byHere: 집계형 지표에서 바깥 집계 바로 아래 값인가 (BY 를 다는 자리). */
export function NodeEditor({
  node,
  ctx,
  onChange,
  aggs = 0,
  byHere = false,
  onlyBase = false,
  depth = 0,
}: {
  node: api.MeasureNode;
  ctx: Ctx;
  onChange: (n: api.MeasureNode) => void;
  aggs?: number;
  byHere?: boolean;
  onlyBase?: boolean;
  depth?: number;
}) {
  if (!isFn(node)) {
    return (
      <div className="expr-leaf">
        <LeafEditor node={node} ctx={ctx} onlyBase={onlyBase} onChange={onChange} />
        {!onlyBase && <WrapSelect node={node} ctx={ctx} aggs={aggs} byHere={byHere} onChange={onChange} />}
      </div>
    );
  }

  const spec = FN[node.fn];
  const agg = isAggregate(node.fn);
  const argsAggs = aggs + (agg ? 1 : 0);
  const by = node.by ?? [];
  // 조회형: 집계는 언제나 윈도라 BY 가 묶음 기준이 된다.
  // 집계형: 집계가 하나라도 있으면 두 자리에 칸을 연다.
  //  - 측정값 맨 위 식: 결과를 이 기준별로 나눈다 (질문의 묶음에 더해짐)
  //  - 바깥 집계 바로 아래 식: 먼저 묶어 계산한 뒤 바깥 집계로 합친다 (안쪽 질의)
  // 이미 적힌 BY 는 어디에 있든 보여 준다.
  const showBy =
    by.length > 0 ||
    (ctx.kind === "projection"
      ? agg && !SERIES_AGGREGATES.includes(node.fn)
      : (depth === 0 || byHere) && !SERIES_AGGREGATES.includes(node.fn) && (agg || containsAggregate(node)));
  // 바깥 집계(aggs 0 의 집계)의 값이 BY 자리다
  const childByHere = ctx.kind === "aggregate" && agg && aggs === 0;
  const setArg = (i: number, n: api.MeasureNode) => onChange({ ...node, args: node.args.map((a, k) => (k === i ? n : a)) });

  return (
    <div className={`expr-fn ${depth % 2 ? "alt" : ""}`}>
      <div className="expr-head">
        <select
          className="select expr-fn-select"
          value={node.fn}
          onChange={(e) => {
            const fn = e.target.value;
            const next = changeFn(node, fn) as { fn: string; args: api.MeasureNode[] };
            if (needsColumn(fn) && !(next.args[0] && isCol(next.args[0]) && ctx.baseColumns.includes(next.args[0].col))) {
              next.args = [{ col: ctx.baseColumns[0] ?? "" }];
            }
            onChange(next);
          }}
        >
          {FN_GROUPS.map((g) => (
            <optgroup key={g} label={g}>
              {MEASURE_FUNCTIONS.filter((f) => f.group === g).map((f) => (
                <option key={f.fn} value={f.fn}>
                  {f.label}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
        <span className="field-hint expr-hint">{spec?.hint}</span>
        <span className="spacer" />
        <WrapSelect node={node} ctx={ctx} aggs={aggs} byHere={byHere} onChange={onChange} />
        <Button
          size="sm"
          variant="ghost"
          onClick={() => onChange(node.args[0] ?? { col: "" })}
        >
          풀기
        </Button>
      </div>
      <div className="expr-args">
        {node.fn === "COUNT" && node.args.length === 0 && <span className="muted">* — 모든 행</span>}
        {node.args.map((a, i) => (
          <div key={i} className="expr-arg">
            {argLabel(node.fn, i) && <span className="expr-arg-label">{argLabel(node.fn, i)}</span>}
            <NodeEditor
              node={a}
              ctx={ctx}
              aggs={argsAggs}
              byHere={childByHere}
              onlyBase={needsColumn(node.fn)}
              depth={depth + 1}
              onChange={(n) => setArg(i, n)}
            />
            {node.args.length > (spec?.min ?? 1) && (
              <Button size="sm" variant="ghost" onClick={() => onChange({ ...node, args: node.args.filter((_, k) => k !== i) })}>
                ✕
              </Button>
            )}
          </div>
        ))}
        {spec && node.args.length < spec.max && (spec.max > 2 || node.fn === "COUNT" || node.fn === "ROUND") && (
          <div>
            <Button
              size="sm"
              variant="ghost"
              aria-label={`+ ${node.fn === "ROUND" ? "자릿수" : ARITH_ADD[node.fn] ?? "값"}`}
              title={ARITH_ADD[node.fn] ? "값을 하나 더 이어 붙입니다 (a − b − c …). 새 값은 컬럼을 고른 뒤 ƒ 감싸기로 MAX·MIN 등을 씌웁니다" : undefined}
              onClick={() =>
                onChange({
                  ...node,
                  args: [
                    ...node.args,
                    node.fn === "ROUND" ? { num: 0 } : ARITH_ADD[node.fn] ? { num: node.fn === "MUL" ? 1 : 0 } : { col: ctx.columns[0]?.value ?? "" },
                  ],
                })
              }
            >
              + {node.fn === "ROUND" ? "자릿수" : ARITH_ADD[node.fn] ?? "값"}
            </Button>
          </div>
        )}
      </div>
      {showBy && (
        <ByEditor
          by={by}
          ctx={ctx}
          inner={aggs > 0}
          onChange={(b) => onChange(b && b.length ? { ...node, by: b } : { fn: node.fn, args: node.args })}
        />
      )}
    </div>
  );
}

const ROLLUP_OPTIONS: { value: NonNullable<api.Measure["rollup"]>; label: string; hint: string }[] = [
  { value: "SUM", label: "합계", hint: "기본 단위로 계산한 값을 더합니다 (일별 가동시간 → 월 합계)" },
  { value: "AVG", label: "평균", hint: "기본 단위로 계산한 값의 평균 (하루 평균)" },
  { value: "MAX", label: "최대", hint: "기본 단위로 계산한 값 중 가장 큰 값" },
  { value: "MIN", label: "최소", hint: "기본 단위로 계산한 값 중 가장 작은 값" },
  { value: "NONE", label: "다시 계산", hint: "질문 단위로 식을 다시 계산합니다 (월 전체의 MAX − MIN)" },
];

/** 합치는 방법을 고를 자리인가 — 맨 위 식이 날짜 단위로 결과를 나누고, 안쪽에 BY 가 없는 지표.
 *  안쪽 BY 가 있는 지표(SUM(… BY 장비))는 바깥 집계가 합치는 방법이다. */
function rollupApplies(expr: api.MeasureNode): boolean {
  if (!isFn(expr)) return false;
  const timeBy = (expr.by ?? []).some((b) => typeof b !== "string");
  return timeBy && !expr.args.some(containsBy);
}

/** 측정값(출력 컬럼) 목록 */
export function MeasureList({
  kind,
  measures,
  metricName,
  columns,
  baseColumns,
  onChange,
}: {
  kind: "aggregate" | "projection";
  measures: api.Measure[];
  metricName: string;
  columns: ColumnOption[];
  baseColumns: string[];
  onChange: (m: api.Measure[]) => void;
}) {
  const ctx: Ctx = { kind, columns, baseColumns };
  const set = (i: number, m: Partial<api.Measure>) => onChange(measures.map((x, k) => (k === i ? { ...x, ...m } : x)));
  const move = (i: number, d: number) => {
    const next = [...measures];
    [next[i], next[i + d]] = [next[i + d], next[i]];
    onChange(next);
  };
  const plainColumns = measures.flatMap((m) => (isCol(m.expr) ? [m.expr.col] : []));

  return (
    <div className="stack" style={{ gap: 10 }}>
      {measures.map((m, i) => (
        <div key={i} className="filter-card measure-card">
          <div className="row" style={{ marginBottom: 10, gap: 8 }}>
            <input
              className="input measure-name"
              value={m.name}
              placeholder={measureLabel({ ...m, name: "" }, metricName, measures.length)}
              onChange={(e) => set(i, { name: e.target.value })}
              title="결과 컬럼 이름. 질문의 정렬·조건에서 이 이름을 씁니다"
            />
            <code className="expr-text" title="식">
              {exprText(m.expr)}
            </code>
            <span className="spacer" />
            <Button size="sm" variant="ghost" disabled={i === 0} onClick={() => move(i, -1)}>
              ↑
            </Button>
            <Button size="sm" variant="ghost" disabled={i === measures.length - 1} onClick={() => move(i, 1)}>
              ↓
            </Button>
            <Button size="sm" variant="danger" onClick={() => onChange(measures.filter((_, k) => k !== i))}>
              삭제
            </Button>
          </div>
          <NodeEditor node={m.expr} ctx={ctx} onChange={(expr) => set(i, { expr })} />
          {kind === "aggregate" && rollupApplies(m.expr) && (
            <div className="expr-by">
              <span className="expr-arg-label" title="질문이 이 지표의 날짜 단위보다 큰 단위(예: 일 → 월)로 물을 때">
                더 큰 기간으로 볼 때
              </span>
              <select
                className="select expr-leaf-select"
                value={m.rollup ?? "SUM"}
                onChange={(e) => set(i, { rollup: e.target.value as api.Measure["rollup"] })}
              >
                {ROLLUP_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
              <span className="field-hint">{ROLLUP_OPTIONS.find((o) => o.value === (m.rollup ?? "SUM"))?.hint}</span>
            </div>
          )}
        </div>
      ))}

      {kind === "projection" && (
        <div className="chip-box">
          {columns.map((c) => (
            <Chip
              key={c.value}
              selected={plainColumns.includes(c.value)}
              type={c.type}
              onClick={() =>
                onChange(
                  plainColumns.includes(c.value)
                    ? measures.filter((m) => !(isCol(m.expr) && m.expr.col === c.value))
                    : [...measures, { name: "", expr: { col: c.value } }]
                )
              }
            >
              {c.value}
            </Chip>
          ))}
        </div>
      )}
      <div className="row wrap" style={{ gap: 8 }}>
        <Button
          size="sm"
          onClick={() =>
            onChange([
              ...measures,
              kind === "aggregate"
                ? { name: "", expr: { fn: "COUNT", args: [] } }
                : { name: "", expr: { col: columns[0]?.value ?? "" } },
            ])
          }
        >
          {kind === "aggregate" ? "+ 측정값" : "+ 계산 컬럼"}
        </Button>
        {kind === "aggregate" && <QuickAggregates columns={columns} measures={measures} onChange={onChange} />}
      </div>
    </div>
  );
}

/** 한 컬럼에 집계 여러 개를 한 번에 — 평균·최고·최저 같은 묶음 */
function QuickAggregates({
  columns,
  measures,
  onChange,
}: {
  columns: ColumnOption[];
  measures: api.Measure[];
  onChange: (m: api.Measure[]) => void;
}) {
  const [col, setCol] = useState("");
  const target = col || columns[0]?.value || "";
  return (
    <div className="row wrap" style={{ gap: 6 }}>
      <span className="muted">한 컬럼에 여러 집계:</span>
      <select className="select expr-leaf-select" value={target} onChange={(e) => setCol(e.target.value)}>
        {columns.map((c) => (
          <option key={c.value} value={c.value}>
            {c.value}
          </option>
        ))}
      </select>
      {["SUM", "AVG", "MIN", "MAX", "COUNT_DISTINCT"].map((fn) => (
        <Button
          key={fn}
          size="sm"
          variant="ghost"
          disabled={!target}
          onClick={() => {
            // 측정값이 여럿이 되면 각각 이름이 필요하다 — 비어 있는 이름은 식에서 지어 둔다
            const named = measures.map((m) => (m.name.trim() ? m : { ...m, name: exprText(m.expr).slice(0, 60) }));
            onChange([...named, { name: `${target.split(".").pop()}_${fn.toLowerCase()}`, expr: { fn, args: [{ col: target }] } }]);
          }}
        >
          {fn}
        </Button>
      ))}
    </div>
  );
}

/** 직전 행 비교의 구분·순서 컬럼. 누른 순서대로 들어간다. */
export function SeriesEditor({
  series,
  used,
  columns,
  onChange,
}: {
  series: api.SeriesSpec;
  used: Set<string>;
  /** 기본 테이블 컬럼 (테이블 이름 없이) */
  columns: ColumnOption[];
  onChange: (s: api.SeriesSpec) => void;
}) {
  const picker = (key: "partition_by" | "order_by") => {
    const list = toList(series[key]);
    return (
      <div className="chip-box">
        {columns.map((c) => {
          const idx = list.indexOf(c.value);
          return (
            <Chip
              key={c.value}
              selected={idx >= 0}
              order={idx + 1}
              type={c.type}
              onClick={() =>
                onChange({ ...series, [key]: idx >= 0 ? list.filter((x) => x !== c.value) : [...list, c.value] })
              }
            >
              {c.value}
            </Chip>
          );
        })}
      </div>
    );
  };
  return (
    <div className="filter-card stack" style={{ gap: 12 }}>
      <div>
        <strong>직전 행 비교</strong>
        <div className="field-hint">
          {[...used].join(", ")} 은(는) 구분 컬럼 값이 같은 행끼리 순서 컬럼 순으로 줄 세워 바로 앞 행과 비교합니다. 지표의 고정
          필터는 비교 전에 적용되고(해제 거래 제외 등), 질문의 기간 조건은 비교 뒤에 적용됩니다.
        </div>
      </div>
      <div>
        <div className="field-label">구분 컬럼 (예: 지역·단지·면적)</div>
        {picker("partition_by")}
      </div>
      <div>
        <div className="field-label">순서 컬럼 (예: 계약일, 같은 날이면 순번)</div>
        {picker("order_by")}
      </div>
      {used.has("CHANGE_COUNT") && (
        <div>
          <div className="field-label">정상값 (CHANGE_COUNT)</div>
          <input
            className="input"
            value={series.baseline ?? ""}
            placeholder="0"
            onChange={(e) => onChange({ ...series, baseline: e.target.value })}
          />
        </div>
      )}
      {used.has("DELTA_SUM") && (
        <div>
          <div className="field-label">한 행 최대 증가폭 (DELTA_SUM, 비우면 제한 없음)</div>
          <input
            className="input"
            type="number"
            min={0}
            step="any"
            value={series.max_step ?? ""}
            onChange={(e) => onChange({ ...series, max_step: e.target.value === "" ? null : Number(e.target.value) })}
          />
        </div>
      )}
    </div>
  );
}
