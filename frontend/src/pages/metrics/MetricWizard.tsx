import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import * as api from "../../api";
import { Alert, Badge, Button, Chip, Field, Loading, PageHeader, Segmented, SqlBlock } from "../../components/ui";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/format";
import {
  definitionLines,
  dimensionLabel,
  EMPTY_METRIC,
  isNoValue,
  isTemporal,
  metricScope,
  newFilter,
  OPERATORS,
  SOURCES,
  toInput,
} from "../../lib/metrics";
import { byColumn, byKey, exprText, GRAIN_LABEL } from "../../lib/measures";

/** 서버 SQL 을 읽기 좋게 절마다 줄을 바꾼다 */
const formatSql = (sql: string) => sql.replace(/ (FROM|WHERE|GROUP BY|HAVING|ORDER BY|LIMIT|FETCH FIRST) /g, "\n$1 ");

/** 조인이 생기거나 사라질 때 컬럼 참조를 맞춘다.
 *  조인이 있으면 모두 '테이블.컬럼', 없으면 기본 테이블의 컬럼 이름만 쓴다. */
function requalify(form: api.MetricInput, joins: api.MetricJoin[]): api.MetricInput {
  const base = form.table_name;
  const qualify = joins.length > 0;
  const fix = (ref: string) => {
    const dot = ref.indexOf(".");
    if (qualify) return dot < 0 ? `${base}.${ref}` : ref;
    if (dot < 0) return ref;
    return ref.slice(0, dot).toLowerCase() === base.toLowerCase() ? ref.slice(dot + 1) : null;
  };
  const kept = new Set([base.toLowerCase(), ...joins.map((j) => j.table.toLowerCase())]);
  const alive = (ref: string | null): ref is string =>
    ref !== null && (ref.indexOf(".") < 0 || kept.has(ref.slice(0, ref.indexOf(".")).toLowerCase()));
  const dimensions = form.dimensions.flatMap((d): api.ByItem[] => {
    const field = fix(byColumn(d));
    if (!alive(field)) return [];
    return [typeof d === "string" ? field : { ...d, field }];
  });
  return {
    ...form,
    joins,
    dimensions,
    fixed_filters: form.fixed_filters.map((f) => ({ ...f, field: fix(f.field) })).filter((f) => alive(f.field)),
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
  const [formulas, setFormulas] = useState<api.Formula[]>([]);
  const [form, setForm] = useState<api.MetricInput>(EMPTY_METRIC);
  const [loaded, setLoaded] = useState(!editing);
  const [saving, setSaving] = useState(false);
  const [preview, setPreview] = useState<{ sql: string | null; error: string | null } | null>(null);
  const [sql, setSql] = useState("");
  const [parsed, setParsed] = useState<api.FormulaParse | null>(null);
  const [parseError, setParseError] = useState<string | null>(null);
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
      setFormulas([]);
      return;
    }
    Promise.all([api.getSchema(dsId), api.listRelations(dsId), api.listMetrics(dsId), api.listFormulas(dsId)])
      .then(([s, rels, ms, fs]) => {
        setTables(s.tables);
        setRelations(rels);
        setFormulas(fs);
        if (editing) {
          const current = ms.find((m) => m.id === metricId);
          if (!current) toast("수정할 지표를 찾을 수 없습니다", "error");
          else setForm(toInput(current));
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
    return tables.filter((t) => names.includes(t.name)).flatMap((t) => t.columns.map((c) => `${t.name}.${c.name}`));
  }

  // ---- 디멘젼: 컬럼 그대로, 날짜 컬럼은 단위(시·일·주·월·분기·연)로
  const dimensionOptions = columnOptions.flatMap((c) =>
    isTemporal(c.type)
      ? Object.entries(GRAIN_LABEL)
          .filter(([grain]) => grain !== "hour" || !/^date\b/i.test(c.type.trim()) || /^datetime/i.test(c.type.trim()))
          .map(([grain, label]) => ({ item: { field: c.value, grain } as api.ByItem, label: `${c.value} (${label})` }))
      : [{ item: c.value as api.ByItem, label: c.value }]
  );
  const takenDims = new Set(form.dimensions.map(byKey));
  const takenTimeColumns = new Set(form.dimensions.filter((d) => typeof d !== "string").map(byColumn));

  // ---- SQL 로 채우기: 테이블·디멘젼을 채우고, 같은 식의 계산식을 허용한다
  async function fillFromSql() {
    setParseError(null);
    setParsed(null);
    try {
      const result = await api.parseFormulaSql(dsId, sql);
      setParsed(result);
      const table = result.table ? tables.find((t) => t.name.toLowerCase() === result.table!.toLowerCase())?.name : null;
      const matching = formulas.filter((f) => result.formulas.some((p) => p.text === f.text)).map((f) => f.name);
      setForm((prev) => ({
        ...prev,
        ...(table && table !== prev.table_name ? { table_name: table, joins: [], fixed_filters: [] } : {}),
        dimensions: result.dimensions,
        formulas: [...new Set([...prev.formulas, ...matching])],
      }));
    } catch (err) {
      setParseError(errorMessage(err));
    }
  }
  const unregistered = (parsed?.formulas ?? []).filter((p) => !formulas.some((f) => f.text === p.text));

  const joinsValid = form.joins.every((j) => j.table && j.on.length > 0 && j.on.every((p) => p.left && p.right));
  const filtersValid = form.fixed_filters.every(
    (f) => f.field && ((f.source ?? "literal") === "question" || isNoValue(f.operator) || String(f.value ?? "") !== "")
  );
  const valid = Boolean(dsId && form.table_name) && joinsValid && filtersValid && form.name.trim() !== "";

  // 서버가 실제로 컴파일한 SQL — 허용 계산식마다 하나
  useEffect(() => {
    if (!dsId || !form.table_name || !joinsValid) {
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
      const saved = editing ? await api.updateMetric(dsId, metricId!, body) : await api.createMetric(dsId, body);
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
        desc="지표는 테이블 · 디멘젼 · 허용 계산식입니다. 무엇을 조회·집계할지는 질문마다 LLM 이 정합니다."
      />

      <div className="wizard">
        <div className="card">
          <div className="card-pad stack" style={{ gap: 20 }}>
            <div className="card-title">어디서 가져올까요?</div>
            {systems.length === 0 ? (
              <Alert tone="warn">스키마를 동기화한 시스템이 없습니다. 먼저 시스템을 등록하고 동기화하세요.</Alert>
            ) : (
              <div className="form-grid">
                <Field label="시스템" required>
                  <select
                    className="select"
                    value={dsId}
                    disabled={editing}
                    onChange={(e) => {
                      setDsId(e.target.value);
                      patch({ table_name: "", joins: [], dimensions: [], formulas: [], fixed_filters: [] });
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
                    onChange={(e) => patch({ table_name: e.target.value, joins: [], dimensions: [], fixed_filters: [] })}
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

            {dsId && (
              <Field
                label="SQL 로 채우기"
                hint="쓰던 SELECT 문을 붙여 넣으면 테이블·디멘젼(가장 안쪽 GROUP BY)을 채우고, 같은 식의 계산식을 허용합니다."
              >
                <textarea
                  className="textarea"
                  rows={3}
                  value={sql}
                  onChange={(e) => setSql(e.target.value)}
                  placeholder="SELECT DATE(collect_datetime), vehicle_id, ABS(MAX(uman_time) - MIN(uman_time)) FROM bms_log GROUP BY 1, 2"
                  style={{ fontFamily: "var(--mono, monospace)" }}
                />
                <div style={{ marginTop: 8 }}>
                  <Button size="sm" disabled={!sql.trim()} onClick={fillFromSql}>
                    채우기
                  </Button>
                </div>
                {parseError && <Alert tone="warn">{parseError}</Alert>}
                {unregistered.length > 0 && (
                  <Alert tone="info">
                    등록되지 않은 식: {unregistered.map((p) => p.text).join(", ")} — '계산식' 화면에서 먼저 등록하면 여기서
                    허용할 수 있습니다.
                  </Alert>
                )}
              </Field>
            )}

            {dsId && form.table_name && (
              <Field label="조인" hint="등록된 관계가 있으면 ON 조건이 자동으로 채워집니다. 관계는 '시스템 › 관계'에서 관리합니다.">
                <div className="stack" style={{ gap: 10 }}>
                  {form.joins.map((j, i) => {
                    const earlier = scopeNames.slice(0, i + 1);
                    const related = relatedTables(earlier);
                    const setOn = (k: number, side: "left" | "right", value: string) => {
                      const next = [...form.joins];
                      const on = [...j.on];
                      on[k] = { ...on[k], [side]: value };
                      next[i] = { ...j, on };
                      setJoins(next);
                    };
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
                          <Button size="sm" variant="danger" onClick={() => setJoins(form.joins.filter((_, k) => k !== i))}>
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
                            <select className="select" value={p.left} onChange={(e) => setOn(k, "left", e.target.value)}>
                              <option value="">앞 테이블 컬럼</option>
                              {columnsOf(earlier).map((c) => (
                                <option key={c}>{c}</option>
                              ))}
                            </select>
                            <span>=</span>
                            <select className="select" value={p.right} onChange={(e) => setOn(k, "right", e.target.value)}>
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

            {dsId && form.table_name && (
              <Field
                label="디멘젼"
                hint="집계 계산식이 뜻을 갖는 단위입니다 (예: 장비 × 일 — 누적값의 MAX − MIN 이 하루 가동시간이 되는 단위). 질문이 더 큰 기간(월)으로 물으면 이 단위로 계산한 뒤 합칩니다."
              >
                <div className="expr-by">
                  {form.dimensions.map((d) => (
                    <Chip
                      key={byKey(d)}
                      selected
                      onClick={() => patch({ dimensions: form.dimensions.filter((x) => byKey(x) !== byKey(d)) })}
                    >
                      {dimensionLabel(d)} ✕
                    </Chip>
                  ))}
                  <select
                    className="select expr-leaf-select"
                    value=""
                    aria-label="디멘젼 추가"
                    onChange={(e) => {
                      const picked = dimensionOptions.find((o) => byKey(o.item) === e.target.value);
                      if (picked) patch({ dimensions: [...form.dimensions, picked.item] });
                    }}
                  >
                    <option value="">{form.dimensions.length ? "+ 디멘젼 추가" : "디멘젼 고르기"}</option>
                    {dimensionOptions
                      .filter(
                        (o) =>
                          !takenDims.has(byKey(o.item)) && (typeof o.item === "string" || !takenTimeColumns.has(byColumn(o.item)))
                      )
                      .map((o) => (
                        <option key={byKey(o.item)} value={byKey(o.item)}>
                          {o.label}
                        </option>
                      ))}
                  </select>
                </div>
              </Field>
            )}

            {dsId && (
              <Field
                label="허용 계산식"
                hint="질문에 '계산된 <이름>' 이 나오면 그 계산식을 허용한 지표만 고릅니다. 계산식은 '계산식' 화면에서 등록합니다."
              >
                {formulas.length === 0 ? (
                  <span className="muted">이 시스템에 등록된 계산식이 없습니다.</span>
                ) : (
                  <div className="chip-box">
                    {formulas.map((f) => {
                      const on = form.formulas.some((n) => n.toLowerCase() === f.name.toLowerCase());
                      return (
                        <Chip
                          key={f.id}
                          selected={on}
                          onClick={() =>
                            patch({
                              formulas: on
                                ? form.formulas.filter((n) => n.toLowerCase() !== f.name.toLowerCase())
                                : [...form.formulas, f.name],
                            })
                          }
                        >
                          <span title={`${exprText(f.expr)}${f.description ? ` — ${f.description}` : ""}`}>
                            {f.name} <span className="muted">{f.level === "aggregate" ? "집계" : "행"}</span>
                          </span>
                        </Chip>
                      );
                    })}
                  </div>
                )}
              </Field>
            )}

            {dsId && form.table_name && (
              <Field label="고정 필터" hint="질문에 언급되지 않아도 SQL 에 항상 붙는 조건입니다. 없으면 비워 둡니다.">
                <div className="stack" style={{ gap: 10 }}>
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
                            onChange={(v) => setFilter(i, { source: v, value: v === "relative" ? "-7d" : "" })}
                          />
                          <Button
                            size="sm"
                            variant="danger"
                            onClick={() => patch({ fixed_filters: form.fixed_filters.filter((_, j) => j !== i) })}
                          >
                            삭제
                          </Button>
                        </div>
                        <div className="filter-grid">
                          <select
                            className="select"
                            value={f.field ?? ""}
                            onChange={(e) => {
                              const next = [...form.fixed_filters];
                              next[i] = newFilter(e.target.value, typeOf(e.target.value));
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
                              setFilter(
                                i,
                                isNoValue(e.target.value)
                                  ? { operator: e.target.value, value: null, source: "literal" }
                                  : { operator: e.target.value }
                              )
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
                      size="sm"
                      disabled={columnOptions.length === 0}
                      onClick={() => {
                        const first = columnOptions[0];
                        patch({ fixed_filters: [...form.fixed_filters, newFilter(first?.value ?? "", first?.type ?? "")] });
                      }}
                    >
                      + 필터 추가
                    </Button>
                  </div>
                </div>
              </Field>
            )}

            <div className="card-title">이름과 설명</div>
            <Field label="이름" required hint="질문에 자주 나오는 말로 짓습니다.">
              <input
                className="input"
                value={form.name}
                onChange={(e) => patch({ name: e.target.value })}
                placeholder="장비 가동"
              />
            </Field>
            <Field label="같은 말" hint="이 지표를 부르는 다른 말 (쉼표로 구분). 지표 선택 때 LLM 이 함께 봅니다.">
              <input
                className="input"
                value={form.synonyms.join(", ")}
                onChange={(e) => patch({ synonyms: e.target.value.split(",").map((s) => s.trimStart()) })}
                onBlur={() => patch({ synonyms: form.synonyms.map((s) => s.trim()).filter(Boolean) })}
                placeholder="운행시간, 가동 기록"
              />
            </Field>
            <Field label="설명" hint="LLM 이 지표를 고를 때 읽습니다. 이 테이블로 무엇을 알 수 있는지 적으세요.">
              <textarea
                className="textarea"
                rows={3}
                value={form.description}
                onChange={(e) => patch({ description: e.target.value })}
                placeholder="장비별 하루 가동 기록 (무인·유인 가동시간 누적값)"
              />
            </Field>
          </div>

          <div className="form-actions" style={{ justifyContent: "flex-end" }}>
            <Button variant="primary" disabled={!valid || saving} onClick={handleSave}>
              {saving ? "저장 중..." : editing ? "수정 저장" : "지표 등록"}
            </Button>
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
              <dt>조인</dt>
              <dd>{form.joins.length}개</dd>
              <dt>디멘젼</dt>
              <dd>{form.dimensions.map(dimensionLabel).join(", ") || "없음"}</dd>
              <dt>계산식</dt>
              <dd>
                {form.formulas.length === 0
                  ? "없음"
                  : form.formulas.map((n) => (
                      <Badge key={n} tone="primary">
                        {n}
                      </Badge>
                    ))}
              </dd>
              <dt>필터</dt>
              <dd>
                {form.fixed_filters.length}개
                {questionFilters > 0 && <span className="muted"> (질문 값 {questionFilters}개)</span>}
              </dd>
            </dl>
          </div>
        </aside>
      </div>
    </div>
  );
}
