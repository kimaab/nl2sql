import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import * as api from "../../api";
import { Badge, Button, EmptyState, Loading, PageHeader } from "../../components/ui";
import { useToast } from "../../components/Toast";
import { errorMessage, formatAgo } from "../../lib/format";

export function SystemList() {
  const [items, setItems] = useState<api.System[] | null>(null);
  const toast = useToast();
  const navigate = useNavigate();

  useEffect(() => {
    load();
  }, []);

  async function load() {
    try {
      setItems(await api.listSystems());
    } catch (err) {
      toast("시스템을 불러오지 못했습니다: " + errorMessage(err), "error");
      setItems([]);
    }
  }

  async function handleDelete(ds: api.System) {
    if (!confirm(`'${ds.name}'을(를) 삭제하시겠습니까? 스키마와 지표도 사라집니다.`)) return;
    try {
      await api.deleteSystem(ds.id);
      toast("삭제했습니다", "ok");
      await load();
    } catch (err) {
      toast("삭제 실패: " + errorMessage(err), "error");
    }
  }

  return (
    <div className="page">
      <PageHeader
        title="시스템"
        desc="질문을 던질 대상 DB입니다. 스키마를 동기화해야 질문에 쓸 수 있습니다."
        actions={
          <Button variant="primary" onClick={() => navigate("/systems/new")}>
            + 새 시스템
          </Button>
        }
      />

      <div className="card">
        {items === null ? (
          <Loading />
        ) : items.length === 0 ? (
          <EmptyState
            title="등록된 시스템이 없습니다"
            desc="DB 연결 정보를 등록하면 스키마를 읽어 질문에 사용할 수 있습니다."
            action={
              <Button variant="primary" onClick={() => navigate("/systems/new")}>
                시스템 등록
              </Button>
            }
          />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>이름</th>
                  <th>드라이버</th>
                  <th>접속 정보</th>
                  <th>상태</th>
                  <th className="num">테이블</th>
                  <th className="num">지표 (활성 / 전체)</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {items.map((ds) => (
                  <tr key={ds.id}>
                    <td>
                      <div className="cell-title">
                        {ds.name} <span className="faint">· {ds.code}</span>
                      </div>
                      {ds.domain_desc && <div className="cell-sub">{ds.domain_desc}</div>}
                    </td>
                    <td>
                      <Badge>{ds.driver}</Badge>
                    </td>
                    <td>
                      <code>
                        {ds.host}:{ds.port || "기본"}/{ds.db_name}
                      </code>
                    </td>
                    <td>
                      {ds.synced_at ? (
                        <Badge tone="ok">동기화됨 · {formatAgo(ds.synced_at)}</Badge>
                      ) : (
                        <Badge tone="danger">스키마 없음</Badge>
                      )}
                    </td>
                    <td className="num">{ds.synced_at ? ds.table_count : "-"}</td>
                    <td className="num">
                      <Link to={`/metrics?ds=${ds.id}`} title="질문에 쓰이는 지표 / 전체">
                        {ds.active_metric_count} / {ds.metric_count}
                      </Link>
                    </td>
                    <td>
                      <div className="row" style={{ justifyContent: "flex-end" }}>
                        <Button size="sm" onClick={() => navigate(`/sync?ds=${ds.id}`)}>
                          동기화
                        </Button>
                        <Button size="sm" onClick={() => navigate(`/tables?ds=${ds.id}`)}>
                          테이블
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => navigate(`/systems/${ds.id}/edit`)}>
                          편집
                        </Button>
                        <Button size="sm" variant="danger" onClick={() => handleDelete(ds)}>
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
