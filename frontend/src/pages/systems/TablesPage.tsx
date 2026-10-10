import { useEffect, useState } from "react";
import * as api from "../../api";
import { Button, EmptyState, Loading, PageHeader } from "../../components/ui";
import { SystemPicker, useSystemParam } from "../../components/SystemPicker";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/format";

/** 테이블 용도. 테이블 추론(LLM)이 보는 것은 용도 → 코멘트 순이다. */
export function TablesPage() {
  const { systems, sysId, setSysId } = useSystemParam();
  const [tables, setTables] = useState<api.TableInfo[] | null>(null);
  const [filter, setFilter] = useState("");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const toast = useToast();

  useEffect(() => {
    setTables(null);
    setDrafts({});
    if (!sysId) return;
    api
      .listTables(sysId)
      .then(setTables)
      .catch((err) => {
        toast("테이블을 불러오지 못했습니다: " + errorMessage(err), "error");
        setTables([]);
      });
  }, [sysId]);

  async function savePurpose(t: api.TableInfo) {
    try {
      const saved = await api.setTablePurpose(sysId, t.id, drafts[t.id] ?? t.purpose);
      setTables((prev) => (prev ?? []).map((x) => (x.id === t.id ? saved : x)));
      setDrafts(({ [t.id]: _, ...rest }) => rest);
      toast("용도를 저장했습니다", "ok");
    } catch (err) {
      toast("저장 실패: " + errorMessage(err), "error");
    }
  }

  const shown = (tables ?? []).filter((t) =>
    `${t.name} ${t.purpose} ${t.comment}`.toLowerCase().includes(filter.toLowerCase())
  );

  return (
    <div className="page">
      <PageHeader
        title="테이블"
        desc="질문이 들어오면 LLM 이 테이블마다 용도(없으면 코멘트) 한 줄을 보고 테이블을 고릅니다. 용도를 적어 두면 더 정확해집니다."
        actions={<SystemPicker systems={systems} value={sysId} onChange={setSysId} />}
      />
      {!sysId ? (
        <div className="card">
          <EmptyState title="시스템을 고르세요" />
        </div>
      ) : tables === null ? (
        <Loading />
      ) : (
        <div className="card">
          <div className="card-pad row" style={{ gap: 8 }}>
            <input
              className="input"
              style={{ maxWidth: 280 }}
              placeholder="테이블 이름·용도로 찾기"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
          </div>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>테이블</th>
                  <th>용도 (테이블 추론에 쓰임)</th>
                  <th className="num">지표</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((t) => (
                  <tr key={t.id}>
                    <td>
                      <div className="cell-title">{t.name}</div>
                      {t.comment && <div className="cell-sub">{t.comment}</div>}
                      <div className="cell-sub">컬럼 {t.column_count}개</div>
                    </td>
                    <td style={{ minWidth: 260 }}>
                      <div className="row" style={{ gap: 6 }}>
                        <input
                          className="input"
                          value={drafts[t.id] ?? t.purpose}
                          placeholder="무엇을 기록하는 테이블인지"
                          onChange={(e) => setDrafts((d) => ({ ...d, [t.id]: e.target.value }))}
                        />
                        {drafts[t.id] !== undefined && drafts[t.id] !== t.purpose && (
                          <Button size="sm" variant="primary" onClick={() => savePurpose(t)}>
                            저장
                          </Button>
                        )}
                      </div>
                    </td>
                    <td className="num">{t.metric_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
