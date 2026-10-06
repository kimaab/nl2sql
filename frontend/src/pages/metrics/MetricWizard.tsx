import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
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
  newFilter,
  OPERATORS,
  SOURCES,
} from "../../lib/metrics";

const STEPS = ["종류", "대상", "조건", "이름·설명"];

export function MetricWizard() {
  const [params] = useSearchParams();
  const [datasources, setDatasources] = useState<api.Datasource[] | null>(null);
  const [dsId, setDsId] = useState(params.get("ds") ?? "");
  const [tables, setTables] = useState<api.SchemaTable[]>([]);
  const [step, setStep] = useState(0);
  const [form, setForm] = useState<api.MetricInput>(EMPTY_METRIC);
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
      return;
    }
    api
      .getSchema(dsId)
      .then((s) => {
        setTables(s.tables);
        setForm((prev) => ({ ...prev, table_name: prev.table_name || s.tables[0]?.name || "" }));
      })
      .catch((err) => toast("스키마를 불러오지 못했습니다: " + errorMessage(err), "error"));
  }, [dsId]);

  const columns = tables.find((t) => t.name === form.table_name)?.columns ?? [];
  const patch = (p: Partial<api.MetricInput>) => setForm((prev) => ({ ...prev, ...p }));

  function setKind(kind: api.MetricKind) {
    if (kind === "aggregate") {
      patch({ kind, agg_field: "*", agg_function: "COUNT", select_columns: [] });
    } else {
      patch({ kind, agg_field: null, agg_function: null });
    }
  }

  function setFilter(i: number, p: Record<string, any>) {
    const next = [...form.fixed_filters];
    next[i] = { ...next[i], ...p };
    patch({ fixed_filters: next });
  }

  const stepValid = [
    true,
    Boolean(dsId && form.table_name) &&
      (form.kind === "aggregate" || form.select_columns.length > 0),
    form.fixed_filters.every(
      (f) => f.field && ((f.source ?? "literal") === "question" || String(f.value ?? "") !== "")
    ),
    form.name.trim() !== "",
  ];

  async function handleSave() {
    try {
      setSaving(true);
      const created = await api.createMetric(dsId, { ...form, name: form.name.trim() });
      toast("지표를 등록했습니다", "ok");
      navigate(`/metrics/${dsId}/${created.id}`);
    } catch (err) {
      toast("지표 등록 실패: " + errorMessage(err), "error");
    } finally {
      setSaving(false);
    }
  }

  if (datasources === null) return <Loading />;

  const questionFilters = form.fixed_filters.filter((f) => (f.source ?? "literal") === "question").length;

  return (
    <div className="page wide">
      <PageHeader
        back={{ to: "/metrics", label: "지표 목록" }}
        title="새 지표 등록"
        desc="스키마에서 자동으로 뽑을 수 없는 업무 개념을 정의합니다."
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
                        onChange={(e) => {
                          setDsId(e.target.value);
                          patch({ table_name: "", agg_field: "*", select_columns: [], fixed_filters: [] });
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
                    <Field label="테이블" required>
                      <select
                        className="select"
                        value={form.table_name}
                        disabled={!dsId}
                        onChange={(e) =>
                          patch({
                            table_name: e.target.value,
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

                {dsId && form.table_name && form.kind === "aggregate" && (
                  <Field label="집계" hint="COUNT는 컬럼 대신 * 를 쓸 수 있습니다">
                    <div className="agg-sentence">
                      <select
                        className="select"
                        value={form.agg_function ?? "COUNT"}
                        onChange={(e) => patch({ agg_function: e.target.value })}
                      >
                        {AGG_FUNCTIONS.map((f) => (
                          <option key={f}>{f}</option>
                        ))}
                      </select>
                      <span>(</span>
                      <select
                        className="select"
                        value={form.agg_field ?? "*"}
                        onChange={(e) => patch({ agg_field: e.target.value })}
                      >
                        <option value="*">*</option>
                        {columns.map((c) => (
                          <option key={c.name} value={c.name}>
                            {c.name} ({c.type})
                          </option>
                        ))}
                      </select>
                      <span>)</span>
                    </div>
                  </Field>
                )}

                {dsId && form.table_name && form.kind === "projection" && (
                  <Field
                    label="조회 컬럼"
                    required
                    hint="누른 순서대로 SELECT에 들어갑니다. 번호는 순서입니다."
                  >
                    <div className="chip-box">
                      {columns.map((c) => {
                        const idx = form.select_columns.indexOf(c.name);
                        return (
                          <Chip
                            key={c.name}
                            selected={idx >= 0}
                            order={idx + 1}
                            type={c.type}
                            onClick={() =>
                              patch({
                                select_columns:
                                  idx >= 0
                                    ? form.select_columns.filter((x) => x !== c.name)
                                    : [...form.select_columns, c.name],
                              })
                            }
                          >
                            {c.name}
                          </Chip>
                        );
                      })}
                    </div>
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
                            const col = columns.find((c) => c.name === e.target.value);
                            // 컬럼이 바뀌면 출처 기본값도 다시 정한다
                            const fresh = newFilter(e.target.value, col?.type ?? "");
                            const next = [...form.fixed_filters];
                            next[i] = fresh;
                            patch({ fixed_filters: next });
                          }}
                        >
                          {columns.map((c) => (
                            <option key={c.name} value={c.name}>
                              {c.name}
                            </option>
                          ))}
                        </select>
                        <select
                          className="select"
                          value={f.operator ?? "equals"}
                          onChange={(e) => setFilter(i, { operator: e.target.value })}
                        >
                          {OPERATORS.map((o) => (
                            <option key={o.value} value={o.value}>
                              {o.label} ({o.symbol})
                            </option>
                          ))}
                        </select>
                        {src === "question" ? (
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
                    disabled={columns.length === 0}
                    onClick={() => {
                      const first = columns[0];
                      patch({
                        fixed_filters: [
                          ...form.fixed_filters,
                          newFilter(first?.name ?? "", first?.type ?? ""),
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
                <Field label="이름" required hint="영문·숫자·밑줄을 권장합니다">
                  <input
                    className="input"
                    value={form.name}
                    onChange={(e) => patch({ name: e.target.value })}
                    placeholder="active_revenue"
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
              <Button variant="primary" disabled={!stepValid[step] || saving} onClick={handleSave}>
                {saving ? "등록 중..." : "지표 등록"}
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
              <dd>{form.kind === "aggregate" ? "집계" : "조회"}</dd>
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
