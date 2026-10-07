import { useEffect, useState } from "react";
import * as api from "../../api";
import { Badge, Button, EmptyState, Field, Loading, PageHeader, Segmented } from "../../components/ui";
import { SystemPicker, useSystemParam } from "../../components/SystemPicker";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/format";

type Form = { id: number | null; term: string; synonyms: string; meaning: string; maps: string; global: boolean };
const EMPTY: Form = { id: null, term: "", synonyms: "", meaning: "", maps: "", global: false };

/** "bms_log.eg_time, 지표:일별 가동시간" ↔ [{table, column}, {metric}] */
function parseMaps(text: string): Record<string, string>[] {
  return text
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .map((s): Record<string, string> => {
      if (s.startsWith("지표:")) return { metric: s.slice(3).trim() };
      const [table, column] = s.split(".");
      return column ? { table: table.trim(), column: column.trim() } : { table: table.trim() };
    });
}
const showMaps = (maps: Record<string, string>[]) =>
  maps.map((m) => (m.metric ? `지표:${m.metric}` : [m.table, m.column].filter(Boolean).join("."))).join(", ");

/** 업무 용어 사전. 질문에 나온 용어만 테이블 추론·지표 선택 프롬프트에 실린다. */
export function BusinessGlossaryPage() {
  const { systems, sysId, setSysId } = useSystemParam();
  const [terms, setTerms] = useState<api.GlossaryTerm[] | null>(null);
  const [form, setForm] = useState<Form>(EMPTY);
  const [saving, setSaving] = useState(false);
  const toast = useToast();

  async function load() {
    try {
      setTerms(sysId ? await api.listGlossary(sysId) : await api.listGlossary(null));
    } catch (err) {
      toast("용어를 불러오지 못했습니다: " + errorMessage(err), "error");
      setTerms([]);
    }
  }

  useEffect(() => {
    setTerms(null);
    setForm(EMPTY);
    load();
  }, [sysId]);

  async function save() {
    const body: api.GlossaryInput = {
      term: form.term,
      synonyms: form.synonyms.split(",").map((s) => s.trim()).filter(Boolean),
      meaning: form.meaning,
      maps_to: parseMaps(form.maps),
      status: "approved",
    };
    const scope = form.global || !sysId ? null : sysId;
    try {
      setSaving(true);
      if (form.id === null) await api.createGlossary(scope, body);
      else await api.updateGlossary(scope, form.id, body);
      setForm(EMPTY);
      await load();
      toast("저장했습니다", "ok");
    } catch (err) {
      toast("저장 실패: " + errorMessage(err), "error");
    } finally {
      setSaving(false);
    }
  }

  async function remove(t: api.GlossaryTerm) {
    if (!confirm(`'${t.term}' 용어를 지우시겠습니까?`)) return;
    try {
      await api.deleteGlossary(t.system_id, t.id);
      await load();
    } catch (err) {
      toast("삭제 실패: " + errorMessage(err), "error");
    }
  }

  const edit = (t: api.GlossaryTerm) =>
    setForm({
      id: t.id,
      term: t.term,
      synonyms: t.synonyms.join(", "),
      meaning: t.meaning,
      maps: showMaps(t.maps_to),
      global: t.system_id === null,
    });

  return (
    <div className="page">
      <PageHeader
        title="업무 용어"
        desc="'운행시간 = 가동시간' 처럼 업무에서 쓰는 말과 그 뜻이 가리키는 테이블·컬럼·지표. 질문에 나온 용어만 프롬프트에 실립니다."
        actions={<SystemPicker systems={systems} value={sysId} onChange={setSysId} />}
      />

      <div className="card card-pad" style={{ marginBottom: 16 }}>
        <div className="form-grid">
          <Field label="용어" required>
            <input className="input" value={form.term} onChange={(e) => setForm({ ...form, term: e.target.value })} />
          </Field>
          <Field label="같은 말" hint="쉼표로 구분">
            <input
              className="input"
              value={form.synonyms}
              placeholder="가동시간, 엔진시간"
              onChange={(e) => setForm({ ...form, synonyms: e.target.value })}
            />
          </Field>
          <Field label="뜻">
            <input className="input" value={form.meaning} onChange={(e) => setForm({ ...form, meaning: e.target.value })} />
          </Field>
          <Field label="가리키는 것" hint="테이블.컬럼 또는 지표:이름, 쉼표로 구분">
            <input
              className="input"
              value={form.maps}
              placeholder="bms_log.eg_time, 지표:일별 가동시간"
              onChange={(e) => setForm({ ...form, maps: e.target.value })}
            />
          </Field>
          {sysId && (
            <Field label="범위">
              <Segmented
                options={[
                  { value: "system", label: "이 시스템" },
                  { value: "global", label: "전사 공통" },
                ]}
                value={form.global ? "global" : "system"}
                onChange={(v) => setForm({ ...form, global: v === "global" })}
              />
            </Field>
          )}
        </div>
        <div className="form-actions" style={{ padding: 0, marginTop: 12 }}>
          {form.id !== null && (
            <Button variant="ghost" onClick={() => setForm(EMPTY)}>
              취소
            </Button>
          )}
          <Button variant="primary" disabled={saving || !form.term.trim()} onClick={save}>
            {form.id === null ? "추가" : "수정"}
          </Button>
        </div>
      </div>

      <div className="card">
        {terms === null ? (
          <Loading />
        ) : terms.length === 0 ? (
          <EmptyState title="등록된 용어가 없습니다" />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>용어</th>
                  <th>같은 말</th>
                  <th>뜻</th>
                  <th>가리키는 것</th>
                  <th>범위</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {terms.map((t) => (
                  <tr key={t.id}>
                    <td className="cell-title">{t.term}</td>
                    <td>{t.synonyms.join(", ") || <span className="faint">-</span>}</td>
                    <td>{t.meaning || <span className="faint">-</span>}</td>
                    <td>
                      <code>{showMaps(t.maps_to) || "-"}</code>
                    </td>
                    <td>{t.system_id ? <Badge>시스템</Badge> : <Badge tone="primary">전사</Badge>}</td>
                    <td>
                      <div className="row" style={{ justifyContent: "flex-end" }}>
                        <Button size="sm" variant="ghost" onClick={() => edit(t)}>
                          편집
                        </Button>
                        <Button size="sm" variant="danger" onClick={() => remove(t)}>
                          삭제
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
