import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import * as api from "../../../api";
import { Alert, Badge, Button, EmptyState, Field, Loading, PageHeader } from "../../../components/ui";
import { SystemPicker, useSystemParam } from "../../../components/SystemPicker";
import { useToast } from "../../../components/Toast";
import { errorMessage, formatDateTime } from "../../../lib/format";
import { STATUS_LABEL, STATUS_TONE } from "../../../lib/metrics";
import { RUN_STATUS_LABEL, RUN_STATUS_TONE, usePolling } from "../../../lib/metricAuto";

export function ValidationPage() {
  const { systems, sysId, setSysId } = useSystemParam();
  const [metrics, setMetrics] = useState<api.Metric[] | null>(null);
  const [runs, setRuns] = useState<api.ValidationRun[]>([]);
  const [picked, setPicked] = useState<string[]>([]);
  const [runSql, setRunSql] = useState(true);
  const [useLlm, setUseLlm] = useState(true);
  const [allowFullScan, setAllowFullScan] = useState(false);
  const [starting, setStarting] = useState(false);
  const toast = useToast();
  const navigate = useNavigate();

  useEffect(() => {
    setPicked([]);
    setMetrics(null);
    setRuns([]);
    if (!sysId) return;
    api.listMetrics(sysId).then(setMetrics).catch((err) => {
      toast("지표를 불러오지 못했습니다: " + errorMessage(err), "error");
      setMetrics([]);
    });
    loadRuns();
  }, [sysId]);

  function loadRuns() {
    if (!sysId) return;
    api.listValidations(sysId).then(setRuns).catch(() => setRuns([]));
  }

  usePolling(runs.some((r) => r.status === "running"), loadRuns);

  async function remove(m: api.Metric) {
    if (!confirm(`지표 '${m.name}'을(를) 삭제하시겠습니까? 정의는 이력에 남습니다.`)) return;
    try {
      await api.deleteMetric(sysId, m.id);
      setMetrics((list) => (list ?? []).filter((x) => x.id !== m.id));
      setPicked((list) => list.filter((id) => id !== m.id));
      toast("삭제했습니다", "ok");
    } catch (err) {
      toast("삭제 실패: " + errorMessage(err), "error");
    }
  }

  function toggle(id: string) {
    setPicked(picked.includes(id) ? picked.filter((p) => p !== id) : [...picked, id]);
  }

  async function start() {
    setStarting(true);
    try {
      const run = await api.startValidation(sysId, {
        metric_ids: picked,
        run_sql: runSql,
        use_llm: useLlm,
        allow_full_scan: allowFullScan,
      });
      navigate(`/metrics/validate/${sysId}/${run.id}`);
    } catch (err) {
      toast("검증을 시작하지 못했습니다: " + errorMessage(err), "error");
    } finally {
      setStarting(false);
    }
  }

  const running = runs.some((r) => r.status === "running");

  return (
    <div className="page wide">
      <PageHeader
        title="지표 검증"
        desc="등록된 지표마다 컴파일, 원본 재변환, 중복, 실행 계획, 결과 비교, 결과 점검, LLM 의미 검토를 돌려 통과, 주의, 실패를 매깁니다. 점검마다 이유와 근거가 남습니다."
      />

      <div className="row" style={{ marginBottom: 16 }}>
        <div style={{ width: 240 }}>
          <SystemPicker systems={systems} value={sysId} onChange={setSysId} />
        </div>
      </div>

      {!sysId ? (
        <div className="card">
          <EmptyState title="시스템을 고르세요" desc="동기화한 시스템만 보입니다." />
        </div>
      ) : (
        <div className="stack">
          <div className="card">
            <div className="card-head">
              <span className="card-title">검증할 지표</span>
              <span className="faint">{picked.length > 0 ? `${picked.length}개 고름` : "검증할 지표를 고르세요"}</span>
              <span className="spacer" />
              {metrics && metrics.length > 0 && picked.length < metrics.length && (
                <Button size="sm" variant="ghost" onClick={() => setPicked(metrics.map((m) => m.id))}>
                  전체 선택
                </Button>
              )}
              {picked.length > 0 && (
                <Button size="sm" variant="ghost" onClick={() => setPicked([])}>
                  선택 해제
                </Button>
              )}
            </div>
            {metrics === null ? (
              <Loading />
            ) : metrics.length === 0 ? (
              <div className="card-pad muted">등록된 지표가 없습니다.</div>
            ) : (
              <div className="table-wrap" style={{ maxHeight: 360, overflowY: "auto" }}>
                <table className="table">
                  <thead>
                    <tr>
                      <th style={{ width: 40 }} />
                      <th>지표</th>
                      <th>테이블</th>
                      <th>상태</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {metrics.map((m) => (
                      <tr key={m.id} className="clickable" onClick={() => toggle(m.id)}>
                        <td>
                          <input type="checkbox" checked={picked.includes(m.id)} readOnly />
                        </td>
                        <td>
                          <div className="cell-title">{m.name}</div>
                          <div className="cell-sub">{m.description || "설명 없음"}</div>
                        </td>
                        <td>
                          <code>{m.tables.join(", ") || "-"}</code>
                        </td>
                        <td>
                          <Badge tone={STATUS_TONE[m.status]}>{STATUS_LABEL[m.status]}</Badge>
                        </td>
                        <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={(e) => {
                              e.stopPropagation();
                              navigate(`/metrics/validate/${sysId}/metric/${m.id}`);
                            }}
                          >
                            검증 이력
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={(e) => {
                              e.stopPropagation();
                              navigate(`/metrics/${sysId}/${m.id}/edit`);
                            }}
                          >
                            수정
                          </Button>
                          <Button
                            size="sm"
                            variant="danger"
                            disabled={running}
                            onClick={(e) => {
                              e.stopPropagation();
                              remove(m);
                            }}
                          >
                            삭제
                          </Button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="card card-pad">
            <div className="form-grid">
              <Field label="대상 DB 점검" hint="실행 계획, 결과 비교, 결과 점검을 대상 DB 에서 돌립니다.">
                <label className="row" style={{ gap: 6 }}>
                  <input type="checkbox" checked={runSql} onChange={(e) => setRunSql(e.target.checked)} />
                  <span>대상 DB 에서 실행합니다</span>
                </label>
              </Field>
              <Field label="LLM 의미 검토" hint="SQL 이 이름, 설명과 맞는지, JOIN 으로 부풀지 않는지 LLM 이 봅니다.">
                <label className="row" style={{ gap: 6 }}>
                  <input type="checkbox" checked={useLlm} onChange={(e) => setUseLlm(e.target.checked)} />
                  <span>LLM 으로 검토합니다</span>
                </label>
              </Field>
              <Field
                label="전체 훑기 허용"
                full
                hint="끄면 실행 계획이 큰 테이블 전체를 훑는 지표는 결과 비교와 결과 점검을 건너뜁니다. 운영 DB 에서는 끄십시오."
              >
                <label className="row" style={{ gap: 6 }}>
                  <input
                    type="checkbox"
                    checked={allowFullScan}
                    disabled={!runSql}
                    onChange={(e) => setAllowFullScan(e.target.checked)}
                  />
                  <span>테스트 DB 라서 전체 훑기를 허용합니다</span>
                </label>
              </Field>
            </div>
            {running && <Alert tone="info">이 시스템에서 검증이 돌고 있습니다. 끝난 뒤 다시 시작할 수 있습니다.</Alert>}
            <div className="form-actions">
              <Button variant="primary" disabled={starting || running || picked.length === 0} onClick={start}>
                {starting ? "시작하는 중..." : picked.length > 0 ? `${picked.length}개 검증` : "지표를 고르세요"}
              </Button>
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <span className="card-title">검증 기록</span>
              <span className="faint">{runs.length}건</span>
              <span className="spacer" />
              <Button size="sm" variant="ghost" onClick={loadRuns}>
                새로고침
              </Button>
            </div>
            {runs.length === 0 ? (
              <div className="card-pad muted">아직 검증한 적이 없습니다.</div>
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>시작</th>
                      <th>상태</th>
                      <th className="num">진행</th>
                      <th className="num">통과</th>
                      <th className="num">주의</th>
                      <th className="num">실패</th>
                      <th>옵션</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.map((r) => (
                      <tr key={r.id} className="clickable" onClick={() => navigate(`/metrics/validate/${sysId}/${r.id}`)}>
                        <td>{formatDateTime(r.started_at)}</td>
                        <td>
                          <Badge tone={RUN_STATUS_TONE[r.status]}>{RUN_STATUS_LABEL[r.status]}</Badge>
                        </td>
                        <td className="num">
                          {r.done} / {r.total}
                        </td>
                        <td className="num">{r.passed}</td>
                        <td className="num">{r.warned}</td>
                        <td className="num">{r.failed}</td>
                        <td className="faint">
                          {[r.options.run_sql ? "대상 DB" : null, r.options.use_llm ? "LLM" : null]
                            .filter(Boolean)
                            .join(", ") || "정적 점검만"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
