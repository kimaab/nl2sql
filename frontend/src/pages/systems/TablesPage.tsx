import { Fragment, useEffect, useState } from "react";
import * as api from "../../api";
import { Badge, Button, EmptyState, Loading, PageHeader } from "../../components/ui";
import { SystemPicker, useSystemParam } from "../../components/SystemPicker";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/format";

const CARD_LABEL = { none: "카드 없음", draft: "검수 대기", approved: "승인됨" } as const;
const CARD_TONE = { none: "danger", draft: "warn", approved: "ok" } as const;

/** 테이블 용도와 카드. 테이블 추론(LLM)이 보는 것은 승인된 '한 줄 요약' → 용도 → 코멘트 순이다. */
export function TablesPage() {
  const { systems, sysId, setSysId } = useSystemParam();
  const [tables, setTables] = useState<api.TableInfo[] | null>(null);
  const [filter, setFilter] = useState("");
  const [open, setOpen] = useState<string | null>(null);
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
      toast("용도를 저장했습니다. 카드는 보강 대기열에서 다시 만들어집니다", "ok");
    } catch (err) {
      toast("저장 실패: " + errorMessage(err), "error");
    }
  }

  const shown = (tables ?? []).filter((t) =>
    `${t.name} ${t.purpose} ${t.comment} ${t.card_line}`.toLowerCase().includes(filter.toLowerCase())
  );
  const counts = (tables ?? []).reduce(
    (acc, t) => ({ ...acc, [t.card_status]: (acc[t.card_status] ?? 0) + 1 }),
    {} as Record<string, number>
  );

  return (
    <div className="page">
      <PageHeader
        title="테이블"
        desc="질문이 들어오면 LLM 이 이 목록의 한 줄 요약을 보고 테이블을 고릅니다. 용도를 적어 두면 카드(요약)가 더 정확해집니다."
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
            <span className="spacer" />
            <Badge tone="ok">승인 {counts.approved ?? 0}</Badge>
            <Badge tone="warn">검수 대기 {counts.draft ?? 0}</Badge>
            <Badge tone="danger">카드 없음 {counts.none ?? 0}</Badge>
          </div>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>테이블</th>
                  <th>용도 (사람)</th>
                  <th>한 줄 요약 (테이블 추론에 쓰임)</th>
                  <th>카드</th>
                  <th className="num">지표</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((t) => (
                  <Fragment key={t.id}>
                    <tr>
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
                      <td>{t.card_line || <span className="faint">-</span>}</td>
                      <td>
                        <Badge tone={CARD_TONE[t.card_status]}>{CARD_LABEL[t.card_status]}</Badge>
                        {t.card && (
                          <Button size="sm" variant="ghost" onClick={() => setOpen(open === t.id ? null : t.id)}>
                            {open === t.id ? "접기" : "보기"}
                          </Button>
                        )}
                      </td>
                      <td className="num">{t.metric_count}</td>
                    </tr>
                    {open === t.id && (
                      <tr>
                        <td colSpan={5}>
                          <pre className="card-text">{t.card}</pre>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
