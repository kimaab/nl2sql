import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import * as api from "../../api";
import { Badge, Button, EmptyState, Loading, PageHeader } from "../../components/ui";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/format";

type Draft = { synonyms: string; codes: api.CodeValue[] };

const key = (table: string, column: string) => `${table}\u0000${column}`;

export function GlossaryPage() {
  const [params, setParams] = useSearchParams();
  const dsId = params.get("ds") ?? "";
  const tableName = params.get("table") ?? "";
  const [datasources, setDatasources] = useState<api.Datasource[] | null>(null);
  const [tables, setTables] = useState<api.SchemaTable[]>([]);
  const [saved, setSaved] = useState<Record<string, api.Annotation>>({});
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [loading, setLoading] = useState(false);
  const toast = useToast();

  useEffect(() => {
    api
      .listDatasources()
      .then((list) => {
        const ready = list.filter((d) => d.synced_at);
        setDatasources(ready);
        if (!dsId && ready.length === 1) setParams({ ds: ready[0].id });
      })
      .catch(() => setDatasources([]));
  }, []);

  useEffect(() => {
    setDrafts({});
    if (!dsId) return;
    setLoading(true);
    Promise.all([api.getSchema(dsId), api.listAnnotations(dsId)])
      .then(([s, notes]) => {
        setTables(s.tables);
        setSaved(Object.fromEntries(notes.map((n) => [key(n.table_name, n.column_name), n])));
        if (!tableName && s.tables[0]) setParams({ ds: dsId, table: s.tables[0].name });
      })
      .catch((err) => toast("사전을 불러오지 못했습니다: " + errorMessage(err), "error"))
      .finally(() => setLoading(false));
  }, [dsId]);

  const table = tables.find((t) => t.name === tableName);
  const countFor = (t: string) => Object.values(saved).filter((n) => n.table_name === t).length;

  function draftOf(column: string): Draft {
    const k = key(tableName, column);
    if (drafts[k]) return drafts[k];
    const note = saved[k];
    return { synonyms: (note?.synonyms ?? []).join(", "), codes: note?.codes ?? [] };
  }

  function setDraft(column: string, d: Draft) {
    setDrafts((prev) => ({ ...prev, [key(tableName, column)]: d }));
  }

  async function save(column: string) {
    const d = draftOf(column);
    const codes = d.codes.filter((c) => c.code.trim() || c.label.trim());
    try {
      const note = await api.putAnnotation(dsId, tableName, column, {
        synonyms: d.synonyms.split(",").map((s) => s.trim()).filter(Boolean),
        codes,
      });
      const k = key(tableName, column);
      setSaved((prev) => {
        const next = { ...prev };
        if (note.synonyms.length || note.codes.length) next[k] = note;
        else delete next[k];
        return next;
      });
      setDrafts((prev) => {
        const next = { ...prev };
        delete next[k];
        return next;
      });
      toast(`${column} 저장`, "ok");
    } catch (err) {
      toast("저장 실패: " + errorMessage(err), "error");
    }
  }

  if (datasources === null) return <Loading />;

  return (
    <div className="page wide">
      <PageHeader
        title="용어·코드 사전"
        desc="컬럼에 업무 용어(동의어)와 코드값의 뜻을 적어 두면, 질문의 '배송완료'·'VIP' 같은 말이 알맞은 컬럼과 코드로 바뀝니다. 코드 사전이 있는 컬럼은 사전에 없는 값을 거부합니다."
      />

      <div className="row" style={{ marginBottom: 16 }}>
        <select className="select" style={{ width: 240 }} value={dsId} onChange={(e) => setParams(e.target.value ? { ds: e.target.value } : {})}>
          <option value="">데이터소스 선택</option>
          {datasources.map((d) => (
            <option key={d.id} value={d.id}>
              {d.name}
            </option>
          ))}
        </select>
        {tables.length > 0 && (
          <select className="select" style={{ width: 300 }} value={tableName} onChange={(e) => setParams({ ds: dsId, table: e.target.value })}>
            {tables.map((t) => (
              <option key={t.name} value={t.name}>
                {t.name}
                {t.description ? ` — ${t.description}` : ""}
                {countFor(t.name) ? ` (${countFor(t.name)})` : ""}
              </option>
            ))}
          </select>
        )}
      </div>

      {!dsId ? (
        <div className="card">
          <EmptyState title="데이터소스를 고르세요" />
        </div>
      ) : loading || !table ? (
        <Loading />
      ) : (
        <div className="card">
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: "20%" }}>컬럼</th>
                  <th style={{ width: "25%" }}>동의어 (쉼표로 구분)</th>
                  <th>코드값</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {table.columns.map((c) => {
                  const d = draftOf(c.name);
                  const dirty = Boolean(drafts[key(tableName, c.name)]);
                  return (
                    <tr key={c.name}>
                      <td>
                        <div className="cell-title">
                          {c.name} {saved[key(tableName, c.name)] && <Badge tone="primary">사전</Badge>}
                        </div>
                        <div className="cell-sub">
                          {c.type}
                          {c.description ? ` · ${c.description}` : ""}
                        </div>
                      </td>
                      <td>
                        <input
                          className="input"
                          value={d.synonyms}
                          placeholder="고객명, 이름"
                          onChange={(e) => setDraft(c.name, { ...d, synonyms: e.target.value })}
                        />
                      </td>
                      <td>
                        <div className="stack" style={{ gap: 6 }}>
                          {d.codes.map((code, i) => (
                            <div key={i} className="row" style={{ gap: 6 }}>
                              <input
                                className="input"
                                style={{ width: 90 }}
                                value={code.code}
                                placeholder="03"
                                onChange={(e) =>
                                  setDraft(c.name, { ...d, codes: d.codes.map((x, k) => (k === i ? { ...x, code: e.target.value } : x)) })
                                }
                              />
                              <input
                                className="input"
                                value={code.label}
                                placeholder="배송완료"
                                onChange={(e) =>
                                  setDraft(c.name, { ...d, codes: d.codes.map((x, k) => (k === i ? { ...x, label: e.target.value } : x)) })
                                }
                              />
                              <Button size="sm" variant="ghost" onClick={() => setDraft(c.name, { ...d, codes: d.codes.filter((_, k) => k !== i) })}>
                                ✕
                              </Button>
                            </div>
                          ))}
                          <div>
                            <Button size="sm" variant="ghost" onClick={() => setDraft(c.name, { ...d, codes: [...d.codes, { code: "", label: "" }] })}>
                              + 코드
                            </Button>
                          </div>
                        </div>
                      </td>
                      <td style={{ textAlign: "right" }}>
                        <Button size="sm" variant={dirty ? "primary" : "secondary"} disabled={!dirty} onClick={() => save(c.name)}>
                          저장
                        </Button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
