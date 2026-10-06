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
  AGG_FUNCTIONS,
  definitionLines,
  EMPTY_METRIC,
  formulaRefs,
  isNoValue,
  isSeries,
  isTemporal,
  KIND_LABEL,
  metricScope,
  newFilter,
  OPERATORS,
  SERIES_FUNCTIONS,
  SOURCES,
  toInput,
} from "../../lib/metrics";

const STEPS = ["종류", "대상", "조건", "예시", "이름·설명"];

type ExampleDraft = { question: string; ast: string };

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
  return {
    ...form,
    joins,
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
  const [datasources, setDatasources] = useState<api.Datasource[] | null>(null);
  const [dsId, setDsId] = useState(editDs ?? params.get("ds") ?? "");
  const [tables, setTables] = useState<api.SchemaTable[]>([]);
  const [relations, setRelations] = useState<api.Relation[]>([]);
  const [metrics, setMetrics] = useState<api.Metric[]>([]);
  const [step, setStep] = useState(editing ? 1 : 0);
  const [form, setForm] = useState<api.MetricInput>(EMPTY_METRIC);
  const [examples, setExamples] = useState<ExampleDraft[]>([]);
  const [loaded, setLoaded] = useState(!editing);
  const [saving, setSaving] = useState(false);
  const toast = useToast();
  const navigate = useNavigate();

  useEffect(() => {
    api
      .listDatasources()
      // 동기화된 것만 쓸 수 있다. 스키마가 없으면 테이블·컬럼을 고를 수 없다.
      .then((list) => setDatasources(list.filter((d) => d.synced_at)))
      .catch((err) => {
        toast("데이터소스를 불러오지 못했습니다: " + errorMessage(err), "error");
        setDatasources([]);
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
            setExamples(
              (current.examples ?? []).map((e) => ({
                question: e.question,
                ast: e.ast ? JSON.stringify(e.ast, null, 2) : "",
              }))
            );
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
  // 시계열 집계는 기본 테이블 컬럼만 쓴다 (LAG 서브쿼리가 기본 테이블만 감싼다)
  const baseColumns = columnOptions
    .filter((c) => c.table === form.table_name)
    .map((c) => ({ ...c, name: c.value.includes(".") ? c.value.slice(c.value.indexOf(".") + 1) : c.value }));

  function setKind(kind: api.MetricKind) {
    if (kind === "aggregate") {
      patch({ kind, agg_field: "*", agg_function: "COUNT", select_columns: [], expression: null, series: null });
    } else if (kind === "projection") {
      patch({ kind, agg_field: null, agg_function: null, expression: null, series: null });
    } else {
      patch({ kind, agg_field: null, agg_function: null, select_columns: [], expression: form.expression ?? "", series: null });
    }
  }

  function setAggFunction(fn: string) {
    if (!isSeries(fn)) {
      patch({
        agg_function: fn,
        agg_field: fn !== "COUNT" && form.agg_field === "*" ? columnOptions[0]?.value ?? null : form.agg_field,
        series: null,
      });
      return;
    }
    // 처음 고를 때 그럴듯한 기본값: 구분은 이름이 _id 로 끝나는 컬럼, 순서는 첫 날짜 컬럼
    const numeric = (type: string) => /int|numeric|decimal|number|real|double|float|serial/i.test(type);
    const current = baseColumns.find((c) => c.value === form.agg_field);
    const keepField =
      current && (fn !== "DELTA_SUM" || numeric(current.type))
        ? current.value
        : (fn === "DELTA_SUM" ? baseColumns.find((c) => numeric(c.type)) : baseColumns[0])?.value ?? null;
    const guessPartition = baseColumns.find((c) => /_?id$/i.test(c.name) && !isTemporal(c.type))?.name ?? "";
    const guessOrder = baseColumns.find((c) => isTemporal(c.type))?.name ?? "";
    patch({
      agg_function: fn,
      agg_field: keepField,
      series: {
        partition_by: form.series?.partition_by || guessPartition,
        order_by: form.series?.order_by || guessOrder,
        baseline: fn === "CHANGE_COUNT" ? form.series?.baseline ?? "0" : null,
      },
    });
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

  // ---- 예시
  function parsedExamples(): { value: api.MetricExample[]; error: string | null } {
    const out: api.MetricExample[] = [];
    for (const [i, e] of examples.entries()) {
      if (!e.question.trim()) return { value: [], error: `예시 ${i + 1}의 질문이 비어 있습니다` };
      if (!e.ast.trim()) {
        out.push({ question: e.question.trim(), ast: null });
        continue;
      }
      try {
        const ast = JSON.parse(e.ast);
        if (typeof ast !== "object" || Array.isArray(ast) || ast === null) throw new Error("객체가 아닙니다");
        out.push({ question: e.question.trim(), ast });
      } catch (err) {
        return { value: [], error: `예시 ${i + 1}의 AST 가 JSON 객체가 아닙니다 (${errorMessage(err)})` };
      }
    }
    return { value: out, error: null };
  }
  const exampleCheck = parsedExamples();

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
      ((form.kind === "aggregate" &&
        (!isSeries(form.agg_function) ||
          Boolean(
            form.series?.partition_by &&
              form.series?.order_by &&
              form.agg_field &&
              form.agg_field !== "*" &&
              (form.agg_function !== "CHANGE_COUNT" || form.series?.baseline?.trim())
          ))) ||
        (form.kind === "projection" && form.select_columns.length > 0) ||
        (form.kind === "derived" && Boolean(form.expression?.trim()) && formulaRefs(form.expression).length > 0)),
    form.fixed_filters.every(
      (f) =>
        f.field &&
        ((f.source ?? "literal") === "question" || isNoValue(f.operator) || String(f.value ?? "") !== "")
    ),
    exampleCheck.error === null,
    form.name.trim() !== "",
  ];

  async function handleSave() {
    if (exampleCheck.error) {
      toast(exampleCheck.error, "error");
      return;
    }
    const body = { ...form, name: form.name.trim(), examples: exampleCheck.value };
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

  if (datasources === null || !loaded) return <Loading />;

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
                {datasources.length === 0 ? (
                  <Alert tone="warn">
                    스키마를 동기화한 데이터소스가 없습니다. 먼저 데이터소스를 등록하고 동기화하세요.
                  </Alert>
                ) : (
                  <div className="form-grid">
                    <Field label="데이터소스" required>
                      <select
                        className="select"
                        value={dsId}
                        disabled={editing}
                        onChange={(e) => {
                          setDsId(e.target.value);
                          patch({ table_name: "", joins: [], agg_field: "*", select_columns: [], fixed_filters: [] });
                        }}
                      >
                        <option value="">선택하세요</option>
                        {datasources.map((d) => (
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
                            agg_field: form.kind === "aggregate" ? "*" : null,
                            select_columns: [],
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
                    hint="등록된 관계가 있으면 ON 조건이 자동으로 채워집니다. 관계는 '데이터소스 › 관계'에서 관리합니다."
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

                {dsId && form.table_name && form.kind === "aggregate" && (
                  <Field
                    label="집계"
                    hint={
                      isSeries(form.agg_function)
                        ? SERIES_FUNCTIONS[form.agg_function!]
                        : "COUNT는 컬럼 대신 * 를 쓸 수 있습니다"
                    }
                  >
                    <div className="agg-sentence">
                      <select
                        className="select"
                        value={form.agg_function ?? "COUNT"}
                        onChange={(e) => setAggFunction(e.target.value)}
                      >
                        {AGG_FUNCTIONS.map((f) => (
                          <option key={f.value} value={f.value}>
                            {f.label}
                          </option>
                        ))}
                      </select>
                      <span>(</span>
                      <select
                        className="select"
                        value={form.agg_field ?? "*"}
                        onChange={(e) => patch({ agg_field: e.target.value })}
                      >
                        {form.agg_function === "COUNT" && <option value="*">*</option>}
                        {(isSeries(form.agg_function) ? baseColumns : columnOptions).map((c) => (
                          <option key={c.value} value={c.value}>
                            {c.value} ({c.type})
                          </option>
                        ))}
                      </select>
                      <span>)</span>
                    </div>
                  </Field>
                )}

                {dsId && form.table_name && form.kind === "aggregate" && isSeries(form.agg_function) && form.series && (
                  <div className="form-grid">
                    <Field label="구분 컬럼" required hint="이 컬럼 값마다 따로 시간순으로 비교합니다 (예: 차량 ID)">
                      <select
                        className="select"
                        value={form.series.partition_by}
                        onChange={(e) => patch({ series: { ...form.series!, partition_by: e.target.value } })}
                      >
                        <option value="">선택</option>
                        {baseColumns.map((c) => (
                          <option key={c.value} value={c.name}>
                            {c.name} ({c.type})
                          </option>
                        ))}
                      </select>
                    </Field>
                    <Field label="순서 컬럼" required hint="직전 행을 정하는 시간 순서 (예: 수집 일시)">
                      <select
                        className="select"
                        value={form.series.order_by}
                        onChange={(e) => patch({ series: { ...form.series!, order_by: e.target.value } })}
                      >
                        <option value="">선택</option>
                        {baseColumns.map((c) => (
                          <option key={c.value} value={c.name}>
                            {c.name} ({c.type})
                          </option>
                        ))}
                      </select>
                    </Field>
                    {form.agg_function === "CHANGE_COUNT" && (
                      <Field label="정상값" required hint="이 값에서 다른 값으로 바뀐 순간을 1건으로 셉니다 (예: 오류 코드 0)">
                        <input
                          className="input"
                          value={form.series.baseline ?? ""}
                          onChange={(e) => patch({ series: { ...form.series!, baseline: e.target.value } })}
                          placeholder="0"
                        />
                      </Field>
                    )}
                  </div>
                )}

                {dsId && form.table_name && form.kind === "projection" && (
                  <Field
                    label="조회 컬럼"
                    required
                    hint="누른 순서대로 SELECT에 들어갑니다. 번호는 순서입니다."
                  >
                    <div className="chip-box">
                      {columnOptions.map((c) => {
                        const idx = form.select_columns.indexOf(c.value);
                        return (
                          <Chip
                            key={c.value}
                            selected={idx >= 0}
                            order={idx + 1}
                            type={c.type}
                            onClick={() =>
                              patch({
                                select_columns:
                                  idx >= 0
                                    ? form.select_columns.filter((x) => x !== c.value)
                                    : [...form.select_columns, c.value],
                              })
                            }
                          >
                            {c.value}
                          </Chip>
                        );
                      })}
                    </div>
                  </Field>
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
                <div>
                  <div className="card-title">예시 질문</div>
                  <p className="muted">
                    사용자가 이 지표를 찾을 법한 질문을 적어 두면 검색이 잘 걸립니다. AST 까지 적으면 모델에게 보여주는
                    모범 답안이 되고, 저장할 때 실제로 컴파일되는지 검사합니다. 없으면 건너뛰어도 됩니다.
                  </p>
                </div>
                {examples.map((e, i) => (
                  <div key={i} className="filter-card stack" style={{ gap: 8 }}>
                    <div className="row">
                      <strong>예시 {i + 1}</strong>
                      <span className="spacer" />
                      <Button size="sm" variant="danger" onClick={() => setExamples(examples.filter((_, k) => k !== i))}>
                        삭제
                      </Button>
                    </div>
                    <input
                      className="input"
                      value={e.question}
                      placeholder="지난달 카드 결제 매출"
                      onChange={(ev) => setExamples(examples.map((x, k) => (k === i ? { ...x, question: ev.target.value } : x)))}
                    />
                    <textarea
                      className="textarea"
                      rows={4}
                      style={{ fontFamily: "var(--mono)" }}
                      value={e.ast}
                      placeholder={`(선택) {"metric": "${form.name || "지표명"}", "filters": [...]}`}
                      onChange={(ev) => setExamples(examples.map((x, k) => (k === i ? { ...x, ast: ev.target.value } : x)))}
                    />
                  </div>
                ))}
                {exampleCheck.error && <Alert tone="warn">{exampleCheck.error}</Alert>}
                <div>
                  <Button onClick={() => setExamples([...examples, { question: "", ast: "" }])}>+ 예시 추가</Button>
                </div>
              </>
            )}

            {step === 4 && (
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
                  label="설명"
                  hint="질문과 이 지표를 이어주는 검색 본문입니다. 사용자가 쓸 법한 표현을 넣어주세요."
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
            <SqlBlock sql={definitionLines(form)} />
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
              <dt>예시</dt>
              <dd>{examples.length}개</dd>
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
