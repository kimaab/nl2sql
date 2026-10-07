import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import * as api from "../../api";
import { Badge, Button, EmptyState, Field, Loading, PageHeader } from "../../components/ui";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/format";

const EMPTY: api.RelationInput = { left_table: "", left_column: "", right_table: "", right_column: "", constraint_name: "" };

export function RelationsPage() {
  const [params, setParams] = useSearchParams();
  const dsId = params.get("ds") ?? "";
  const [systems, setSystems] = useState<api.System[] | null>(null);
  const [tables, setTables] = useState<api.SchemaTable[]>([]);
  const [relations, setRelations] = useState<api.Relation[] | null>(null);
  const [form, setForm] = useState<api.RelationInput>(EMPTY);
  const [saving, setSaving] = useState(false);
  const toast = useToast();

  useEffect(() => {
    api
      .listSystems()
      .then((list) => {
        const ready = list.filter((d) => d.synced_at);
        setSystems(ready);
        if (!dsId && ready.length === 1) setParams({ ds: ready[0].id });
      })
      .catch(() => setSystems([]));
  }, []);

  useEffect(() => {
    setRelations(null);
    setForm(EMPTY);
    if (!dsId) return;
    Promise.all([api.getSchema(dsId), api.listRelations(dsId)])
      .then(([s, rels]) => {
        setTables(s.tables);
        setRelations(rels);
      })
      .catch((err) => {
        toast("관계를 불러오지 못했습니다: " + errorMessage(err), "error");
        setRelations([]);
      });
  }, [dsId]);

  const columnsOf = (table: string) => tables.find((t) => t.name === table)?.columns ?? [];

  async function add() {
    try {
      setSaving(true);
      const created = await api.createRelation(dsId, form);
      setRelations((prev) => [...(prev ?? []), created]);
      setForm(EMPTY);
      toast("관계를 등록했습니다", "ok");
    } catch (err) {
      toast("등록 실패: " + errorMessage(err), "error");
    } finally {
      setSaving(false);
    }
  }

  async function remove(r: api.Relation) {
    if (!confirm(`${r.left_table}.${r.left_column} → ${r.right_table}.${r.right_column} 관계를 지우시겠습니까?`)) return;
    try {
      await api.deleteRelation(dsId, r.id);
      setRelations((prev) => (prev ?? []).filter((x) => x.id !== r.id));
    } catch (err) {
      toast("삭제 실패: " + errorMessage(err), "error");
    }
  }

  if (systems === null) return <Loading />;

  const valid = form.left_table && form.left_column && form.right_table && form.right_column && form.left_table !== form.right_table;

  return (
    <div className="page">
      <PageHeader
        title="테이블 관계"
        desc="질문이 여러 테이블에 걸칠 때 모델은 여기 등록된 관계로만 조인합니다. ON 조건은 모델이 아니라 이 정의에서 옵니다. 대상 DB의 FK는 동기화할 때 자동으로 읽힙니다."
      />

      <div className="row" style={{ marginBottom: 16 }}>
        <select className="select" style={{ width: 260 }} value={dsId} onChange={(e) => setParams(e.target.value ? { ds: e.target.value } : {})}>
          <option value="">시스템 선택</option>
          {systems.map((d) => (
            <option key={d.id} value={d.id}>
              {d.name} ({d.driver})
            </option>
          ))}
        </select>
      </div>

      {!dsId ? (
        <div className="card">
          <EmptyState title="시스템을 고르세요" desc="스키마를 동기화한 시스템만 표시됩니다." />
        </div>
      ) : relations === null ? (
        <Loading />
      ) : (
        <div className="stack">
          <div className="card">
            {relations.length === 0 ? (
              <EmptyState title="등록된 관계가 없습니다" desc="FK가 없는 DB라면 아래에서 직접 등록하세요." />
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>참조하는 쪽</th>
                      <th />
                      <th>참조되는 쪽</th>
                      <th>이름</th>
                      <th>출처</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {relations.map((r) => (
                      <tr key={r.id}>
                        <td>
                          <code>
                            {r.left_table}.{r.left_column}
                          </code>
                        </td>
                        <td className="faint">→</td>
                        <td>
                          <code>
                            {r.right_table}.{r.right_column}
                          </code>
                        </td>
                        <td className="faint">{r.constraint_name}</td>
                        <td>
                          <Badge tone={r.source === "fk" ? "info" : "primary"}>{r.source === "fk" ? "FK" : "수동"}</Badge>
                        </td>
                        <td style={{ textAlign: "right" }}>
                          {r.source === "manual" && (
                            <Button size="sm" variant="danger" onClick={() => remove(r)}>
                              삭제
                            </Button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="card">
            <div className="card-head">
              <span className="card-title">관계 직접 등록</span>
            </div>
            <div className="card-pad stack">
              <div className="form-grid">
                <Field label="참조하는 테이블 (예: 주문)" required>
                  <select
                    className="select"
                    value={form.left_table}
                    onChange={(e) => setForm({ ...form, left_table: e.target.value, left_column: "" })}
                  >
                    <option value="">선택</option>
                    {tables.map((t) => (
                      <option key={t.name}>{t.name}</option>
                    ))}
                  </select>
                </Field>
                <Field label="컬럼" required>
                  <select
                    className="select"
                    value={form.left_column}
                    disabled={!form.left_table}
                    onChange={(e) => setForm({ ...form, left_column: e.target.value })}
                  >
                    <option value="">선택</option>
                    {columnsOf(form.left_table).map((c) => (
                      <option key={c.name}>{c.name}</option>
                    ))}
                  </select>
                </Field>
                <Field label="참조되는 테이블 (예: 회원)" required>
                  <select
                    className="select"
                    value={form.right_table}
                    onChange={(e) => setForm({ ...form, right_table: e.target.value, right_column: "" })}
                  >
                    <option value="">선택</option>
                    {tables
                      .filter((t) => t.name !== form.left_table)
                      .map((t) => (
                        <option key={t.name}>{t.name}</option>
                      ))}
                  </select>
                </Field>
                <Field label="컬럼" required>
                  <select
                    className="select"
                    value={form.right_column}
                    disabled={!form.right_table}
                    onChange={(e) => setForm({ ...form, right_column: e.target.value })}
                  >
                    <option value="">선택</option>
                    {columnsOf(form.right_table).map((c) => (
                      <option key={c.name}>{c.name}</option>
                    ))}
                  </select>
                </Field>
                <Field label="이름" hint="복합 키는 같은 이름으로 여러 줄 등록합니다. 비우면 자동으로 짓습니다." full>
                  <input
                    className="input"
                    value={form.constraint_name}
                    onChange={(e) => setForm({ ...form, constraint_name: e.target.value })}
                    placeholder="FK_ORDER_MEMBER"
                  />
                </Field>
              </div>
            </div>
            <div className="form-actions">
              <Button variant="primary" disabled={!valid || saving} onClick={add}>
                {saving ? "등록 중..." : "관계 등록"}
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
