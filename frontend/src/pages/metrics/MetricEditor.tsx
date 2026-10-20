import { useState, type ReactNode } from "react";
import * as api from "../../api";
import { Alert, Badge, Button, Field, SqlBlock } from "../../components/ui";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/format";
import { CHECK_STAGE_LABEL, formatSql } from "../../lib/metrics";

/**
 * 지표 입력 폼 + 확인 결과. 지표 등록·수정 페이지와 질문 화면의 지표 초안 모달이 같이 쓴다.
 * SQL 을 AST 로 옮기고, 다시 만든 SQL 이 원본과 같은 결과를 내는지 대상 DB 에서 확인한 뒤 저장한다.
 */
export function MetricEditor({
  dsId,
  initial,
  metricId,
  initialCheck,
  systemField,
  notes,
  onSaved,
}: {
  dsId: string;
  initial: api.MetricInput;
  /** 있으면 수정 */
  metricId?: string;
  /** initial.sql 을 이미 확인한 결과 (초안) */
  initialCheck?: api.MetricCheck | null;
  /** 시스템 고르기 — 페이지에서만 */
  systemField?: ReactNode;
  /** 확인 결과 위에 붙이는 것 — 초안의 가정 · 값 샘플 */
  notes?: ReactNode;
  onSaved: (saved: api.Metric) => void;
}) {
  const editing = Boolean(metricId);
  const [form, setForm] = useState<api.MetricInput>(initial);
  const [checking, setChecking] = useState(false);
  const [check, setCheck] = useState<{ dsId: string; sql: string; result: api.MetricCheck } | null>(
    initialCheck ? { dsId, sql: initial.sql, result: initialCheck } : null
  );
  const [saving, setSaving] = useState(false);
  const toast = useToast();

  const patch = (p: Partial<api.MetricInput>) => setForm((prev) => ({ ...prev, ...p }));
  const valid = Boolean(dsId) && form.name.trim() !== "" && form.sql.trim() !== "";
  // 확인한 뒤 SQL 이나 시스템을 바꾸면 결과는 지난 것이다
  const result = check && check.sql === form.sql && check.dsId === dsId ? check.result : null;

  async function runCheck() {
    try {
      setChecking(true);
      setCheck({ dsId, sql: form.sql, result: await api.checkMetric(dsId, form.sql) });
    } catch (err) {
      toast("확인 실패: " + errorMessage(err), "error");
    } finally {
      setChecking(false);
    }
  }

  async function handleSave() {
    const body = { ...form, name: form.name.trim(), synonyms: form.synonyms.map((s) => s.trim()).filter(Boolean) };
    try {
      setSaving(true);
      const saved = editing ? await api.updateMetric(dsId, metricId!, body) : await api.createMetric(dsId, body);
      toast(editing ? `지표를 수정했습니다 (v${saved.version})` : "지표를 등록했습니다", "ok");
      onSaved(saved);
    } catch (err) {
      toast((editing ? "지표 수정 실패: " : "지표 등록 실패: ") + errorMessage(err), "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="wizard">
      <div className="card">
        <div className="card-pad stack" style={{ gap: 20 }}>
          {systemField}

          <Field
            label="SQL"
            required
            hint="질문에 답하던 SELECT 문 그대로 넣습니다. 기간 조건은 예시 값이면 됩니다 — 질문마다 LLM 이 바꿉니다."
          >
            <textarea
              className="textarea"
              rows={12}
              value={form.sql}
              onChange={(e) => patch({ sql: e.target.value })}
              placeholder={
                "SELECT DATE(collect_datetime + INTERVAL '9 hours') AS day, vehicle_id,\n" +
                "       ABS(MAX(uman_time) - MIN(uman_time)) AS uman_hours\n" +
                "  FROM bms_log\n" +
                " WHERE collect_datetime >= '2026-10-01'\n" +
                " GROUP BY 1, 2"
              }
              style={{ fontFamily: "var(--mono, monospace)" }}
            />
            <div style={{ marginTop: 8 }}>
              <Button size="sm" disabled={!dsId || !form.sql.trim() || checking} onClick={runCheck}>
                {checking ? "확인 중..." : "확인 (AST 변환 · 결과 비교)"}
              </Button>
            </div>
          </Field>

          <Field label="이름" required hint="질문에 자주 나오는 말로 짓습니다.">
            <input
              className="input"
              value={form.name}
              onChange={(e) => patch({ name: e.target.value })}
              placeholder="무인 가동시간"
            />
          </Field>
          <Field label="같은 말" hint="이 지표를 부르는 다른 말 (쉼표로 구분). 테이블 선택 때 LLM 이 함께 봅니다.">
            <input
              className="input"
              value={form.synonyms.join(", ")}
              onChange={(e) => patch({ synonyms: e.target.value.split(",").map((s) => s.trimStart()) })}
              onBlur={() => patch({ synonyms: form.synonyms.map((s) => s.trim()).filter(Boolean) })}
              placeholder="무인 운행시간, 자율 가동"
            />
          </Field>
          <Field label="설명" hint="LLM 이 지표가 필요한 질문인지, 이 지표가 맞는지 판단할 때 읽습니다. 무엇을 어떻게 계산하는지 적으세요.">
            <textarea
              className="textarea"
              rows={3}
              value={form.description}
              onChange={(e) => patch({ description: e.target.value })}
              placeholder="차량별 하루 무인 가동시간. uman_time 은 누적값이라 하루의 MAX - MIN 으로 구한다."
            />
          </Field>
        </div>

        <div className="form-actions" style={{ justifyContent: "flex-end" }}>
          <Button variant="primary" disabled={!valid || saving} onClick={handleSave}>
            {saving ? "확인·저장 중..." : editing ? "수정 저장" : "지표 등록"}
          </Button>
        </div>
      </div>

      <aside className="card preview">
        <div className="card-head">
          <span className="card-title">확인 결과</span>
          {result && (result.error ? <Badge tone="danger">실패</Badge> : <Badge tone="ok">통과</Badge>)}
        </div>
        <div className="preview-body">
          {notes}
          {!result ? (
            <div className="field-hint">
              저장할 때 서버가 다시 확인합니다. 먼저 보려면 '확인' 을 누르세요 — SQL 을 AST 로 옮기고, 다시 만든 SQL 과
              원본을 대상 DB 에서 실행해 결과가 같은지 비교합니다.
            </div>
          ) : (
            <>
              {result.error && (
                <Alert tone="danger">
                  <strong>{CHECK_STAGE_LABEL[result.stage ?? ""] ?? "확인 실패"}</strong>
                  <div style={{ marginTop: 4 }}>{result.error}</div>
                </Alert>
              )}
              {!result.error && <Alert tone="info">원본과 결과가 같습니다 ({(result.rows ?? 0).toLocaleString()}행)</Alert>}
              {result.warnings.map((w) => (
                <Alert key={w} tone="warn">
                  {w}
                </Alert>
              ))}
              {result.tables.length > 0 && (
                <div className="faint">
                  테이블: <code>{result.tables.join(", ")}</code>
                </div>
              )}
              {result.sql && (
                <>
                  <div className="field-label">AST 에서 다시 만든 SQL</div>
                  <SqlBlock sql={formatSql(result.sql)} />
                </>
              )}
              {result.ast && (
                <details>
                  <summary className="faint">AST 보기</summary>
                  <pre className="sql">{JSON.stringify(result.ast, null, 2)}</pre>
                </details>
              )}
            </>
          )}
        </div>
      </aside>
    </div>
  );
}
