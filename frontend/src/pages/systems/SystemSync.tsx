import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import * as api from "../../api";
import { Alert, Badge, Button, EmptyState, Field, Loading, PageHeader } from "../../components/ui";
import { errorMessage, formatAgo, formatDateTime } from "../../lib/format";

type Result = { tone: "info" | "ok" | "warn" | "danger"; text: string };

export function SystemSync() {
  const [params, setParams] = useSearchParams();
  const [items, setItems] = useState<api.System[] | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [result, setResult] = useState<Result | null>(null);

  const selectedId = params.get("ds") ?? "";
  const selected = items?.find((d) => d.id === selectedId) ?? null;

  useEffect(() => {
    api
      .listSystems()
      .then(setItems)
      .catch((err) => {
        setItems([]);
        setResult({ tone: "danger", text: "시스템을 불러오지 못했습니다: " + errorMessage(err) });
      });
  }, []);

  async function run() {
    if (!selected) return;
    try {
      setSyncing(true);
      setResult({ tone: "info", text: "대상 DB에 접속해 스키마를 읽는 중입니다..." });
      const r = await api.syncSystem(selected.id);
      // 접속은 됐는데 0건이면 스키마 이름이나 Oracle owner 오타일 때가 많다.
      // 빨갛지 않아서 제일 오래 붙잡게 되는 실패라 경고로 띄운다.
      setResult(
        r.table_count === 0
          ? {
              tone: "warn",
              text: "접속은 됐지만 읽은 테이블이 0개입니다. 스키마 이름(Oracle은 owner)이 맞는지 확인하세요.",
            }
          : r.broken_metrics.length > 0
            ? {
                tone: "warn",
                text:
                  `테이블 ${r.table_count}개, 컬럼 ${r.column_count}개를 읽었습니다. 깨진 지표 ${r.broken_metrics.length}개 — ` +
                  r.broken_metrics.map((b) => `${b.name} (${b.reason})`).join(", "),
              }
            : {
                tone: "ok",
                text:
                  `테이블 ${r.table_count}개, 컬럼 ${r.column_count}개` +
                  (r.relation_count !== null ? `, FK 관계 ${r.relation_count}개` : " (FK는 읽지 못해 기존 관계를 유지)") +
                  "를 읽었습니다",
              }
      );
      setItems(await api.listSystems());
    } catch (err) {
      setResult({ tone: "danger", text: "동기화 실패: " + errorMessage(err) });
    } finally {
      setSyncing(false);
    }
  }

  if (items === null) return <Loading />;

  return (
    <div className="page narrow">
      <PageHeader
        title="스키마 동기화"
        desc="대상 DB의 테이블과 컬럼을 읽어 옵니다. 스키마가 바뀌면 다시 실행하세요."
      />

      {items.length === 0 ? (
        <div className="card">
          <EmptyState
            title="동기화할 시스템이 없습니다"
            action={
              <Link to="/systems/new">
                <Button variant="primary">시스템 등록</Button>
              </Link>
            }
          />
        </div>
      ) : (
        <div className="card">
          <div className="card-pad stack">
            <Field label="시스템">
              <select
                className="select"
                value={selectedId}
                disabled={syncing}
                onChange={(e) => {
                  setResult(null);
                  setParams(e.target.value ? { ds: e.target.value } : {});
                }}
              >
                <option value="">선택하세요</option>
                {items.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name} ({d.driver})
                  </option>
                ))}
              </select>
            </Field>

            {selected && (
              <dl className="kv" style={{ margin: 0 }}>
                <dt>접속 정보</dt>
                <dd>
                  <code>
                    {selected.host}:{selected.port || "기본"}/{selected.db_name}
                    {selected.db_schema ? `.${selected.db_schema}` : ""}
                  </code>
                </dd>
                <dt>현재 상태</dt>
                <dd>
                  {selected.synced_at ? (
                    <>
                      <Badge tone="ok">동기화됨</Badge>{" "}
                      <span className="muted">
                        {formatDateTime(selected.synced_at)} ({formatAgo(selected.synced_at)}) · 테이블{" "}
                        {selected.table_count}개
                      </span>
                    </>
                  ) : (
                    <Badge tone="danger">스키마 없음</Badge>
                  )}
                </dd>
              </dl>
            )}

            {result && <Alert tone={result.tone}>{result.text}</Alert>}
          </div>
          <div className="form-actions">
            <Button variant="primary" onClick={run} disabled={!selected || syncing}>
              {syncing ? "동기화 중..." : selected?.synced_at ? "다시 동기화" : "동기화 시작"}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
