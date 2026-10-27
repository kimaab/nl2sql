import { Fragment, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import * as api from "../../../api";
import { Badge, EmptyState, Loading, PageHeader } from "../../../components/ui";
import { useToast } from "../../../components/Toast";
import { errorMessage, formatDateTime } from "../../../lib/format";
import { VERDICT_LABEL, VERDICT_TONE } from "../../../lib/metricAuto";
import { ValidationChecks } from "./ValidationChecks";

/** 지표 하나의 검증 이력. 가장 최근 결과는 펼쳐 둔다 */
export function MetricValidationHistory() {
  const { dsId = "", metricId = "" } = useParams();
  const [metric, setMetric] = useState<api.Metric | null>(null);
  const [items, setItems] = useState<api.ValidationItem[] | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const toast = useToast();

  useEffect(() => {
    api.listMetrics(dsId).then((list) => setMetric(list.find((m) => m.id === metricId) ?? null)).catch(() => setMetric(null));
    api
      .metricValidations(dsId, metricId)
      .then((list) => {
        setItems(list);
        if (list.length > 0) setOpen(list[0].id);
      })
      .catch((err) => {
        toast("검증 이력을 불러오지 못했습니다: " + errorMessage(err), "error");
        setItems([]);
      });
  }, [dsId, metricId]);

  const name = metric?.name ?? items?.[0]?.metric_name ?? "지표";

  return (
    <div className="page">
      <PageHeader
        back={{ to: `/metrics/validate?ds=${dsId}`, label: "지표 검증" }}
        title={`${name} 검증 이력`}
        desc={metric ? <Link to={`/metrics/${dsId}/${metricId}`}>지표 상세 보기</Link> : "지워진 지표입니다. 검증 기록만 남아 있습니다."}
      />
      <div className="card">
        {items === null ? (
          <Loading />
        ) : items.length === 0 ? (
          <EmptyState title="검증한 적이 없습니다" desc="지표 검증 화면에서 이 지표를 골라 검증하세요." />
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>검증 시각</th>
                <th>버전</th>
                <th>판정</th>
                <th>사유</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <Fragment key={item.id}>
                  <tr className="clickable" onClick={() => setOpen(open === item.id ? null : item.id)}>
                    <td>{formatDateTime(item.created_at)}</td>
                    <td>v{item.metric_version}</td>
                    <td>
                      <Badge tone={VERDICT_TONE[item.verdict]}>{VERDICT_LABEL[item.verdict]}</Badge>
                    </td>
                    <td className="muted">{item.summary}</td>
                  </tr>
                  {open === item.id && (
                    <tr>
                      <td colSpan={4}>
                        <ValidationChecks item={item} />
                        <Link to={`/metrics/validate/${dsId}/${item.run_id}`} className="faint">
                          이 검증 실행 전체 보기
                        </Link>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
