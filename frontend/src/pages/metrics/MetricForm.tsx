import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import * as api from "../../api";
import { Alert, Field, Loading, PageHeader } from "../../components/ui";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/format";
import { EMPTY_METRIC, toInput } from "../../lib/metrics";
import { MetricEditor } from "./MetricEditor";

/** 지표 등록·수정 페이지. 폼은 MetricEditor 가 그린다. */
export function MetricForm() {
  const { dsId: editDs, metricId } = useParams();
  const editing = Boolean(editDs && metricId);
  const [params] = useSearchParams();
  const [systems, setSystems] = useState<api.System[] | null>(null);
  const [dsId, setDsId] = useState(editDs ?? params.get("ds") ?? "");
  const [initial, setInitial] = useState<api.MetricInput | null>(editing ? null : EMPTY_METRIC);
  const toast = useToast();
  const navigate = useNavigate();

  useEffect(() => {
    api
      .listSystems()
      // 동기화된 것만 쓸 수 있다. 스키마가 없으면 SQL 을 AST 로 옮길 수 없다.
      .then((list) => setSystems(list.filter((d) => d.synced_at)))
      .catch((err) => {
        toast("시스템을 불러오지 못했습니다: " + errorMessage(err), "error");
        setSystems([]);
      });
  }, []);

  useEffect(() => {
    if (!editing || !dsId) return;
    api
      .listMetrics(dsId)
      .then((ms) => {
        const current = ms.find((m) => m.id === metricId);
        if (!current) toast("수정할 지표를 찾을 수 없습니다", "error");
        setInitial(current ? toInput(current) : EMPTY_METRIC);
      })
      .catch((err) => toast("지표를 불러오지 못했습니다: " + errorMessage(err), "error"));
  }, [dsId]);

  if (systems === null || initial === null) return <Loading />;

  return (
    <div className="page wide">
      <PageHeader
        back={editing ? { to: `/metrics/${dsId}/${metricId}`, label: "지표 상세" } : { to: "/metrics", label: "지표 목록" }}
        title={editing ? `지표 수정 — ${initial.name}` : "새 지표 등록"}
        desc="계산이 필요한 숫자를 SQL 로 등록합니다. SQL 은 조회 명세(AST)로 옮겨 저장되고, 질문마다 LLM 이 기간·조건만 바꿔 씁니다."
      />

      <MetricEditor
        dsId={dsId}
        initial={initial}
        metricId={editing ? metricId : undefined}
        onSaved={(saved) => navigate(`/metrics/${dsId}/${saved.id}`)}
        systemField={
          systems.length === 0 ? (
            <Alert tone="warn">스키마를 동기화한 시스템이 없습니다. 먼저 시스템을 등록하고 동기화하세요.</Alert>
          ) : (
            <Field label="시스템" required>
              <select className="select" value={dsId} disabled={editing} onChange={(e) => setDsId(e.target.value)}>
                <option value="">선택하세요</option>
                {systems.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name} ({d.driver})
                  </option>
                ))}
              </select>
            </Field>
          )
        }
      />
    </div>
  );
}
