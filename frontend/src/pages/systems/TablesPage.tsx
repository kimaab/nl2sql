import { useEffect, useState } from "react";
import * as api from "../../api";
import { Link } from "react-router-dom";
import { Badge, Button, EmptyState, Loading, PageHeader } from "../../components/ui";
import { SystemPicker, useSystemParam } from "../../components/SystemPicker";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/format";

/** 테이블 용도와 컬럼 설정 요약. 컬럼 설정(UTC 보정, 계산 필요)은 하위 페이지(TableColumns)에서 한다.
 * 테이블 선택(LLM)이 보는 것은 용도 → 코멘트 순이다. */
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
    `${t.name} ${t.purpose} ${t.comment} ${t.ai_comment}`.toLowerCase().includes(filter.toLowerCase())
  );

  return (
    <div className="page">
      <PageHeader
        title="테이블"
        desc="질문이 들어오면 LLM 이 테이블마다 용도(없으면 코멘트)와 컬럼을 보고 테이블을 고릅니다. 계산 필요 컬럼을 계산해야 답할 수 있는 질문은 지표가 필요한 질문으로 판단합니다."
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
                  <th>용도 (테이블 선택에 쓰임)</th>
                  <th title="DB 에 UTC 로 저장된 시각 컬럼의 현지 시차. 컬럼 설정에서 고릅니다.">UTC 보정</th>
                  <th title="그대로 합치거나 평균내면 틀리는 컬럼 (예: 계속 커지는 누적값). 컬럼 설정에서 표시합니다.">
                    계산 필요 컬럼
                  </th>
                  <th className="num">지표</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {shown.map((t) => (
                  <tr key={t.id}>
                    <td>
                      <div className="cell-title">{t.name}</div>
                      {t.comment ? (
                        <div className="cell-sub">{t.comment}</div>
                      ) : (
                        t.ai_comment && <div className="cell-sub" title="추론한 설명">{t.ai_comment} (추론)</div>
                      )}
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
                    <td>
                      {t.time_columns.filter((c) => c.utc_offset_hours).length === 0 ? (
                        <span className="muted">{t.time_columns.length ? "보정 없음" : "—"}</span>
                      ) : (
                        t.time_columns
                          .filter((c) => c.utc_offset_hours)
                          .map((c) => (
                            <div key={c.name} className="cell-sub">
                              {c.name} UTC{c.utc_offset_hours! > 0 ? "+" : ""}
                              {c.utc_offset_hours}
                            </div>
                          ))
                      )}
                    </td>
                    <td style={{ minWidth: 180 }}>
                      <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                        {t.columns.filter((c) => c.needs_calc).length === 0 ? (
                          <span className="muted">—</span>
                        ) : (
                          t.columns
                            .filter((c) => c.needs_calc)
                            .map((c) => (
                              <Badge key={c.name} tone="warn">
                                <span title={c.note || c.comment || c.ai_comment || c.data_type}>{c.name}</span>
                              </Badge>
                            ))
                        )}
                      </div>
                    </td>
                    <td className="num">{t.metric_count}</td>
                    <td>
                      <Link to={`/tables/${sysId}/${t.id}`} className="btn sm">
                        컬럼 설정
                      </Link>
                    </td>
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
