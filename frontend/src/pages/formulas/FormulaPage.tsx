import { useEffect, useMemo, useState } from "react";
import * as api from "../../api";
import { Alert, Badge, Button, Chip, EmptyState, Field, Loading, PageHeader, Segmented } from "../../components/ui";
import { SystemPicker, useSystemParam } from "../../components/SystemPicker";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/format";
import { exprComplete, exprText, hasAggregate } from "../../lib/measures";
import { NodeEditor, type ColumnOption } from "../metrics/MeasureEditor";

const MODES: { value: api.FormulaMode; label: string; hint: string }[] = [
  {
    value: "forced",
    label: "강제",
    hint: "질문에 '계산된 <이름>' 이 있을 때만 씁니다. 이름만 나오면 원래 컬럼 그대로입니다.",
  },
  {
    value: "organic",
    label: "유기적",
    hint: "질문 뜻에 맞으면 '계산된' 이 없어도 LLM 이 골라 씁니다.",
  },
];

const ROLLUPS: { value: api.Rollup; label: string; hint: string }[] = [
  { value: "SUM", label: "합계", hint: "지표 디멘젼 단위(일)로 계산한 값을 더합니다 (일별 가동시간 → 월 합계)" },
  { value: "AVG", label: "평균", hint: "디멘젼 단위로 계산한 값의 평균 (하루 평균)" },
  { value: "MAX", label: "최대", hint: "디멘젼 단위로 계산한 값 중 가장 큰 값" },
  { value: "MIN", label: "최소", hint: "디멘젼 단위로 계산한 값 중 가장 작은 값" },
  { value: "NONE", label: "다시 계산", hint: "질문 단위로 식을 다시 계산합니다 (월 전체의 MAX − MIN)" },
];

const EMPTY: api.FormulaInput = {
  name: "",
  description: "",
  column_name: null,
  mode: "forced",
  expr: null,
  source_sql: null,
  rollup: null,
};

/** 시스템 계산식 — 데이터를 보지 않으면 알 수 없는 계산을 한 번 등록하고 여러 지표가 허용해 쓴다 */
export function FormulaPage() {
  const { systems, sysId, setSysId, system } = useSystemParam();
  const [formulas, setFormulas] = useState<api.Formula[] | null>(null);
  const [tables, setTables] = useState<api.SchemaTable[]>([]);
  const [editing, setEditing] = useState<api.Formula | "new" | null>(null);
  const toast = useToast();

  async function load() {
    if (!sysId) return;
    try {
      const [list, schema] = await Promise.all([api.listFormulas(sysId), api.getSchema(sysId)]);
      setFormulas(list);
      setTables(schema.tables);
    } catch (err) {
      toast("계산식을 불러오지 못했습니다: " + errorMessage(err), "error");
      setFormulas([]);
    }
  }

  useEffect(() => {
    setFormulas(null);
    setEditing(null);
    load();
  }, [sysId]);

  // 계산식은 테이블에 매이지 않는다 — 시스템의 컬럼 이름을 모두 고를 수 있게
  const columns = useMemo<ColumnOption[]>(() => {
    const seen = new Map<string, ColumnOption>();
    for (const t of tables) for (const c of t.columns) if (!seen.has(c.name)) seen.set(c.name, { value: c.name, type: c.type, table: t.name });
    return [...seen.values()].sort((a, b) => a.value.localeCompare(b.value));
  }, [tables]);

  return (
    <div className="page wide">
      <PageHeader
        title="계산식"
        desc={
          <>
            데이터를 보지 않으면 알 수 없는 계산을 시스템에 한 번 등록합니다 (예: 누적값이라 하루 가동시간은 MAX − MIN).
            질문에 <code>계산된 &lt;이름&gt;</code> 이 나오면 그 계산식을 씁니다. 지표에서 허용해야 질문에 쓰입니다.
          </>
        }
        actions={
          <>
            <SystemPicker systems={systems} value={sysId} onChange={setSysId} />
            <Button variant="primary" disabled={!sysId} onClick={() => setEditing("new")}>
              + 새 계산식
            </Button>
          </>
        }
      />

      {!sysId ? (
        <div className="card">
          <EmptyState title="시스템을 고르세요" desc="계산식은 시스템마다 따로 등록합니다." />
        </div>
      ) : formulas === null ? (
        <Loading />
      ) : (
        <div className="stack">
          {editing && (
            <FormulaForm
              key={editing === "new" ? "new" : editing.id}
              sysId={sysId}
              driver={system?.driver ?? "postgresql"}
              current={editing === "new" ? null : editing}
              columns={columns}
              others={formulas.filter((f) => editing === "new" || f.id !== editing.id).map((f) => f.name)}
              onDone={() => {
                setEditing(null);
                load();
              }}
            />
          )}
          <div className="card">
            {formulas.length === 0 ? (
              <EmptyState title="등록된 계산식이 없습니다" desc="누적값 증가분처럼 LLM 이 알 수 없는 계산부터 등록하세요." />
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th style={{ width: "22%" }}>이름</th>
                      <th>식</th>
                      <th>종류</th>
                      <th>방식</th>
                      <th>쓰는 지표</th>
                    </tr>
                  </thead>
                  <tbody>
                    {formulas.map((f) => (
                      <tr key={f.id} className="clickable" onClick={() => setEditing(f)}>
                        <td>
                          <div className="cell-title">{f.name}</div>
                          <div className="cell-sub">
                            {f.column_name ? `${f.column_name} 대신 · ` : ""}
                            {f.description || "설명 없음"}
                          </div>
                        </td>
                        <td>
                          <code>{f.text}</code>
                        </td>
                        <td>
                          <Badge tone={f.level === "aggregate" ? "primary" : "info"}>
                            {f.level === "aggregate" ? "집계" : "행 단위"}
                          </Badge>
                        </td>
                        <td>
                          <Badge tone={f.mode === "forced" ? "warn" : "ok"}>
                            {MODES.find((m) => m.value === f.mode)?.label}
                          </Badge>
                        </td>
                        <td className="muted">{f.used_by.join(", ") || "없음"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function FormulaForm({
  sysId,
  driver,
  current,
  columns,
  others,
  onDone,
}: {
  sysId: string;
  driver: api.Driver;
  current: api.Formula | null;
  columns: ColumnOption[];
  others: string[];
  onDone: () => void;
}) {
  const [form, setForm] = useState<api.FormulaInput>(
    current
      ? {
          name: current.name,
          description: current.description,
          column_name: current.column_name,
          mode: current.mode,
          expr: current.expr,
          source_sql: current.source_sql,
          rollup: current.rollup,
        }
      : EMPTY
  );
  const [input, setInput] = useState<"sql" | "editor">(current && !current.source_sql ? "editor" : "sql");
  const [sql, setSql] = useState(current?.source_sql ?? "");
  const [parsed, setParsed] = useState<api.FormulaParse | null>(null);
  const [parseError, setParseError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const toast = useToast();
  const patch = (p: Partial<api.FormulaInput>) => setForm((prev) => ({ ...prev, ...p }));

  async function unfold() {
    setParseError(null);
    setParsed(null);
    try {
      const result = await api.parseFormulaSql(sysId, sql);
      setParsed(result);
      if (result.formulas.length === 1) pick(result.formulas[0]);
    } catch (err) {
      setParseError(errorMessage(err));
    }
  }

  function pick(f: api.ParsedFormula) {
    patch({ expr: f.expr, source_sql: sql.trim() || null, name: form.name || f.name || "" });
  }

  const aggregate = form.expr ? hasAggregate(form.expr) : false;
  const valid = form.name.trim() !== "" && form.expr !== null && exprComplete(form.expr);

  async function save() {
    const body = { ...form, name: form.name.trim(), rollup: aggregate ? form.rollup : null };
    try {
      setSaving(true);
      if (current) await api.updateFormula(sysId, current.id, body);
      else await api.createFormula(sysId, body);
      toast(current ? "계산식을 수정했습니다" : "계산식을 등록했습니다", "ok");
      onDone();
    } catch (err) {
      toast("저장 실패: " + errorMessage(err), "error");
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!current || !confirm(`계산식 '${current.name}'을(를) 삭제하시겠습니까?`)) return;
    try {
      await api.deleteFormula(sysId, current.id);
      toast("삭제했습니다", "ok");
      onDone();
    } catch (err) {
      toast("삭제 실패: " + errorMessage(err), "error");
    }
  }

  return (
    <div className="card">
      <div className="card-head">
        <span className="card-title">{current ? `계산식 수정 — ${current.name}` : "새 계산식"}</span>
        <span className="spacer" />
        {current && current.used_by.length > 0 && (
          <span className="faint">쓰는 지표: {current.used_by.join(", ")}</span>
        )}
      </div>
      <div className="card-pad stack" style={{ gap: 16 }}>
        <div className="form-grid">
          <Field label="이름" required hint="질문에서 '계산된 <이름>' 으로 부릅니다. 다른 계산식에서는 [이름] 으로 씁니다.">
            <input
              className="input"
              value={form.name}
              onChange={(e) => patch({ name: e.target.value })}
              placeholder="무인 가동시간"
            />
          </Field>
          <Field label="대신하는 컬럼" hint="이 계산식이 계산해 주는 컬럼 (예: 누적값 컬럼). 없으면 비웁니다.">
            <input
              className="input"
              list="formula-columns"
              value={form.column_name ?? ""}
              onChange={(e) => patch({ column_name: e.target.value || null })}
              placeholder="uman_time"
            />
            <datalist id="formula-columns">
              {columns.map((c) => (
                <option key={c.value} value={c.value} />
              ))}
            </datalist>
          </Field>
        </div>

        <Field label="방식" hint={MODES.find((m) => m.value === form.mode)?.hint}>
          <Segmented
            value={form.mode}
            options={MODES.map((m) => ({ value: m.value, label: m.label }))}
            onChange={(v) => patch({ mode: v })}
          />
        </Field>

        <Field
          label="식"
          required
          hint={
            input === "sql"
              ? "식 하나(ABS(MAX(uman_time) - MIN(uman_time)))나 SELECT 전체를 붙여 넣고 '식으로 풀기' 를 누르세요. 다른 계산식은 [이름] 으로 적습니다."
              : "'ƒ 감싸기' 로 함수를 겹칩니다. 값 자리에서 다른 계산식을 고를 수 있습니다."
          }
        >
          <Segmented
            value={input}
            options={[
              { value: "sql", label: "SQL 로 입력" },
              { value: "editor", label: "편집기" },
            ]}
            onChange={(v) => {
              setInput(v);
              if (v === "editor" && !form.expr) patch({ expr: { fn: "SUM", args: [{ col: columns[0]?.value ?? "" }] } });
            }}
          />
          {input === "sql" ? (
            <div className="stack" style={{ gap: 8, marginTop: 8 }}>
              <textarea
                className="textarea"
                rows={5}
                value={sql}
                onChange={(e) => setSql(e.target.value)}
                placeholder={`ABS(MAX(uman_time) - MIN(uman_time))\n\n또는 ${driver} SELECT 문 전체`}
                style={{ fontFamily: "var(--mono, monospace)" }}
              />
              <div>
                <Button size="sm" disabled={!sql.trim()} onClick={unfold}>
                  식으로 풀기
                </Button>
              </div>
              {parseError && <Alert tone="warn">{parseError}</Alert>}
              {parsed && parsed.formulas.length > 1 && (
                <div className="chip-box">
                  <span className="muted">계산식으로 쓸 식을 고르세요:</span>
                  {parsed.formulas.map((f, i) => (
                    <Chip
                      key={i}
                      selected={form.expr !== null && exprText(form.expr) === f.text}
                      onClick={() => pick(f)}
                    >
                      {f.name ? `${f.name}: ` : ""}
                      {f.text}
                    </Chip>
                  ))}
                </div>
              )}
              {parsed && (parsed.dimensions.length > 0 || parsed.outer.length > 0) && (
                <div className="field-hint">
                  {parsed.dimensions.length > 0 && (
                    <div>
                      묶음 {parsed.dimensions.map((d) => (typeof d === "string" ? d : `${d.field}(${d.grain})`)).join(", ")}
                      {parsed.table ? ` (${parsed.table})` : ""} 은 계산식이 아니라 지표의 디멘젼으로 등록합니다.
                    </div>
                  )}
                  {parsed.outer.length > 0 && <div>바깥 질의({parsed.outer.join(" · ")})는 질문이 정합니다.</div>}
                </div>
              )}
            </div>
          ) : (
            form.expr && (
              <div style={{ marginTop: 8 }}>
                <NodeEditor
                  node={form.expr}
                  ctx={{ kind: "aggregate", columns, baseColumns: columns.map((c) => c.value), formulas: others }}
                  onChange={(expr) => patch({ expr, source_sql: null })}
                />
              </div>
            )
          )}
          {form.expr && (
            <div className="row" style={{ marginTop: 8, gap: 8 }}>
              <code className="expr-text">{exprText(form.expr)}</code>
              <Badge tone={aggregate ? "primary" : "info"}>{aggregate ? "집계" : "행 단위"}</Badge>
            </div>
          )}
        </Field>

        {aggregate && (
          <Field
            label="더 큰 기간으로 볼 때"
            hint={ROLLUPS.find((r) => r.value === (form.rollup ?? "SUM"))?.hint}
          >
            <select
              className="select"
              value={form.rollup ?? "SUM"}
              onChange={(e) => patch({ rollup: e.target.value === "SUM" ? null : (e.target.value as api.Rollup) })}
            >
              {ROLLUPS.map((r) => (
                <option key={r.value} value={r.value}>
                  {r.label}
                </option>
              ))}
            </select>
          </Field>
        )}

        <Field label="설명" hint="지표 선택·SQL 생성 때 LLM 이 읽습니다. 왜 이렇게 계산하는지 적으세요.">
          <textarea
            className="textarea"
            rows={2}
            value={form.description}
            onChange={(e) => patch({ description: e.target.value })}
            placeholder="uman_time 은 장비별 누적값이라 하루 가동시간은 그날의 최댓값 − 최솟값"
          />
        </Field>
      </div>
      <div className="form-actions" style={{ justifyContent: "space-between" }}>
        <div className="row" style={{ gap: 8 }}>
          <Button variant="ghost" onClick={onDone}>
            닫기
          </Button>
          {current && (
            <Button variant="danger" onClick={remove}>
              삭제
            </Button>
          )}
        </div>
        <Button variant="primary" disabled={!valid || saving} onClick={save}>
          {saving ? "저장 중..." : current ? "수정 저장" : "계산식 등록"}
        </Button>
      </div>
    </div>
  );
}
