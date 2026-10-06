import { Fragment, useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import * as api from "../api";
import { Badge, Button, EmptyState, Loading, PageHeader, Segmented, SqlBlock } from "../components/ui";
import { useToast } from "../components/Toast";
import { errorMessage, formatDateTime } from "../lib/format";

const PAGE = 50;
type View = "all" | "favorite" | "up" | "down";

export function HistoryPage() {
  const [params, setParams] = useSearchParams();
  const dsId = params.get("ds") ?? "";
  const view = (params.get("view") as View) ?? "all";
  const [datasources, setDatasources] = useState<api.Datasource[]>([]);
  const [items, setItems] = useState<api.HistoryEntry[] | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const toast = useToast();
  const navigate = useNavigate();

  useEffect(() => {
    api.listDatasources().then(setDatasources).catch(() => setDatasources([]));
  }, []);

  useEffect(() => {
    setItems(null);
    load(0);
  }, [dsId, view]);

  async function load(offset: number) {
    try {
      const page = await api.listHistory({
        datasourceId: dsId || undefined,
        favorite: view === "favorite" ? true : undefined,
        feedback: view === "up" || view === "down" ? view : undefined,
        limit: PAGE,
        offset,
      });
      setItems((prev) => (offset === 0 ? page : [...(prev ?? []), ...page]));
      setHasMore(page.length === PAGE);
    } catch (err) {
      toast("질문 기록을 불러오지 못했습니다: " + errorMessage(err), "error");
      setItems([]);
    }
  }

  function setFilter(next: { ds?: string; view?: View }) {
    const merged = { ds: next.ds ?? dsId, view: next.view ?? view };
    const p: Record<string, string> = {};
    if (merged.ds) p.ds = merged.ds;
    if (merged.view !== "all") p.view = merged.view;
    setParams(p);
  }

  async function update(entry: api.HistoryEntry, patch: api.HistoryPatch) {
    try {
      const saved = await api.patchHistory(entry.id, patch);
      setItems((prev) => (prev ?? []).map((e) => (e.id === saved.id ? saved : e)));
    } catch (err) {
      toast("저장 실패: " + errorMessage(err), "error");
    }
  }

  async function remove(entry: api.HistoryEntry) {
    if (!confirm("이 질문 기록을 지우시겠습니까?")) return;
    try {
      await api.deleteHistory(entry.id);
      setItems((prev) => (prev ?? []).filter((e) => e.id !== entry.id));
    } catch (err) {
      toast("삭제 실패: " + errorMessage(err), "error");
    }
  }

  async function exportCases() {
    try {
      const data = await api.exportHistory(dsId);
      if (data.cases.length === 0) {
        toast("'맞음'으로 표시한 기록이 없습니다", "error");
        return;
      }
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `eval-${data.datasource}.json`;
      a.click();
      URL.revokeObjectURL(a.href);
      toast(`평가셋 ${data.cases.length}건을 내려받았습니다`, "ok");
    } catch (err) {
      toast("내보내기 실패: " + errorMessage(err), "error");
    }
  }

  return (
    <div className="page wide">
      <PageHeader
        title="질문 기록"
        desc="지난 질문과 결과입니다. 맞음/틀림을 표시해 두면 '맞음' 기록을 평가셋으로 내보내 회귀 테스트에 쓸 수 있습니다."
        actions={
          <Button onClick={exportCases} disabled={!dsId} title={dsId ? "" : "데이터소스를 먼저 고르세요"}>
            평가셋 내보내기
          </Button>
        }
      />

      <div className="row" style={{ marginBottom: 16 }}>
        <select
          className="select"
          style={{ width: 240 }}
          value={dsId}
          onChange={(e) => setFilter({ ds: e.target.value })}
        >
          <option value="">전체 데이터소스</option>
          {datasources.map((d) => (
            <option key={d.id} value={d.id}>
              {d.name}
            </option>
          ))}
        </select>
        <Segmented<View>
          value={view}
          options={[
            { value: "all", label: "전체" },
            { value: "favorite", label: "즐겨찾기" },
            { value: "up", label: "맞음" },
            { value: "down", label: "틀림" },
          ]}
          onChange={(v) => setFilter({ view: v })}
        />
      </div>

      <div className="card">
        {items === null ? (
          <Loading />
        ) : items.length === 0 ? (
          <EmptyState title="질문 기록이 없습니다" desc="SQL 생성 화면에서 질문하면 여기에 쌓입니다." />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: "40%" }}>질문</th>
                  <th>결과</th>
                  <th>데이터소스</th>
                  <th>시각</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {items.map((e) => (
                  <Fragment key={e.id}>
                    <tr className="clickable" onClick={() => setOpen(open === e.id ? null : e.id)}>
                      <td>
                        <div className="cell-title">
                          {e.favorite && "★ "}
                          {e.question}
                        </div>
                        <div className="cell-sub">
                          {e.attempts}번 시도 · {Math.round(e.elapsed_ms)}ms
                        </div>
                      </td>
                      <td>
                        {e.sql ? (
                          <Badge tone="ok">성공</Badge>
                        ) : e.clarification ? (
                          <Badge tone="warn">되물음</Badge>
                        ) : (
                          <Badge tone="danger">실패</Badge>
                        )}{" "}
                        {e.feedback === "up" && <Badge tone="ok">맞음</Badge>}
                        {e.feedback === "down" && <Badge tone="danger">틀림</Badge>}
                      </td>
                      <td>{e.datasource_name}</td>
                      <td className="faint">{formatDateTime(e.created_at)}</td>
                      <td style={{ textAlign: "right", whiteSpace: "nowrap" }} onClick={(ev) => ev.stopPropagation()}>
                        <Button size="sm" variant="ghost" onClick={() => update(e, { favorite: !e.favorite })}>
                          {e.favorite ? "★" : "☆"}
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => navigate(`/ask?ds=${e.datasource_id}&q=${encodeURIComponent(e.question)}`)}
                        >
                          다시 묻기
                        </Button>
                      </td>
                    </tr>
                    {open === e.id && (
                      <tr>
                        <td colSpan={5}>
                          <div className="stack" style={{ gap: 10 }}>
                            {e.sql ? (
                              <SqlBlock sql={e.sql} />
                            ) : (
                              <div className={`alert ${e.clarification ? "warn" : "danger"}`}>
                                {e.clarification ?? e.error}
                              </div>
                            )}
                            {e.ast && <pre className="sql">{JSON.stringify(e.ast)}</pre>}
                            <div className="row">
                              {e.sql && (
                                <>
                                  <Button
                                    size="sm"
                                    variant={e.feedback === "up" ? "primary" : "secondary"}
                                    onClick={() => update(e, { feedback: e.feedback === "up" ? "" : "up" })}
                                  >
                                    맞음
                                  </Button>
                                  <Button
                                    size="sm"
                                    variant={e.feedback === "down" ? "danger" : "secondary"}
                                    onClick={() => update(e, { feedback: e.feedback === "down" ? "" : "down" })}
                                  >
                                    틀림
                                  </Button>
                                </>
                              )}
                              <input
                                className="input"
                                style={{ flex: 1 }}
                                defaultValue={e.feedback_note}
                                placeholder="메모 (무엇이 틀렸는지 등) — 입력 후 Enter"
                                onKeyDown={(ev) => {
                                  if (ev.key === "Enter" && !ev.nativeEvent.isComposing)
                                    update(e, { feedback_note: (ev.target as HTMLInputElement).value });
                                }}
                              />
                              <Button size="sm" variant="danger" onClick={() => remove(e)}>
                                삭제
                              </Button>
                            </div>
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {hasMore && (
        <div style={{ textAlign: "center", marginTop: 12 }}>
          <Button onClick={() => load(items?.length ?? 0)}>더 보기</Button>
        </div>
      )}
    </div>
  );
}
