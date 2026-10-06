import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import * as api from "../../api";
import { Alert, Badge, EmptyState, Loading, PageHeader } from "../../components/ui";
import { formatAgo, formatDateTime } from "../../lib/format";

export function SyncHistory() {
  const [items, setItems] = useState<api.Datasource[] | null>(null);

  useEffect(() => {
    api
      .listDatasources()
      .then((list) =>
        // 오래 동기화하지 않은 것, 한 번도 안 한 것이 위로 오게 한다
        setItems([...list].sort((a, b) => (a.synced_at ?? "").localeCompare(b.synced_at ?? "")))
      )
      .catch(() => setItems([]));
  }, []);

  return (
    <div className="page">
      <PageHeader
        title="동기화 현황"
        desc="데이터소스별 마지막 동기화 상태입니다. 동기화하지 않았거나 오래된 항목이 위에 표시됩니다."
      />

      <div className="stack">
        <Alert tone="info">
          현재는 마지막 동기화 결과만 기록됩니다. 실행할 때마다의 이력을 보려면 백엔드에 동기화 이력 저장이
          필요합니다.
        </Alert>

        <div className="card">
          {items === null ? (
            <Loading />
          ) : items.length === 0 ? (
            <EmptyState title="데이터소스가 없습니다" />
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>데이터소스</th>
                    <th>상태</th>
                    <th>마지막 동기화</th>
                    <th className="num">테이블</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {items.map((ds) => (
                    <tr key={ds.id}>
                      <td>
                        <div className="cell-title">{ds.name}</div>
                        <div className="cell-sub">{ds.driver}</div>
                      </td>
                      <td>
                        {ds.synced_at ? (
                          <Badge tone="ok">동기화됨</Badge>
                        ) : (
                          <Badge tone="danger">스키마 없음</Badge>
                        )}
                      </td>
                      <td>
                        {ds.synced_at ? (
                          <>
                            {formatDateTime(ds.synced_at)}{" "}
                            <span className="faint">({formatAgo(ds.synced_at)})</span>
                          </>
                        ) : (
                          <span className="faint">-</span>
                        )}
                      </td>
                      <td className="num">{ds.synced_at ? ds.table_count : "-"}</td>
                      <td style={{ textAlign: "right" }}>
                        <Link to={`/sync?ds=${ds.id}`}>동기화 →</Link>
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
