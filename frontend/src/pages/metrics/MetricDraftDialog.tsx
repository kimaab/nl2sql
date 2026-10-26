import { useEffect, useRef, useState } from "react";
import * as api from "../../api";
import { Alert, Loading, Modal } from "../../components/ui";
import { errorMessage } from "../../lib/format";
import { EMPTY_METRIC } from "../../lib/metrics";
import { MetricEditor } from "./MetricEditor";

/**
 * 지표가 없어 멈춘 질문에서 LLM 이 쓴 지표 초안을 등록 폼에 채워 보여준다. 저장해야 지표가 된다.
 * 초안은 대상 컬럼의 값 샘플(인덱스를 타는 조회만)을 보고 쓰고, 등록과 같은 확인을 거친 것이다.
 */
export function MetricDraftDialog({
  dsId,
  question,
  missing,
  onClose,
  onSaved,
}: {
  dsId: string;
  question: string;
  missing: api.MissingMetric;
  onClose: () => void;
  onSaved: (saved: api.Metric) => void;
}) {
  const [draft, setDraft] = useState<api.MetricDraft | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const started = useRef(false);

  useEffect(() => {
    // StrictMode 가 effect 를 두 번 불러도 LLM 은 한 번만 부른다
    if (started.current) return;
    started.current = true;
    api
      .draftMetric(dsId, question, missing)
      .then(setDraft)
      .catch((err) => setFailed(errorMessage(err)));
  }, []);

  const title = `지표 등록 — ${missing.name || missing.table}`;
  if (!draft && !failed) {
    return (
      <Modal title={title} onClose={onClose}>
        <Loading />
        <p className="field-hint" style={{ textAlign: "center" }}>
          값 샘플 조회(인덱스) → LLM 초안 → 등록 확인 순서로 초안을 만듭니다. 30초쯤 걸립니다.
        </p>
      </Modal>
    );
  }

  const initial: api.MetricInput = draft
    ? { name: draft.name || missing.name, description: draft.description, synonyms: draft.synonyms, sql: draft.sql }
    : { ...EMPTY_METRIC, name: missing.name };

  return (
    <Modal title={title} onClose={onClose}>
      <MetricEditor
        dsId={dsId}
        initial={initial}
        initialCheck={draft?.check}
        onSaved={onSaved}
        notes={failed ? <Alert tone="danger">초안을 만들지 못했습니다: {failed}</Alert> : <DraftNotes draft={draft!} />}
      />
    </Modal>
  );
}

function DraftNotes({ draft }: { draft: api.MetricDraft }) {
  const { sample } = draft;
  return (
    <>
      {draft.unsure && <Alert tone="warn">LLM 이 계산 방식을 정하지 못했습니다: {draft.unsure}</Alert>}
      {draft.full_scans.length > 0 && (
        <Alert tone="danger">
          초안 SQL 의 실행 계획이 {draft.full_scans.join(", ")} 전체를 훑어 실행하지 않았습니다. 기간 조건을 인덱스가
          있는 시각 컬럼에 함수 없이 걸어 주세요. '확인' 은 계획을 보지 않고 실행하니, 고친 뒤에 누르세요.
        </Alert>
      )}
      {draft.check && !draft.check.error && draft.check.rows === 0 && (
        <Alert tone="warn">예시 기간에 결과가 없습니다. 계산이 맞는지 볼 수 없으니 기간을 바꿔 확인하세요.</Alert>
      )}
      {draft.assumptions.length > 0 && (
        <Alert tone="danger">
          <strong>지표생성 근거</strong>
          <ul className="draft-list">
            {draft.assumptions.map((a) => (
              <li key={a}>{a}</li>
            ))}
          </ul>
        </Alert>
      )}
      <details open={sample.rows.length > 0}>
        <summary className="faint">값 샘플{sample.index ? ` (인덱스 ${sample.index})` : ""}</summary>
        <div className="field-hint" style={{ margin: "6px 0" }}>
          {sample.note}
        </div>
        {sample.rows.length > 0 && (
          <div className="table-wrap">
            <table className="table sample-table">
              <thead>
                <tr>
                  {sample.columns.map((c) => (
                    <th key={c}>{c}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {sample.rows.map((r, i) => (
                  <tr key={i}>
                    {r.map((v, j) => (
                      <td key={j}>{v}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {sample.queries.length > 0 && (
          <details>
            <summary className="faint">실행한 조회 {sample.queries.length}개</summary>
            <pre className="sql">{sample.queries.join("\n")}</pre>
          </details>
        )}
      </details>
      <div className="faint">초안 {draft.attempts}번 시도</div>
    </>
  );
}
