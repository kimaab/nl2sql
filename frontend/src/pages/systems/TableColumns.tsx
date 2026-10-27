import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import * as api from "../../api";
import { Badge, EmptyState, Loading, PageHeader } from "../../components/ui";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/format";

/** UTC 보정으로 고를 수 있는 시차 (0 은 '보정 없음') */
const OFFSETS = Array.from({ length: 27 }, (_, i) => i - 12).filter((h) => h !== 0);

/** 테이블 하나의 컬럼 설정: 시각 컬럼 UTC 보정, 계산 필요 표시 */
export function TableColumns() {
  const { sysId = "", tableId = "" } = useParams();
  const [table, setTable] = useState<api.TableInfo | null | undefined>(undefined);
  const [filter, setFilter] = useState("");
  const [onlySet, setOnlySet] = useState(false);
  const [saving, setSaving] = useState<string | null>(null);
  const toast = useToast();

  useEffect(() => {
    setTable(undefined);
    api
      .listTables(sysId)
      .then((list) => setTable(list.find((t) => t.id === tableId) ?? null))
      .catch((err) => {
        toast("테이블을 불러오지 못했습니다: " + errorMessage(err), "error");
        setTable(null);
      });
  }, [sysId, tableId]);

  async function saveOffset(column: string, hours: number | null) {
    setSaving(column);
    try {
      setTable(await api.setUtcOffset(sysId, tableId, column, hours));
      toast(hours ? `${column}: UTC${hours > 0 ? "+" : ""}${hours} 로 보정합니다` : `${column}: 보정하지 않습니다`, "ok");
    } catch (err) {
      toast("저장 실패: " + errorMessage(err), "error");
    } finally {
      setSaving(null);
    }
  }

  async function saveNeedsCalc(column: string, needsCalc: boolean) {
    setSaving(column);
    try {
      setTable(await api.setNeedsCalc(sysId, tableId, column, needsCalc));
      toast(needsCalc ? `${column}: 계산 필요로 표시했습니다` : `${column}: 표시를 지웠습니다`, "ok");
    } catch (err) {
      toast("저장 실패: " + errorMessage(err), "error");
    } finally {
      setSaving(null);
    }
  }

  const back = { to: `/tables?ds=${sysId}`, label: "테이블 목록" };
  if (table === undefined) {
    return (
      <div className="page">
        <Loading />
      </div>
    );
  }
  if (table === null) {
    return (
      <div className="page">
        <PageHeader back={back} title="테이블을 찾을 수 없습니다" />
        <div className="card">
          <EmptyState title="삭제되었거나 동기화에서 사라진 테이블입니다" />
        </div>
      </div>
    );
  }

  // 시각 컬럼(시각을 포함한 날짜)만 UTC 보정을 고를 수 있다
  const timeColumns = new Map(table.time_columns.map((c) => [c.name, c]));
  const offsetCount = table.time_columns.filter((c) => c.utc_offset_hours).length;
  const calcCount = table.columns.filter((c) => c.needs_calc).length;
  const word = filter.toLowerCase();
  const shown = table.columns.filter((c) => {
    if (onlySet && !c.needs_calc && !timeColumns.get(c.name)?.utc_offset_hours) return false;
    return `${c.name} ${c.comment} ${c.ai_comment} ${c.data_type}`.toLowerCase().includes(word);
  });

  return (
    <div className="page">
      <PageHeader
        back={back}
        title={table.name}
        desc={
          <>
            {table.purpose || table.comment || table.ai_comment || "용도가 없습니다 (테이블 목록에서 적습니다)"}
            <br />
            UTC 보정: DB 에 UTC 로 저장된 시각 컬럼이면 현지 시차를 고릅니다. 날짜 묶음, 기간 조건, 표시가 현지 시각 기준이
            됩니다. 계산 필요: 그대로 합치거나 평균내면 틀리는 컬럼 (예: 계속 커지는 누적값)입니다. 이 컬럼을 계산해야
            하는 질문은 지표가 있어야 답합니다.
          </>
        }
      />
      <div className="card">
        <div className="card-pad row wrap" style={{ gap: 12 }}>
          <input
            className="input"
            style={{ maxWidth: 280 }}
            placeholder="컬럼 이름, 설명, 타입으로 찾기"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          <label className="row" style={{ gap: 6 }}>
            <input type="checkbox" checked={onlySet} onChange={(e) => setOnlySet(e.target.checked)} />
            설정한 컬럼만
          </label>
          <span className="spacer" />
          <span className="muted">
            컬럼 {table.columns.length}개 / UTC 보정 {offsetCount}개 / 계산 필요 {calcCount}개 / 지표 {table.metric_count}개
          </span>
        </div>
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>컬럼</th>
                <th>타입</th>
                <th>설명</th>
                <th>UTC 보정</th>
                <th>계산 필요</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((c) => {
                const time = timeColumns.get(c.name);
                return (
                  <tr key={c.name}>
                    <td className="cell-title">{c.name}</td>
                    <td className="cell-sub">{c.data_type}</td>
                    <td>
                      {c.comment || (c.ai_comment ? (
                        <span title="추론한 설명">{c.ai_comment} <span className="faint">(추론)</span></span>
                      ) : (
                        <span className="muted">—</span>
                      ))}
                    </td>
                    <td>
                      {time ? (
                        <select
                          className="select"
                          style={{ width: 130 }}
                          value={time.utc_offset_hours ?? ""}
                          disabled={saving === c.name}
                          onChange={(e) => saveOffset(c.name, e.target.value === "" ? null : Number(e.target.value))}
                        >
                          <option value="">보정 없음</option>
                          {OFFSETS.map((h) => (
                            <option key={h} value={h}>
                              UTC{h > 0 ? "+" : ""}
                              {h}
                              {h === 9 ? " (KST)" : ""}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <span className="muted" title="시각을 포함한 날짜 컬럼만 보정합니다">
                          —
                        </span>
                      )}
                    </td>
                    <td>
                      <label className="row" style={{ gap: 6 }}>
                        <input
                          type="checkbox"
                          checked={c.needs_calc}
                          disabled={saving === c.name}
                          onChange={(e) => saveNeedsCalc(c.name, e.target.checked)}
                        />
                        {c.needs_calc && <Badge tone="warn">계산 필요</Badge>}
                      </label>
                    </td>
                  </tr>
                );
              })}
              {shown.length === 0 && (
                <tr>
                  <td colSpan={5}>
                    <EmptyState title={onlySet ? "설정한 컬럼이 없습니다" : "맞는 컬럼이 없습니다"} />
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
