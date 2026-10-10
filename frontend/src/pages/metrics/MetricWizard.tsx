import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import * as api from "../../api";
import {
  Alert,
  Button,
  Chip,
  Field,
  Loading,
  OptionCard,
  PageHeader,
  Segmented,
  SqlBlock,
} from "../../components/ui";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/format";
import {
  definitionLines,
  EMPTY_METRIC,
  formulaRefs,
  isNoValue,
  isTemporal,
  KIND_LABEL,
  metricScope,
  newFilter,
  OPERATORS,
  SOURCES,
  toInput,
} from "../../lib/metrics";
import { exprComplete, mapColumns, toList, usesSeries } from "../../lib/measures";
import { MeasureList, SeriesEditor } from "./MeasureEditor";

const COUNT_ALL: api.Measure[] = [{ name: "", expr: { fn: "COUNT", args: [] } }];

/** 서버 SQL 을 읽기 좋게 절마다 줄을 바꾼다 */
const formatSql = (sql: string) => sql.replace(/ (FROM|WHERE|GROUP BY|HAVING|ORDER BY|LIMIT|FETCH FIRST) /g, "\n$1 ");

const STEPS = ["종류", "대상", "조건", "이름·설명"];

/** 조인이 생기거나 사라질 때 컬럼 참조를 맞춘다.
 *  조인이 있으면 모두 '테이블.컬럼', 없으면 기본 테이블의 컬럼 이름만 쓴다. */
function requalify(form: api.MetricInput, joins: api.MetricJoin[]): api.MetricInput {
  const base = form.table_name;
  const qualify = joins.length > 0;
  const fix = (ref: string | null) => {
    if (!ref || ref === "*") return ref;
    const dot = ref.indexOf(".");
    if (qualify) return dot < 0 ? `${base}.${ref}` : ref;
    if (dot < 0) return ref;
    return ref.slice(0, dot).toLowerCase() === base.toLowerCase() ? ref.slice(dot + 1) : null;
  };
  const kept = new Set([base.toLowerCase(), ...joins.map((j) => j.table.toLowerCase())]);
  const alive = (ref: string | null) =>
    ref !== null && (ref.indexOf(".") < 0 || kept.has(ref.slice(0, ref.indexOf(".")).toLowerCase()));
  const fixAlive = (ref: string) => {
    const fixed = fix(ref);
    return alive(fixed) ? fixed : null;
  };
  return {
    ...form,
    joins,
    measures: form.measures.map((m) => ({ ...m, expr: mapColumns(m.expr, fixAlive) })),
    agg_field: form.agg_field && alive(fix(form.agg_field)) ? fix(form.agg_field) : form.kind === "aggregate" ? "*" : null,
    select_columns: form.select_columns.map(fix).filter(alive) as string[],
    fixed_filters: form.fixed_filters
      .map((f) => ({ ...f, field: fix(f.field) }))
      .filter((f) => alive(f.field)),
  };
}

export function MetricWizard() {
  const { dsId: editDs, metricId } = useParams();
  const editing = Boolean(editDs && metricId);
  const [params] = useSearchParams();
  const [systems, setSystems] = useState<api.System[] | null>(null);
  const [dsId, setDsId] = useState(editDs ?? params.get("ds") ?? "");
  const [tables, setTables] = useState<api.SchemaTable[]>([]);
  const [relations, setRelations] = useState<api.Relation[]>([]);
  const [metrics, setMetrics] = useState<api.Metric[]>([]);
  const [step, setStep] = useState(editing ? 1 : 0);
  const [form, setForm] = useState<api.MetricInput>(EMPTY_METRIC);
  const [loaded, setLoaded] = useState(!editing);
  const [saving, setSaving] = useState(false);
  const [preview, setPreview] = useState<{ sql: string | null; error: string | null } | null>(null);
  const toast = useToast();
  const navigate = useNavigate();

  useEffect(() => {
    api
      .listSystems()
      // 동기화된 것만 쓸 수 있다. 스키마가 없으면 테이블·컬럼을 고를 수 없다.
      .then((list) => setSystems(list.filter((d) => d.synced_at)))
      .catch((err) => {
        toast("시스템을 불러오지 못했습니다: " + errorMessage(err), "error");
        setSystems([]);
      });
  }, []);

  useEffect(() => {
    if (!dsId) {
      setTables([]);
      setRelations([]);
      setMetrics([]);
      return;
    }
    Promise.all([api.getSchema(dsId), api.listRelations(dsId), api.listMetrics(dsId)])
      .then(([s, rels, ms]) => {
        setTables(s.tables);
        setRelations(rels);
        setMetrics(ms);
        if (editing) {
          const current = ms.find((m) => m.id === metricId);
          if (!current) {
            toast("수정할 지표를 찾을 수 없습니다", "error");
          } else {
            setForm(toInput(current));
          }
          setLoaded(true);
        } else {
          setForm((prev) => ({ ...prev, table_name: prev.table_name || s.tables[0]?.name || "" }));
        }
      })
      .catch((err) => toast("스키마를 불러오지 못했습니다: " + errorMessage(err), "error"));
  }, [dsId]);

  const patch = (p: Partial<api.MetricInput>) => setForm((prev) => ({ ...prev, ...p }));
  const qualified = form.joins.length > 0;

  // 이 지표가 쓸 수 있는 컬럼. 조인이 있으면 '테이블.컬럼'.
  const columnOptions = useMemo(() => {
    const { scope } = metricScope(tables, form);
    return scope.flatMap((t) =>
      t.columns.map((c) => ({ value: qualified ? `${t.name}.${c.name}` : c.name, type: c.type, table: t.name }))
    );
  }, [tables, form.table_name, form.joins, qualified]);
  const typeOf = (ref: string) => columnOptions.find((c) => c.value === ref)?.type ?? "";
  // 직전 행 비교(LAG)는 기본 테이블 컬럼만 쓴다 (LAG 서브쿼리가 기본 테이블만 감싼다)
  const baseColumns = columnOptions.filter((c) => c.table === form.table_name);
  // 구분·순서 컬럼은 조인이 있어도 기본 테이블의 컬럼 이름만 적는다
  const bare = (ref: string) => (ref.includes(".") ? ref.slice(ref.indexOf(".") + 1) : ref);
  const seriesUsed = form.kind === "derived" ? new Set<string>() : usesSeries(form.measures);

  /** 측정값을 바꾼다. 직전 행 비교를 처음 쓰면 순서 컬럼은 첫 날짜 컬럼으로 넣어 둔다.
   *  구분 컬럼은 짐작하지 않는다 — '_id' 로 끝나는 수집 ID 같은 엉뚱한 컬럼이 걸리기 쉽다. */
  function setMeasures(measures: api.Measure[]) {
    const used = usesSeries(measures);
    if (!used.size) {
      patch({ measures, series: null });
      return;
    }
    const series: api.SeriesSpec = form.series ?? {
      partition_by: [],
      order_by: baseColumns.filter((c) => isTemporal(c.type)).slice(0, 1).map((c) => bare(c.value)),
      baseline: null,
    };
    patch({
      measures,
      series: {
        ...series,
        baseline: used.has("CHANGE_COUNT") ? series.baseline ?? "0" : null,
        max_step: used.has("DELTA_SUM") ? series.max_step ?? null : null,
      },
    });
  }

  function setKind(kind: api.MetricKind) {
    const reset = { kind, agg_field: null, agg_function: null, select_columns: [], series: null };
    if (kind === "aggregate") patch({ ...reset, expression: null, measures: COUNT_ALL });
    else if (kind === "projection") patch({ ...reset, expression: null, measures: [] });
    else patch({ ...reset, expression: form.expression ?? "", measures: [] });
  }

  function setFilter(i: number, p: Record<string, any>) {
    const next = [...form.fixed_filters];
    next[i] = { ...next[i], ...p };
    patch({ fixed_filters: next });
  }

  // ---- 조인
  const scopeNames = [form.table_name, ...form.joins.map((j) => j.table)].filter(Boolean);

  /** 범위 안의 테이블과 table 사이의 관계 → ON 쌍 */
  function relationPairs(table: string, earlier: string[]) {
    const pairs: { left: string; right: string }[] = [];
    for (const r of relations) {
      if (earlier.includes(r.left_table) && r.right_table === table)
        pairs.push({ left: `${r.left_table}.${r.left_column}`, right: `${table}.${r.right_column}` });
      else if (earlier.includes(r.right_table) && r.left_table === table)
        pairs.push({ left: `${r.right_table}.${r.right_column}`, right: `${table}.${r.left_column}` });
    }
    return pairs;
  }

  function relatedTables(earlier: string[]) {
    const names = new Set<string>();
    for (const r of relations) {
      if (earlier.includes(r.left_table) && !earlier.includes(r.right_table)) names.add(r.right_table);
      if (earlier.includes(r.right_table) && !earlier.includes(r.left_table)) names.add(r.left_table);
    }
    return names;
  }

  function setJoins(joins: api.MetricJoin[]) {
    setForm((prev) => requalify(prev, joins));
  }

  function setJoinTable(i: number, table: string) {
    const earlier = scopeNames.slice(0, i + 1);
    const next = [...form.joins];
    const pairs = relationPairs(table, earlier);
    next[i] = { ...next[i], table, on: pairs.length ? pairs : [{ left: "", right: "" }] };
    setJoins(next);
  }

  function addJoin() {
    const related = [...relatedTables(scopeNames)];
    const table = related[0] ?? tables.find((t) => !scopeNames.includes(t.name))?.name ?? "";
    const pairs = relationPairs(table, scopeNames);
    setJoins([...form.joins, { table, type: "inner", on: pairs.length ? pairs : [{ left: "", right: "" }] }]);
  }

  function columnsOf(names: string[]) {
    return tables
      .filter((t) => names.includes(t.name))
      .flatMap((t) => t.columns.map((c) => `${t.name}.${c.name}`));
  }

  const aggregateMetrics = metrics.filter(
    (m) => m.kind === "aggregate" && m.id !== metricId && scopeNames.includes(m.table_name)
  );
  const unknownRefs = formulaRefs(form.expression).filter(
    (r) => !aggregateMetrics.some((m) => m.name.toLowerCase() === r.toLowerCase())
  );

  const joinsValid = form.joins.every((j) => j.table && j.on.length > 0 && j.on.every((p) => p.left && p.right));
  const stepValid = [
    true,
    Boolean(dsId && form.table_name) &&
      joinsValid &&
      (form.kind === "derived"
        ? Boolean(form.expression?.trim()) && formulaRefs(form.expression).length > 0
        : form.measures.length > 0 &&
          form.measures.every((m) => exprComplete(m.expr)) &&
          (form.measures.length === 1 || form.measures.every((m) => m.name.trim() || "col" in m.expr)) &&
          (seriesUsed.size === 0 ||
            Boolean(
              form.series &&
                toList(form.series.partition_by).length &&
                toList(form.series.order_by).length &&
                (!seriesUsed.has("CHANGE_COUNT") || String(form.series.baseline ?? "").trim()) &&
                (form.series.max_step == null || form.series.max_step > 0)
            ))),
    form.fixed_filters.every(
      (f) =>
        f.field &&
        ((f.source ?? "literal") === "question" || isNoValue(f.operator) || String(f.value ?? "") !== "")
    ),
    form.name.trim() !== "",
  ];

  // 서버가 실제로 컴파일한 SQL. 함수 조합 규칙(집계 안의 집계, 직전 행 비교 등)은 서버가 판정한다.
  useEffect(() => {
    // 빈 칸이 남은 식은 보내지 않는다 — 고르는 중에 오류가 깜빡이지 않게
    if (!dsId || !form.table_name || (form.kind !== "derived" && !stepValid[1])) {
      setPreview(null);
      return;
    }
    let stale = false;
    const timer = setTimeout(() => {
      api
        .previewMetric(dsId, { ...form, name: form.name.trim() || "새 지표" })
        .then((p) => !stale && setPreview(p))
        .catch((err) => !stale && setPreview({ sql: null, error: errorMessage(err) }));
    }, 400);
    return () => {
      stale = true;
      clearTimeout(timer);
    };
  }, [dsId, form]);

  async function handleSave() {
    const body = { ...form, name: form.name.trim() };
    try {
      setSaving(true);
      const saved = editing
        ? await api.updateMetric(dsId, metricId!, body)
        : await api.createMetric(dsId, body);
      toast(editing ? `지표를 수정했습니다 (v${saved.version})` : "지표를 등록했습니다", "ok");
      navigate(`/metrics/${dsId}/${saved.id}`);
    } catch (err) {
      toast((editing ? "지표 수정 실패: " : "지표 등록 실패: ") + errorMessage(err), "error");
    } finally {
      setSaving(false);
    }
  }

  if (systems === null || !loaded) return <Loading />;

  const questionFilters = form.fixed_filters.filter((f) => (f.source ?? "literal") === "question").length;

  return (
    <div className="page wide">
      <PageHeader
        back={editing ? { to: `/metrics/${dsId}/${metricId}`, label: "지표 상세" } : { to: "/metrics", label: "지표 목록" }}
        title={editing ? `지표 수정 — ${form.name}` : "새 지표 등록"}
        desc={
          editing
            ? "저장하면 버전이 올라가고 이전 정의는 이력에 남습니다."
            : "스키마에서 자동으로 뽑을 수 없는 업무 개념을 정의합니다."
        }
      />

      <div className="steps">
        {STEPS.map((label, i) => (
          <div key={label} className="row" style={{ gap: 8 }}>
            {i > 0 && <span className="step-line" />}
            <div
              className={`step ${i === step ? "active" : i < step ? "done" : ""}`}
              onClick={() => i < step && setStep(i)}
            >
              <span className="dot">{i < step ? "✓" : i + 1}</span>
              {label}
            </div>
          </div>
        ))}
      </div>

      <div className="wizard">
        <div className="card">
          <div className="card-pad stack" style={{ gap: 20 }}>
            {step === 0 && (
              <>
                <div>
                  <div className="card-title">어떤 종류의 지표인가요?</div>
                  <p className="muted">질문이 이 지표에 걸리면 아래 형태로 SQL이 만들어집니다.</p>
                </div>
                <div className="option-cards">
                  <OptionCard
                    selected={form.kind === "aggregate"}
                    icon="Σ"
                    title="집계"
                    desc="숫자 하나를 구합니다 (SUM, COUNT …)"
                    onClick={() => setKind("aggregate")}
                  />
                  <OptionCard
                    selected={form.kind === "projection"}
                    icon="☰"
                    title="조회"
                    desc="컬럼 목록을 가져옵니다 (SELECT a, b, c)"
                    onClick={() => setKind("projection")}
                  />
                  <OptionCard
                    selected={form.kind === "derived"}
                    icon="ƒ"
                    title="파생"
                    desc="집계 지표를 수식으로 잇습니다 ([매출] - [환불])"
                    onClick={() => setKind("derived")}
                  />
                </div>
              </>
            )}

            {step === 1 && (
              <>
                <div className="card-title">어디서 가져올까요?</div>
                {systems.length === 0 ? (
                  <Alert tone="warn">
                    스키마를 동기화한 시스템이 없습니다. 먼저 시스템을 등록하고 동기화하세요.
                  </Alert>
                ) : (
                  <div className="form-grid">
                    <Field label="시스템" required>
                      <select
                        className="select"
                        value={dsId}
                        disabled={editing}
                        onChange={(e) => {
                          setDsId(e.target.value);
                          patch({
                            table_name: "",
                            joins: [],
                            measures: form.kind === "aggregate" ? COUNT_ALL : [],
                            series: null,
                            fixed_filters: [],
                          });
                        }}
                      >
                        <option value="">선택하세요</option>
                        {systems.map((d) => (
                          <option key={d.id} value={d.id}>
                            {d.name} ({d.driver})
                          </option>
                        ))}
                      </select>
                    </Field>
                    <Field label="기본 테이블" required>
                      <select
                        className="select"
                        value={form.table_name}
                        disabled={!dsId}
                        onChange={(e) =>
                          patch({
                            table_name: e.target.value,
                            joins: [],
                            measures: form.kind === "aggregate" ? COUNT_ALL : [],
                            series: null,
                            fixed_filters: [],
                          })
                        }
                      >
                        {tables.map((t) => (
                          <option key={t.name} value={t.name}>
                            {t.name}
                            {t.description ? ` — ${t.description}` : ""}
                          </option>
                        ))}
                      </select>
                    </Field>
                  </div>
                )}

                {dsId && form.table_name && (
                  <Field
                    label="조인"
                    hint="등록된 관계가 있으면 ON 조건이 자동으로 채워집니다. 관계는 '시스템 › 관계'에서 관리합니다."
                  >
                    <div className="stack" style={{ gap: 10 }}>
                      {form.joins.map((j, i) => {
                        const earlier = scopeNames.slice(0, i + 1);
                        const related = relatedTables(earlier);
                        return (
                          <div key={i} className="filter-card">
                            <div className="row" style={{ marginBottom: 10 }}>
                              <strong>조인 {i + 1}</strong>
                              <span className="spacer" />
                              <Segmented
                                value={j.type}
                                options={[
                                  { value: "inner", label: "INNER" },
                                  { value: "left", label: "LEFT" },
                                ]}
                                onChange={(v) => {
                                  const next = [...form.joins];
                                  next[i] = { ...j, type: v };
                                  setJoins(next);
                                }}
                              />
                              <Button
                                size="sm"
                                variant="danger"
                                onClick={() => setJoins(form.joins.filter((_, k) => k !== i))}
                              >
                                삭제
                              </Button>
                            </div>
                            <select className="select" value={j.table} onChange={(e) => setJoinTable(i, e.target.value)}>
                              <option value="">테이블 선택</option>
                              {tables
                                .filter((t) => t.name === j.table || !scopeNames.includes(t.name))
                                .sort((a, b) => Number(related.has(b.name)) - Number(related.has(a.name)))
                                .map((t) => (
                                  <option key={t.name} value={t.name}>
                                    {related.has(t.name) ? "★ " : ""}
                                    {t.name}
                                    {t.description ? ` — ${t.description}` : ""}
                                  </option>
                                ))}
                            </select>
                            {j.on.map((p, k) => (
                              <div key={k} className="filter-grid" style={{ marginTop: 8, gridTemplateColumns: "1fr auto 1fr auto" }}>
                                <select
                                  className="select"
                                  value={p.left}
                                  onChange={(e) => {
                                    const next = [...form.joins];
                                    const on = [...j.on];
                                    on[k] = { ...p, left: e.target.value };
                                    next[i] = { ...j, on };
                                    setJoins(next);
                                  }}
                                >
                                  <option value="">앞 테이블 컬럼</option>
                                  {columnsOf(earlier).map((c) => (
                                    <option key={c}>{c}</option>
                                  ))}
                                </select>
                                <span>=</span>
                                <select
                                  className="select"
                                  value={p.right}
                                  onChange={(e) => {
                                    const next = [...form.joins];
                                    const on = [...j.on];
                                    on[k] = { ...p, right: e.target.value };
                                    next[i] = { ...j, on };
                                    setJoins(next);
                                  }}
                                >
                                  <option value="">{j.table || "조인 테이블"} 컬럼</option>
                                  {columnsOf([j.table]).map((c) => (
                                    <option key={c}>{c}</option>
                                  ))}
                                </select>
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  disabled={j.on.length === 1}
                                  onClick={() => {
                                    const next = [...form.joins];
                                    next[i] = { ...j, on: j.on.filter((_, x) => x !== k) };
                                    setJoins(next);
                                  }}
                                >
                                  ✕
                                </Button>
                              </div>
                            ))}
                            <div style={{ marginTop: 8 }}>
                              <Button
                                size="sm"
                                variant="ghost"
                                onClick={() => {
                                  const next = [...form.joins];
                                  next[i] = { ...j, on: [...j.on, { left: "", right: "" }] };
                                  setJoins(next);
                                }}
                              >
                                + ON 조건
                              </Button>
                            </div>
                          </div>
                        );
                      })}
                      <div>
                        <Button size="sm" onClick={addJoin} disabled={scopeNames.length >= tables.length}>
                          + 조인 추가
                        </Button>
                      </div>
                    </div>
                  </Field>
                )}

                {dsId && form.table_name && form.kind !== "derived" && (
                  <Field
                    label={form.kind === "aggregate" ? "측정값" : "조회 컬럼"}
                    required
                    hint={
                      form.kind === "aggregate"
                        ? "결과 숫자마다 식을 만듭니다. 'ƒ 감싸기'로 함수를 겹치면 MAX − MIN, ROUND(AVG(…)), 집계 안의 집계(BY)를 만들 수 있습니다."
                        : "결과 컬럼마다 식을 만듭니다. 집계 함수는 조회한 행 전체(또는 BY 묶음)의 값이 각 행에 붙습니다. PREV·DELTA 로 직전 행과 비교합니다."
                    }
                  >
                    <MeasureList
                      kind={form.kind}
                      measures={form.measures}
                      metricName={form.name}
                      columns={columnOptions}
                      baseColumns={baseColumns.map((c) => c.value)}
                      onChange={setMeasures}
                    />
                  </Field>
                )}

                {dsId && form.table_name && seriesUsed.size > 0 && form.series && (
                  <SeriesEditor
                    series={form.series}
                    used={seriesUsed}
                    columns={baseColumns.map((c) => ({ ...c, value: bare(c.value) }))}
                    onChange={(series) => patch({ series })}
                  />
                )}

                {dsId && form.table_name && form.kind === "derived" && (
                  <Field
                    label="수식"
                    required
                    hint="[지표명] 과 숫자, + - * / ( ) 를 씁니다. 나눗셈은 0으로 나누지 않도록 자동 처리됩니다. 구성 지표는 이 지표의 테이블 범위에 있는 집계형만 쓸 수 있습니다."
                  >
                    <input
                      className="input"
                      value={form.expression ?? ""}
                      onChange={(e) => patch({ expression: e.target.value })}
                      placeholder="[취소주문수] / [주문수]"
                    />
                    <div className="chip-box" style={{ marginTop: 8 }}>
                      {aggregateMetrics.length === 0 ? (
                        <span className="muted">이 테이블 범위에 집계형 지표가 없습니다. 먼저 집계형 지표를 등록하세요.</span>
                      ) : (
                        aggregateMetrics.map((m) => (
                          <Chip
                            key={m.id}
                            onClick={() => patch({ expression: `${form.expression ?? ""}${form.expression ? " " : ""}[${m.name}]` })}
                          >
                            [{m.name}]
                          </Chip>
                        ))
                      )}
                    </div>
                    {unknownRefs.length > 0 && (
                      <Alert tone="warn">쓸 수 없는 지표: {unknownRefs.join(", ")}</Alert>
                    )}
                  </Field>
                )}
              </>
            )}

            {step === 2 && (
              <>
                <div>
                  <div className="card-title">고정 필터</div>
                  <p className="muted">
                    질문에 언급되지 않아도 SQL에 항상 붙는 조건입니다. 없으면 건너뛰어도 됩니다.
                  </p>
                </div>

                {form.fixed_filters.map((f, i) => {
                  const src = f.source ?? "literal";
                  return (
                    <div key={i} className="filter-card">
                      <div className="row" style={{ marginBottom: 10 }}>
                        <strong>필터 {i + 1}</strong>
                        <span className="spacer" />
                        <Segmented
                          value={src}
                          options={SOURCES.map((s) => ({ value: s.value, label: s.label }))}
                          onChange={(v) =>
                            setFilter(i, { source: v, value: v === "relative" ? "-7d" : "" })
                          }
                        />
                        <Button
                          size="sm"
                          variant="danger"
                          onClick={() =>
                            patch({ fixed_filters: form.fixed_filters.filter((_, j) => j !== i) })
                          }
                        >
                          삭제
                        </Button>
                      </div>
                      <div className="filter-grid">
                        <select
                          className="select"
                          value={f.field ?? ""}
                          onChange={(e) => {
                            // 컬럼이 바뀌면 출처 기본값도 다시 정한다
                            const fresh = newFilter(e.target.value, typeOf(e.target.value));
                            const next = [...form.fixed_filters];
                            next[i] = fresh;
                            patch({ fixed_filters: next });
                          }}
                        >
                          {columnOptions.map((c) => (
                            <option key={c.value} value={c.value}>
                              {c.value}
                            </option>
                          ))}
                        </select>
                        <select
                          className="select"
                          value={f.operator ?? "equals"}
                          onChange={(e) =>
                            setFilter(i, isNoValue(e.target.value)
                              ? { operator: e.target.value, value: null, source: "literal" }
                              : { operator: e.target.value })
                          }
                        >
                          {OPERATORS.map((o) => (
                            <option key={o.value} value={o.value}>
                              {o.label} ({o.symbol})
                            </option>
                          ))}
                        </select>
                        {isNoValue(f.operator) ? (
                          <input className="input" value="값 없음" disabled />
                        ) : src === "question" ? (
                          <input className="input" value="질문에서 받음" disabled />
                        ) : (
                          <input
                            className="input"
                            value={f.value ?? ""}
                            onChange={(e) => setFilter(i, { value: e.target.value })}
                            placeholder={src === "relative" ? "-7d · -1m · 0d" : "값"}
                          />
                        )}
                      </div>
                      <div className="field-hint" style={{ marginTop: 8 }}>
                        {SOURCES.find((s) => s.value === src)?.hint}
                      </div>
                    </div>
                  );
                })}

                <div>
                  <Button
                    disabled={columnOptions.length === 0}
                    onClick={() => {
                      const first = columnOptions[0];
                      patch({
                        fixed_filters: [
                          ...form.fixed_filters,
                          newFilter(first?.value ?? "", first?.type ?? ""),
                        ],
                      });
                    }}
                  >
                    + 필터 추가
                  </Button>
                </div>
              </>
            )}

            {step === 3 && (
              <>
                <div className="card-title">이름과 설명</div>
                <Field label="이름" required hint="질문에 자주 나오는 말로 짓습니다. 파생 지표의 수식에서 [이름]으로 부릅니다.">
                  <input
                    className="input"
                    value={form.name}
                    onChange={(e) => patch({ name: e.target.value })}
                    placeholder="매출"
                    autoFocus
                  />
                </Field>
                <Field
                  label="같은 말"
                  hint="이 지표를 부르는 다른 말 (쉼표로 구분). 지표 선택 때 LLM 이 함께 봅니다."
                >
                  <input
                    className="input"
                    value={form.synonyms.join(", ")}
                    onChange={(e) => patch({ synonyms: e.target.value.split(",").map((s) => s.trimStart()) })}
                    onBlur={() => patch({ synonyms: form.synonyms.map((s) => s.trim()).filter(Boolean) })}
                    placeholder="운행시간, 엔진 시간"
                  />
                </Field>
                <Field
                  label="설명"
                  hint="LLM 이 지표를 고를 때 읽습니다. 이름을 되풀이하지 말고 무엇을 세는지 적으세요."
                >
                  <textarea
                    className="textarea"
                    rows={3}
                    value={form.description}
                    onChange={(e) => patch({ description: e.target.value })}
                    placeholder="계약이 살아있는 건의 매출액 합계"
                  />
                </Field>
              </>
            )}
          </div>

          <div className="form-actions" style={{ justifyContent: "space-between" }}>
            <Button variant="ghost" disabled={step === 0} onClick={() => setStep(step - 1)}>
              이전
            </Button>
            {step < STEPS.length - 1 ? (
              <Button variant="primary" disabled={!stepValid[step]} onClick={() => setStep(step + 1)}>
                다음
              </Button>
            ) : (
              <Button variant="primary" disabled={!stepValid.every(Boolean) || saving} onClick={handleSave}>
                {saving ? "저장 중..." : editing ? "수정 저장" : "지표 등록"}
              </Button>
            )}
          </div>
        </div>

        <aside className="card preview">
          <div className="card-head">
            <span className="card-title">미리보기</span>
          </div>
          <div className="preview-body">
            <SqlBlock sql={preview?.sql ? formatSql(preview.sql) : definitionLines(form)} />
            {preview?.error && <Alert tone="warn">{preview.error}</Alert>}
            {preview?.sql && questionFilters > 0 && (
              <div className="field-hint">질문에서 받는 조건 {questionFilters}개는 값이 없어 미리보기에서 빠졌습니다.</div>
            )}
            <dl className="kv" style={{ margin: 0, gridTemplateColumns: "72px 1fr" }}>
              <dt>종류</dt>
              <dd>{KIND_LABEL[form.kind]}</dd>
              <dt>조인</dt>
              <dd>{form.joins.length}개</dd>
              <dt>필터</dt>
              <dd>
                {form.fixed_filters.length}개
                {questionFilters > 0 && <span className="muted"> (질문 값 {questionFilters}개)</span>}
              </dd>
              {form.name && (
                <>
                  <dt>이름</dt>
                  <dd>{form.name}</dd>
                </>
              )}
            </dl>
          </div>
        </aside>
      </div>
    </div>
  );
}
