import { useState } from "react";
import type * as api from "../../api";
import { Button, Chip } from "../../components/ui";
import {
  changeFn,
  exprText,
  FN,
  FN_GROUPS,
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

/** 이 노드를 감쌀 수 있는 함수인가 */
function canWrap(fn: string, node: api.MeasureNode, ctx: Ctx, insideAgg: boolean): boolean {
  if (needsColumn(fn)) {
    if (!isCol(node) || !ctx.baseColumns.includes(node.col)) return false;
    if (SERIES_AGGREGATES.includes(fn)) return ctx.kind === "aggregate" && !insideAgg;
    return true;
  }
  if (isAggregate(fn)) {
    // 조회형은 집계를 겹칠 수 없다 (윈도 안의 윈도). 집계형은 한 단계까지 겹친다 (BY).
    if (insideAgg) return false;
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

/** 집계를 집계로 감쌀 때 안쪽 집계에 BY 칸을 열어 둔다 — 비워 두면 저장되지 않아 고르게 된다 */
function openBy(n: api.MeasureNode): api.MeasureNode {
  if (!isFn(n)) return n;
  if (isAggregate(n.fn)) return { ...n, by: n.by?.length ? n.by : [""] };
  return { ...n, args: n.args.map(openBy) };
}

function WrapSelect({
  node,
  ctx,
  insideAgg,
  onChange,
}: {
  node: api.MeasureNode;
  ctx: Ctx;
  insideAgg: boolean;
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
        onChange(wrap(inner, fn));
      }}
    >
      <option value="">ƒ 감싸기</option>
      {FN_GROUPS.map((g) => {
        const options = MEASURE_FUNCTIONS.filter((f) => f.group === g && canWrap(f.fn, node, ctx, insideAgg));
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

function argLabel(fn: string, i: number): string {
  if (fn === "ADD" || fn === "SUB" || fn === "MUL" || fn === "DIV") return i === 0 ? "a" : "b";
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

function ByEditor({
  by,
  ctx,
  onChange,
}: {
  by: string[];
  ctx: Ctx;
  onChange: (by: string[] | undefined) => void;
}) {
  return (
    <div className="expr-by">
      <span className="expr-arg-label" title={ctx.kind === "aggregate" ? "이 기준으로 먼저 묶어 집계한 뒤, 바깥 함수가 그 결과를 다시 집계합니다" : "이 기준별로 따로 집계합니다 (윈도)"}>
        {ctx.kind === "aggregate" ? "먼저 묶을 기준 BY" : "묶음별 BY"}
      </span>
      {by.map((b, i) => (
        <span key={i} className="row" style={{ gap: 4 }}>
          <select
            className="select expr-leaf-select"
            value={b}
            onChange={(e) => onChange(by.map((x, k) => (k === i ? e.target.value : x)))}
          >
            <option value="" disabled>
              기준 컬럼
            </option>
            {ctx.columns.map((c) => (
              <option key={c.value} value={c.value}>
                {c.value}
              </option>
            ))}
          </select>
          <Button size="sm" variant="ghost" onClick={() => onChange(by.filter((_, k) => k !== i))}>
            ✕
          </Button>
        </span>
      ))}
      <Button size="sm" variant="ghost" onClick={() => onChange([...by, ""])}>
        + 기준
      </Button>
    </div>
  );
}

/** 식 트리의 노드 하나. 함수 노드는 값(args)마다 자기 자신을 다시 그린다. */
export function NodeEditor({
  node,
  ctx,
  onChange,
  insideAgg = false,
  onlyBase = false,
  depth = 0,
}: {
  node: api.MeasureNode;
  ctx: Ctx;
  onChange: (n: api.MeasureNode) => void;
  insideAgg?: boolean;
  onlyBase?: boolean;
  depth?: number;
}) {
  if (!isFn(node)) {
    return (
      <div className="expr-leaf">
        <LeafEditor node={node} ctx={ctx} onlyBase={onlyBase} onChange={onChange} />
        {!onlyBase && <WrapSelect node={node} ctx={ctx} insideAgg={insideAgg} onChange={onChange} />}
      </div>
    );
  }

  const spec = FN[node.fn];
  const agg = isAggregate(node.fn);
  const argsInsideAgg = insideAgg || agg;
  const by = node.by ?? [];
  // 조회형: 집계는 언제나 윈도라 BY 가 묶음 기준이 된다. 집계형: 다른 집계 안에서만 BY 를 쓴다.
  const showBy = agg && !SERIES_AGGREGATES.includes(node.fn) && (ctx.kind === "projection" || insideAgg || by.length > 0);
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
        <WrapSelect node={node} ctx={ctx} insideAgg={insideAgg} onChange={onChange} />
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
              insideAgg={argsInsideAgg}
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
              onClick={() =>
                onChange({ ...node, args: [...node.args, node.fn === "ROUND" ? { num: 0 } : { col: ctx.columns[0]?.value ?? "" }] })
              }
            >
              + {node.fn === "ROUND" ? "자릿수" : "값"}
            </Button>
          </div>
        )}
      </div>
      {showBy && (
        <ByEditor
          by={by}
          ctx={ctx}
          onChange={(b) => onChange(b && b.length ? { ...node, by: b } : { fn: node.fn, args: node.args })}
        />
      )}
    </div>
  );
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
