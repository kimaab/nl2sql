import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import * as api from "../../api";
import { Alert, Badge, EmptyState, Loading, PageHeader } from "../../components/ui";
import { formatAgo, formatDateTime } from "../../lib/format";

export function SyncHistory() {
  const [params, setParams] = useSearchParams();
  const dsId = params.get("ds") ?? "";
  const [systems, setSystems] = useState<api.System[] | null>(null);
  const [logs, setLogs] = useState<api.SyncLog[] | null>(null);

  useEffect(() => {
    api
      .listSystems()
      // 오래 동기화하지 않은 것, 한 번도 안 한 것이 위로 오게 한다
      .then((list) => setSystems([...list].sort((a, b) => (a.synced_at ?? "").localeCompare(b.synced_at ?? ""))))
      .catch(() => setSystems([]));
  }, []);

  useEffect(() => {
    setLogs(null);
    api.listSyncLogs(dsId || undefined, 100).then(setLogs).catch(() => setLogs([]));
  }, [dsId]);

  // 시스템마다 가장 최근 기록 — 깨진 지표 알림은 마지막 동기화 기준이다
  const latest = new Map<string, api.SyncLog>();
  for (const log of logs ?? []) if (!latest.has(log.system_id)) latest.set(log.system_id, log);
  const alerts = [...latest.values()].filter((l) => l.status === "error" || l.broken_metrics.length > 0);

  return (
    <div className="page wide">
      <PageHeader
        title="동기화 현황"
        desc="시스템별 마지막 상태와 실행 기록입니다. 자동 동기화(AUTO_SYNC_MINUTES)를 켜면 주기 실행도 여기에 'auto'로 남습니다."
      />

      <div className="stack">
        {alerts.map((l) =>
          l.status === "error" ? (
            <Alert key={l.id} tone="danger">
              <strong>{l.system_name}</strong> 마지막 동기화 실패 ({formatDateTime(l.started_at)}): {l.error}
            </Alert>
          ) : (
            <Alert key={l.id} tone="warn">
              <strong>{l.system_name}</strong> 동기화 뒤 깨진 지표 {l.broken_metrics.length}개 —{" "}
              {l.broken_metrics.map((b) => `${b.name} (${b.reason})`).join(", ")}.{" "}
              <Link to={`/metrics?ds=${l.system_id}`}>지표 확인 →</Link>
            </Alert>
          )
        )}

        <div className="card">
          {systems === null ? (
            <Loading />
          ) : systems.length === 0 ? (
            <EmptyState title="시스템이 없습니다" />
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>시스템</th>
                    <th>상태</th>
                    <th>마지막 동기화</th>
                    <th className="num">테이블</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {systems.map((ds) => (
                    <tr key={ds.id} className="clickable" onClick={() => setParams(dsId === ds.id ? {} : { ds: ds.id })}>
                      <td>
                        <div className="cell-title">
                          {dsId === ds.id ? "▸ " : ""}
                          {ds.name}
                        </div>
                        <div className="cell-sub">{ds.driver}</div>
                      </td>
                      <td>
                        {ds.synced_at ? <Badge tone="ok">동기화됨</Badge> : <Badge tone="danger">스키마 없음</Badge>}
                      </td>
                      <td>
                        {ds.synced_at ? (
                          <>
                            {formatDateTime(ds.synced_at)} <span className="faint">({formatAgo(ds.synced_at)})</span>
                          </>
                        ) : (
                          <span className="faint">-</span>
                        )}
                      </td>
                      <td className="num">{ds.synced_at ? ds.table_count : "-"}</td>
                      <td style={{ textAlign: "right" }} onClick={(e) => e.stopPropagation()}>
                        <Link to={`/sync?ds=${ds.id}`}>동기화 →</Link>
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
            <span className="card-title">실행 기록</span>
            <span className="faint">{dsId ? systems?.find((d) => d.id === dsId)?.name : "전체"}</span>
          </div>
          {logs === null ? (
            <Loading />
          ) : logs.length === 0 ? (
            <EmptyState title="동기화 기록이 없습니다" />
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>시각</th>
                    <th>시스템</th>
                    <th>방식</th>
                    <th>결과</th>
                    <th className="num">추가</th>
                    <th className="num">변경</th>
                    <th className="num">삭제</th>
                    <th>깨진 지표 / 오류</th>
                  </tr>
                </thead>
                <tbody>
                  {logs.map((l) => (
                    <tr key={l.id}>
                      <td className="faint">{formatDateTime(l.started_at)}</td>
                      <td>{l.system_name}</td>
                      <td>
                        <Badge tone={l.trigger === "auto" ? "info" : "primary"}>{l.trigger === "auto" ? "자동" : "수동"}</Badge>
                      </td>
                      <td>{l.status === "ok" ? <Badge tone="ok">성공</Badge> : <Badge tone="danger">실패</Badge>}</td>
                      <td className="num">{l.tables_added ?? "-"}</td>
                      <td className="num">{l.tables_changed ?? "-"}</td>
                      <td className="num">{l.tables_removed ?? "-"}</td>
                      <td>
                        {l.error ? (
                          <span className="faint">{l.error}</span>
                        ) : l.broken_metrics.length ? (
                          <span title={l.broken_metrics.map((b) => `${b.name}: ${b.reason}`).join("\n")}>
                            <Badge tone="warn">{l.broken_metrics.map((b) => b.name).join(", ")}</Badge>
                          </span>
                        ) : (
                          <span className="faint">-</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
