import { Fragment, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import * as api from "../../../api";
import { Alert, Badge, EmptyState, Loading, PageHeader, Segmented, SqlBlock } from "../../../components/ui";
import { useToast } from "../../../components/Toast";
import { errorMessage, formatDateTime } from "../../../lib/format";
import { CHECK_STAGE_LABEL, formatSql } from "../../../lib/metrics";
import {
  AUTO_STAGE_LABEL,
  ITEM_STATUS_LABEL,
  ITEM_STATUS_TONE,
  RUN_STATUS_LABEL,
  RUN_STATUS_TONE,
  usePolling,
} from "../../../lib/metricAuto";

type Filter = "all" | "registered" | "failed" | "skipped";

export function AutoRunDetail() {
  const { dsId = "", runId = "" } = useParams();
  const [run, setRun] = useState<api.AutoRunDetail | null | undefined>(undefined);
  const [open, setOpen] = useState<number | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const toast = useToast();

  function load() {
    api
      .getAutoRun(dsId, runId)
      .then(setRun)
      .catch((err) => {
        toast("실행 기록을 불러오지 못했습니다: " + errorMessage(err), "error");
        setRun(null);
      });
  }

  useEffect(load, [dsId, runId]);
  usePolling(run?.status === "running", load);

  if (run === undefined) {
    return (
      <div className="page">
        <Loading />
      </div>
    );
  }
  if (run === null) {
    return (
      <div className="page">
        <PageHeader back={{ to: "/metrics/auto", label: "지표 자동 등록" }} title="실행 기록을 찾을 수 없습니다" />
      </div>
    );
  }

  const items = filter === "all" ? run.items : run.items.filter((i) => i.status === filter);

  return (
    <div className="page wide">
      <PageHeader
        back={{ to: `/metrics/auto?ds=${dsId}`, label: "지표 자동 등록" }}
        title={`자동 등록 ${formatDateTime(run.started_at)}`}
        desc={`대상 테이블: ${run.tables.join(", ")}`}
      />

      <div className="stack">
        <div className="card card-pad">
          <div className="row wrap" style={{ gap: 16 }}>
            <Badge tone={RUN_STATUS_TONE[run.status]}>{RUN_STATUS_LABEL[run.status]}</Badge>
            <span>
              테이블 {run.tables_done} / {run.tables.length}
            </span>
            <span>후보 {run.proposed}</span>
            <span>등록 {run.registered}</span>
            <span>실패 {run.failed}</span>
            <span>건너뜀 {run.skipped}</span>
            <span className="faint">
              토큰 {run.prompt_tokens.toLocaleString()} + {run.completion_tokens.toLocaleString()}
            </span>
            <span className="faint">
              테이블마다 후보 {run.options.per_table ?? "-"}개, 전체 훑기 {run.options.allow_full_scan ? "허용" : "막음"}
            </span>
            {run.finished_at && <span className="faint">끝: {formatDateTime(run.finished_at)}</span>}
          </div>
          {run.status === "running" && (
            <div className="faint" style={{ marginTop: 8 }}>
              진행 중입니다. 2초마다 다시 읽습니다.
            </div>
          )}
          {run.status === "interrupted" && (
            <div style={{ marginTop: 8 }}>
              <Alert tone="warn">서버가 재시작돼 실행이 멈췄습니다. 남은 테이블은 다시 실행하십시오.</Alert>
            </div>
          )}
          {run.error && (
            <div style={{ marginTop: 8 }}>
              <Alert tone="danger">{run.error}</Alert>
            </div>
          )}
        </div>

        <div className="card">
          <div className="card-head">
            <span className="card-title">후보</span>
            <span className="faint">{items.length}건</span>
            <span className="spacer" />
            <Segmented<Filter>
              value={filter}
              onChange={setFilter}
              options={[
                { value: "all", label: "전체" },
                { value: "registered", label: "등록됨" },
                { value: "failed", label: "실패" },
                { value: "skipped", label: "건너뜀" },
              ]}
            />
          </div>
          {items.length === 0 ? (
            <EmptyState title={run.status === "running" ? "후보를 쓰는 중입니다" : "후보가 없습니다"} />
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>지표</th>
                    <th>테이블</th>
                    <th>결과</th>
                    <th>사유</th>
                    <th className="num">시도</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item) => (
                    <Fragment key={item.id}>
                      <tr className="clickable" onClick={() => setOpen(open === item.id ? null : item.id)}>
                        <td>
                          <div className="cell-title">{item.name || "(이름 없음)"}</div>
                          <div className="cell-sub">{item.description || item.rationale || "-"}</div>
                        </td>
                        <td>
                          <code>{item.table_name}</code>
                        </td>
                        <td>
                          <Badge tone={ITEM_STATUS_TONE[item.status]}>{ITEM_STATUS_LABEL[item.status]}</Badge>
                        </td>
                        <td className="muted" style={{ maxWidth: 420 }}>
                          {item.stage && <b>{AUTO_STAGE_LABEL[item.stage] ?? item.stage}: </b>}
                          {item.reason || (item.status === "registered" ? "등록 확인을 통과했습니다" : "-")}
                        </td>
                        <td className="num">{item.attempts}</td>
                      </tr>
                      {open === item.id && (
                        <tr>
                          <td colSpan={5}>
                            <ItemDetail dsId={dsId} item={item} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
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

function ItemDetail({ dsId, item }: { dsId: string; item: api.AutoItem }) {
  const check = item.check;
  return (
    <div className="stack" style={{ gap: 12, padding: "8px 0" }}>
      <dl className="kv" style={{ margin: 0 }}>
        <dt>고른 이유</dt>
        <dd>{item.rationale || "-"}</dd>
        <dt>설명</dt>
        <dd>{item.description || "-"}</dd>
        {item.synonyms.length > 0 && (
          <>
            <dt>같은 말</dt>
            <dd>{item.synonyms.join(", ")}</dd>
          </>
        )}
        {item.full_scans.length > 0 && (
          <>
            <dt>전체 훑기</dt>
            <dd>
              <code>{item.full_scans.join(", ")}</code>
            </dd>
          </>
        )}
        {item.metric_id && (
          <>
            <dt>등록된 지표</dt>
            <dd>
              <Link to={`/metrics/${dsId}/${item.metric_id}`}>지표 상세 보기</Link>
            </dd>
          </>
        )}
      </dl>

      {item.sql && (
        <div>
          <div className="faint" style={{ marginBottom: 4 }}>
            마지막 SQL
          </div>
          <SqlBlock sql={item.sql} />
        </div>
      )}

      {check && (
        <div className="stack" style={{ gap: 8 }}>
          <div className="faint">마지막 등록 확인</div>
          {check.error ? (
            <Alert tone="danger">
              {CHECK_STAGE_LABEL[check.stage ?? ""] ?? "확인 실패"}: {check.error}
            </Alert>
          ) : (
            <Alert tone="ok">원본 SQL 과 AST 로 만든 SQL 의 결과가 같습니다 ({check.rows?.toLocaleString() ?? "-"}행)</Alert>
          )}
          {check.warnings.map((w, i) => (
            <Alert key={i} tone="warn">
              {w}
            </Alert>
          ))}
          {check.sql && (
            <details>
              <summary className="faint">AST 로 만든 SQL</summary>
              <SqlBlock sql={formatSql(check.sql)} />
            </details>
          )}
          {check.ast && (
            <details>
              <summary className="faint">AST 보기</summary>
              <pre className="sql">{JSON.stringify(check.ast, null, 2)}</pre>
            </details>
          )}
        </div>
      )}

      {item.tries.length > 1 && (
        <details>
          <summary className="faint">시도 {item.tries.length}번 보기</summary>
          <div className="stack" style={{ gap: 10, marginTop: 8 }}>
            {item.tries.map((t, i) => (
              <div key={i} className="stack" style={{ gap: 4 }}>
                <div className="row" style={{ gap: 8 }}>
                  <b>{i + 1}번째</b>
                  {t.error ? (
                    <Badge tone="danger">{AUTO_STAGE_LABEL[t.stage ?? ""] ?? t.stage}</Badge>
                  ) : (
                    <Badge tone="ok">통과</Badge>
                  )}
                  {t.error && <span className="muted">{t.error}</span>}
                </div>
                <SqlBlock sql={t.sql} />
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}
