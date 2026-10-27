import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import * as api from "../../../api";
import { Alert, Badge, Button, EmptyState, Field, Loading, PageHeader } from "../../../components/ui";
import { SystemPicker, useSystemParam } from "../../../components/SystemPicker";
import { useToast } from "../../../components/Toast";
import { errorMessage, formatDateTime } from "../../../lib/format";
import { RUN_STATUS_LABEL, RUN_STATUS_TONE, usePolling } from "../../../lib/metricAuto";

export function AutoRegisterPage() {
  const { systems, sysId, setSysId, system } = useSystemParam();
  const [tables, setTables] = useState<api.TableInfo[] | null>(null);
  const [runs, setRuns] = useState<api.AutoRun[]>([]);
  const [picked, setPicked] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const [onlyEmpty, setOnlyEmpty] = useState(true);
  const [maxTables, setMaxTables] = useState(5);
  const [perTable, setPerTable] = useState(3);
  const [allowFullScan, setAllowFullScan] = useState(false);
  const [starting, setStarting] = useState(false);
  const toast = useToast();
  const navigate = useNavigate();

  useEffect(() => {
    setPicked([]);
    setTables(null);
    setRuns([]);
    if (!sysId) return;
    api.listTables(sysId).then(setTables).catch((err) => {
      toast("테이블을 불러오지 못했습니다: " + errorMessage(err), "error");
      setTables([]);
    });
    loadRuns();
  }, [sysId]);

  function loadRuns() {
    if (!sysId) return;
    api.listAutoRuns(sysId).then(setRuns).catch(() => setRuns([]));
  }

  usePolling(runs.some((r) => r.status === "running"), loadRuns);

  function toggle(name: string) {
    setPicked(picked.includes(name) ? picked.filter((n) => n !== name) : [...picked, name]);
  }

  async function start() {
    setStarting(true);
    try {
      const run = await api.startAutoRun(sysId, {
        tables: picked,
        max_tables: maxTables,
        per_table: perTable,
        allow_full_scan: allowFullScan,
      });
      navigate(`/metrics/auto/${sysId}/${run.id}`);
    } catch (err) {
      toast("자동 등록을 시작하지 못했습니다: " + errorMessage(err), "error");
    } finally {
      setStarting(false);
    }
  }

  const keyword = search.trim().toLowerCase();
  const visible = (tables ?? []).filter(
    (t) =>
      (!onlyEmpty || t.metric_count === 0 || picked.includes(t.name)) &&
      (!keyword || t.name.toLowerCase().includes(keyword) || (t.purpose || t.comment).toLowerCase().includes(keyword))
  );
  const running = runs.some((r) => r.status === "running");

  return (
    <div className="page wide">
      <PageHeader
        title="지표 자동 등록"
        desc="LLM 이 테이블마다 지표 후보를 SQL 로 쓰고, 지표 등록과 같은 확인(AST 변환, 컴파일, 결과 비교)을 통과한 것만 지표로 저장합니다. 실패한 후보도 이유와 시도한 SQL 이 남습니다."
      />

      <div className="row" style={{ marginBottom: 16 }}>
        <div style={{ width: 240 }}>
          <SystemPicker systems={systems} value={sysId} onChange={setSysId} />
        </div>
        {system && <span className="muted">지표 {system.metric_count}개, 테이블 {system.table_count}개</span>}
      </div>

      {!sysId ? (
        <div className="card">
          <EmptyState title="시스템을 고르세요" desc="동기화한 시스템만 보입니다." />
        </div>
      ) : (
        <div className="stack">
          <div className="card">
            <div className="card-head">
              <span className="card-title">대상 테이블</span>
              <span className="faint">
                {picked.length > 0 ? `${picked.length}개 고름` : `고르지 않으면 지표가 없는 테이블 중 관계가 많은 것부터 ${maxTables}개`}
              </span>
              <span className="spacer" />
              <input
                className="input"
                style={{ width: 220 }}
                placeholder="테이블 이름, 용도 검색"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              <label className="row" style={{ gap: 6 }}>
                <input type="checkbox" checked={onlyEmpty} onChange={(e) => setOnlyEmpty(e.target.checked)} />
                <span className="muted">지표 없는 테이블만</span>
              </label>
              {picked.length > 0 && (
                <Button size="sm" variant="ghost" onClick={() => setPicked([])}>
                  선택 해제
                </Button>
              )}
            </div>
            {tables === null ? (
              <Loading />
            ) : visible.length === 0 ? (
              <div className="card-pad muted">보여 줄 테이블이 없습니다.</div>
            ) : (
              <div className="table-wrap" style={{ maxHeight: 360, overflowY: "auto" }}>
                <table className="table">
                  <thead>
                    <tr>
                      <th style={{ width: 40 }} />
                      <th>테이블</th>
                      <th>용도</th>
                      <th className="num">컬럼</th>
                      <th className="num">지표</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visible.map((t) => (
                      <tr key={t.id} className="clickable" onClick={() => toggle(t.name)}>
                        <td>
                          <input type="checkbox" checked={picked.includes(t.name)} readOnly />
                        </td>
                        <td>
                          <code>{t.name}</code>
                        </td>
                        <td className="muted">{t.purpose || t.comment || "-"}</td>
                        <td className="num">{t.column_count}</td>
                        <td className="num">{t.metric_count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="card card-pad">
            <div className="form-grid">
              <Field label="테이블마다 지표 후보 수" hint="1 ~ 10개. 후보마다 LLM 이 최대 3번 고쳐 씁니다.">
                <input
                  className="input"
                  type="number"
                  min={1}
                  max={10}
                  value={perTable}
                  onChange={(e) => setPerTable(Number(e.target.value))}
                />
              </Field>
              <Field label="자동으로 고를 테이블 수" hint="테이블을 고르지 않았을 때만 씁니다 (1 ~ 50개).">
                <input
                  className="input"
                  type="number"
                  min={1}
                  max={50}
                  value={maxTables}
                  disabled={picked.length > 0}
                  onChange={(e) => setMaxTables(Number(e.target.value))}
                />
              </Field>
              <Field
                label="전체 훑기 허용"
                full
                hint="끄면 실행 계획이 큰 테이블 전체를 훑는 후보는 대상 DB 에서 결과 비교를 하지 않고 실패로 남깁니다. 운영 DB 에서는 끄십시오."
              >
                <label className="row" style={{ gap: 6 }}>
                  <input type="checkbox" checked={allowFullScan} onChange={(e) => setAllowFullScan(e.target.checked)} />
                  <span>테스트 DB 라서 전체 훑기를 허용합니다</span>
                </label>
              </Field>
            </div>
            {running && <Alert tone="info">이 시스템에서 자동 등록이 돌고 있습니다. 끝난 뒤 다시 시작할 수 있습니다.</Alert>}
            <div className="form-actions">
              <Button variant="primary" disabled={starting || running} onClick={start}>
                {starting ? "시작하는 중..." : "자동 등록 시작"}
              </Button>
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <span className="card-title">실행 기록</span>
              <span className="faint">{runs.length}건</span>
              <span className="spacer" />
              <Button size="sm" variant="ghost" onClick={loadRuns}>
                새로고침
              </Button>
            </div>
            {runs.length === 0 ? (
              <div className="card-pad muted">아직 실행한 적이 없습니다.</div>
            ) : (
              <RunTable runs={runs} onOpen={(r) => navigate(`/metrics/auto/${sysId}/${r.id}`)} />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function RunTable({ runs, onOpen }: { runs: api.AutoRun[]; onOpen: (r: api.AutoRun) => void }) {
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>시작</th>
            <th>상태</th>
            <th className="num">테이블</th>
            <th className="num">후보</th>
            <th className="num">등록</th>
            <th className="num">실패</th>
            <th className="num">건너뜀</th>
            <th className="num">토큰</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id} className="clickable" onClick={() => onOpen(r)}>
              <td>{formatDateTime(r.started_at)}</td>
              <td>
                <Badge tone={RUN_STATUS_TONE[r.status]}>{RUN_STATUS_LABEL[r.status]}</Badge>
              </td>
              <td className="num">
                {r.tables_done} / {r.tables.length}
              </td>
              <td className="num">{r.proposed}</td>
              <td className="num">{r.registered}</td>
              <td className="num">{r.failed}</td>
              <td className="num">{r.skipped}</td>
              <td className="num">{(r.prompt_tokens + r.completion_tokens).toLocaleString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
