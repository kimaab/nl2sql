import { Fragment, useState, useEffect } from "react";
import * as api from "./api";

const TONE = {
  info: { color: "#555", background: "#f5f5f5" },
  ok: { color: "#2e7d32", background: "#e8f5e9" },
  warn: { color: "#e65100", background: "#fff3e0" },
  error: { color: "#c62828", background: "#ffebee" },
} as const;

const AGG_FUNCTIONS = ["SUM", "COUNT", "AVG", "MIN", "MAX"];
const OPERATORS = [
  "equals",
  "not_equals",
  "greater_than",
  "greater_or_equal",
  "less_than",
  "less_or_equal",
];

const SOURCES: { value: string; label: string; hint: string }[] = [
  { value: "literal", label: "고정", hint: "질문과 무관하게 항상 적용됩니다" },
  { value: "relative", label: "상대 기간", hint: "오늘 기준으로 계산합니다. 질문이 같은 컬럼을 지정하면 물러납니다" },
  { value: "question", label: "질문에서", hint: "값을 질문에서 받습니다. 질문이 지정하지 않으면 거부합니다" },
];

/** 날짜 컬럼인지만 본다. 시각 포함 여부는 백엔드가 드라이버를 보고 판정한다. */
function isTemporal(type: string): boolean {
  const t = (type || "").trim().toLowerCase();
  if (!t) return false;
  if (t.startsWith("timestamp") || t.startsWith("datetime")) return true;
  return t.split("(")[0].trim() === "date";
}

const EMPTY_METRIC: api.MetricInput = {
  name: "",
  description: "",
  kind: "aggregate",
  table_name: "",
  joins: [],
  agg_field: "*",
  agg_function: "COUNT",
  select_columns: [],
  fixed_filters: [],
};

export function DatasourcePage() {
  const [datasources, setDatasources] = useState<api.Datasource[]>([]);
  const [loading, setLoading] = useState(true);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [metrics, setMetrics] = useState<Record<string, api.Metric[]>>({});
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [syncingId, setSyncingId] = useState<string | null>(null);
  const [syncMessage, setSyncMessage] = useState<
    Record<string, { text: string; tone: "info" | "ok" | "warn" | "error" }>
  >({});
  const [schemas, setSchemas] = useState<Record<string, api.SchemaTable[]>>({});
  const [metricForm, setMetricForm] = useState<api.MetricInput>(EMPTY_METRIC);
  const [savingMetric, setSavingMetric] = useState(false);

  const [formData, setFormData] = useState<api.DatasourceInput>({
    name: "",
    description: "",
    driver: "postgresql",
    host: "",
    port: 0,
    db_name: "",
    db_schema: "",
    username: "",
    password: "",
  });

  useEffect(() => {
    loadDatasources();
  }, []);

  async function loadDatasources() {
    // loading은 첫 로드에만 쓴다. 갱신할 때마다 켜면 등록·삭제·동기화 때마다
    // 화면 전체가 "로딩 중..."으로 사라졌다 다시 그려져서, 펼쳐둔 패널과
    // 스크롤 위치가 매번 날아간다.
    try {
      const data = await api.listDatasources();
      setDatasources(data);
    } catch (err) {
      alert("데이터소스 로드 실패: " + err);
    } finally {
      setLoading(false);
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    try {
      if (editingId) {
        await api.updateDatasource(editingId, formData);
      } else {
        await api.createDatasource(formData);
      }
      setFormData({
        name: "",
        description: "",
        driver: "postgresql",
        host: "",
        port: 0,
        db_name: "",
        db_schema: "",
        username: "",
        password: "",
      });
      setEditingId(null);
      await loadDatasources();
    } catch (err) {
      alert("저장 실패: " + err);
    }
  }

  async function handleEdit(ds: api.Datasource) {
    setEditingId(ds.id);
    setFormData({
      name: ds.name,
      description: ds.description,
      driver: ds.driver,
      host: ds.host,
      port: ds.port,
      db_name: ds.db_name,
      db_schema: ds.db_schema,
      username: ds.username,
      password: "",
    });
  }

  async function handleDelete(id: string) {
    if (!confirm("데이터소스를 삭제하시겠습니까? 스키마와 지표도 사라집니다.")) {
      return;
    }
    try {
      await api.deleteDatasource(id);
      await loadDatasources();
    } catch (err) {
      alert("삭제 실패: " + err);
    }
  }

  async function handleSync(id: string) {
    try {
      setSyncingId(id);
      setSyncMessage((prev) => ({
        ...prev,
        [id]: { text: "대상 DB에 접속 중...", tone: "info" },
      }));
      const result = await api.syncDatasource(id);
      setSyncMessage((prev) => ({
        ...prev,
        [id]: {
          text: `테이블 ${result.table_count}개, 컬럼 ${result.column_count}개를 읽었습니다`,
          // 접속은 됐는데 0건이면 스키마 이름이나 Oracle owner 오타일 때가 많다.
          // 빨갛지 않아서 제일 오래 붙잡게 되는 실패라 경고로 띄운다.
          tone: result.table_count === 0 ? "warn" : "ok",
        },
      }));
      await loadDatasources();
    } catch (err) {
      setSyncMessage((prev) => ({
        ...prev,
        [id]: { text: "동기화 실패: " + err, tone: "error" },
      }));
    } finally {
      setSyncingId(null);
    }
  }

  async function loadMetrics(datasourceId: string) {
    const data = await api.listMetrics(datasourceId);
    setMetrics((prev) => ({ ...prev, [datasourceId]: data }));
  }

  async function openMetrics(datasourceId: string) {
    setExpandedId(datasourceId);
    // 폼은 데이터소스마다 새로 시작한다. 남겨두면 A의 테이블명이 B의 폼에 남는다.
    setMetricForm(EMPTY_METRIC);
    try {
      const [, schema] = await Promise.all([
        loadMetrics(datasourceId),
        schemas[datasourceId]
          ? Promise.resolve({ tables: schemas[datasourceId] })
          : api.getSchema(datasourceId),
      ]);
      setSchemas((prev) => ({ ...prev, [datasourceId]: schema.tables }));
      setMetricForm((prev) => ({
        ...prev,
        table_name: prev.table_name || schema.tables[0]?.name || "",
      }));
    } catch (err) {
      alert("지표 화면을 여는 데 실패했습니다: " + err);
    }
  }

  /** 날짜 컬럼이면 상대 기간을 기본으로 준다 — 날짜를 고정값으로 박아두면
   *  다음 주에 그 지표가 지난주를 가리키게 된다. */
  function newFilter(field: string, type: string) {
    const temporal = isTemporal(type);
    return temporal
      ? { field, operator: "greater_or_equal", source: "relative", value: "-7d" }
      : { field, operator: "equals", source: "literal", value: "" };
  }

  function columnsOf(datasourceId: string, tableName: string): api.SchemaColumn[] {
    return (
      schemas[datasourceId]?.find((t) => t.name === tableName)?.columns ?? []
    );
  }

  /** 동기화로 테이블·컬럼이 사라진 지표. 모델에게 전달되지 않으므로 화면에서도 알려야 한다. */
  function isBroken(datasourceId: string, m: api.Metric): boolean {
    const tables = schemas[datasourceId];
    return !!tables && api.metricBrokenReason(tables, m) !== null;
  }

  /** 테이블들의 컬럼을 선택지로. 조인이 있으면 '테이블.컬럼' 으로 적어 모호함을 없앤다. */
  function columnOptions(datasourceId: string, tables: string[], qualify: boolean) {
    return tables.flatMap((t) =>
      columnsOf(datasourceId, t).map((c) => ({
        value: qualify ? `${t}.${c.name}` : c.name,
        type: c.type,
        comment: c.description,
      }))
    );
  }

  function formColumns() {
    if (!expandedId) return [];
    const f = metricForm;
    return columnOptions(expandedId, [f.table_name, ...f.joins.map((j) => j.table)], f.joins.length > 0);
  }

  /** 조인이 바뀌면 이미 고른 컬럼을 새 모양으로 옮긴다. 사라진 테이블의 컬럼은 버린다. */
  function withJoins(form: api.MetricInput, joins: api.JoinSpec[]): api.MetricInput {
    const inScope = new Set([form.table_name, ...joins.map((j) => j.table)]);
    const qualify = joins.length > 0;
    const move = (ref: string | null | undefined): string | null => {
      if (!ref) return null;
      const dot = ref.indexOf(".");
      const [t, c] = dot >= 0 ? [ref.slice(0, dot), ref.slice(dot + 1)] : [form.table_name, ref];
      if (!inScope.has(t)) return null;
      if (qualify) return `${t}.${c}`;
      return t === form.table_name ? c : null;
    };
    // 앞에서 빠진 테이블을 가리키는 ON 조건도 함께 버린다.
    const cleaned = joins.map((j, i) => {
      const earlier = new Set([form.table_name, ...joins.slice(0, i).map((x) => x.table)]);
      return {
        ...j,
        on: j.on.filter((p) => earlier.has(p.left.split(".")[0]) && p.right.split(".")[0] === j.table),
      };
    });
    return {
      ...form,
      joins: cleaned,
      agg_field: form.agg_field === "*" ? "*" : move(form.agg_field) ?? "*",
      select_columns: form.select_columns.map(move).filter((c): c is string => c !== null),
      fixed_filters: form.fixed_filters
        .map((f) => ({ ...f, field: move(f.field) }))
        .filter((f) => f.field !== null),
    };
  }

  /** 새 조인의 ON 기본값 — 앞선 테이블과 이름이 같은 컬럼이 있으면 그것으로 잇는다. */
  function guessPair(datasourceId: string, earlier: string[], table: string): api.JoinOn {
    const right = columnsOf(datasourceId, table);
    for (const t of earlier) {
      const common = columnsOf(datasourceId, t).find((c) =>
        right.some((r) => r.name.toLowerCase() === c.name.toLowerCase())
      );
      if (common) {
        const r = right.find((x) => x.name.toLowerCase() === common.name.toLowerCase())!;
        return { left: `${t}.${common.name}`, right: `${table}.${r.name}` };
      }
    }
    const first = columnsOf(datasourceId, earlier[0])[0];
    return { left: `${earlier[0]}.${first?.name ?? ""}`, right: `${table}.${right[0]?.name ?? ""}` };
  }

  async function handleCreateMetric(e: React.FormEvent) {
    e.preventDefault();
    if (!expandedId) return;
    try {
      setSavingMetric(true);
      await api.createMetric(expandedId, metricForm);
      setMetricForm({
        ...EMPTY_METRIC,
        kind: metricForm.kind,
        table_name: metricForm.table_name,
        joins: metricForm.joins,
      });
      await loadMetrics(expandedId);
      await loadDatasources();
    } catch (err) {
      alert("지표 등록 실패: " + err);
    } finally {
      setSavingMetric(false);
    }
  }

  function formatDate(dateStr: string | null) {
    if (!dateStr) return "동기화 안 됨";
    const date = new Date(dateStr);
    const now = new Date();
    const diff = now.getTime() - date.getTime();
    const minutes = Math.floor(diff / 60000);
    const hours = Math.floor(diff / 3600000);
    const days = Math.floor(diff / 86400000);
    if (minutes < 1) return "방금 전";
    if (minutes < 60) return `${minutes}분 전`;
    if (hours < 24) return `${hours}시간 전`;
    return `${days}일 전`;
  }

  if (loading) return <div>로딩 중...</div>;

  return (
    <div style={{ padding: "20px" }}>
      <h2>데이터소스 관리</h2>

      <form onSubmit={handleSubmit} autoComplete="off" style={{ marginBottom: "30px", border: "1px solid #ccc", padding: "15px" }}>
        <h3>{editingId ? "데이터소스 편집" : "새 데이터소스 등록"}</h3>

        <div>
          <label>이름: </label>
          <input
            type="text"
            value={formData.name}
            onChange={(e) => setFormData({ ...formData, name: e.target.value })}
            required
          />
        </div>

        <div>
          <label>설명: </label>
          <input
            type="text"
            value={formData.description}
            onChange={(e) => setFormData({ ...formData, description: e.target.value })}
          />
        </div>

        <div>
          <label>드라이버: </label>
          <select
            value={formData.driver}
            onChange={(e) => setFormData({ ...formData, driver: e.target.value as any })}
          >
            <option value="postgresql">PostgreSQL</option>
            <option value="mysql">MySQL</option>
            <option value="oracle">Oracle</option>
          </select>
        </div>

        <div>
          <label>호스트: </label>
          <input
            type="text"
            value={formData.host}
            onChange={(e) => setFormData({ ...formData, host: e.target.value })}
            required
          />
        </div>

        <div>
          <label>포트 (비우면 기본값): </label>
          <input
            type="number"
            value={formData.port || ""}
            onChange={(e) => setFormData({ ...formData, port: e.target.value ? parseInt(e.target.value) : 0 })}
          />
        </div>

        <div>
          <label>데이터베이스 이름: </label>
          <input
            type="text"
            value={formData.db_name}
            onChange={(e) => setFormData({ ...formData, db_name: e.target.value })}
            required
          />
        </div>

        {formData.driver !== "mysql" && (
          <div>
            <label>스키마: </label>
            <input
              type="text"
              value={formData.db_schema}
              onChange={(e) => setFormData({ ...formData, db_schema: e.target.value })}
              placeholder={formData.driver === "postgresql" ? "기본값: public" : "기본값: 계정명"}
            />
          </div>
        )}

        <div>
          <label>사용자명: </label>
          <input
            type="text"
            value={formData.username}
            onChange={(e) => setFormData({ ...formData, username: e.target.value })}
            autoComplete="off"
          />
        </div>

        <div>
          <label>비밀번호: </label>
          <input
            type="password"
            value={formData.password}
            onChange={(e) => setFormData({ ...formData, password: e.target.value })}
            placeholder={editingId ? "비워두면 저장된 비밀번호를 그대로 씁니다" : ""}
            autoComplete="new-password"
          />
        </div>

        <button type="submit">{editingId ? "수정" : "등록"}</button>
        {editingId && (
          <button
            type="button"
            onClick={() => {
              setEditingId(null);
              setFormData({
                name: "",
                description: "",
                driver: "postgresql",
                host: "",
                port: 0,
                db_name: "",
                db_schema: "",
                username: "",
                password: "",
              });
            }}
          >
            취소
          </button>
        )}
      </form>

      <div>
        <h3>등록된 데이터소스</h3>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ borderBottom: "2px solid #ccc" }}>
              <th>이름</th>
              <th>드라이버</th>
              <th>호스트</th>
              <th>상태</th>
              <th>작업</th>
            </tr>
          </thead>
          <tbody>
            {datasources.map((ds) => (
              <Fragment key={ds.id}>
              <tr style={{ borderBottom: "1px solid #ccc" }}>
                <td>{ds.name}</td>
                <td>{ds.driver}</td>
                <td>{ds.host}:{ds.port}</td>
                <td>
                  {ds.synced_at ? (
                    <span style={{ color: "green" }}>테이블 {ds.table_count}개 · 지표 {ds.metric_count}개 · {formatDate(ds.synced_at)}</span>
                  ) : (
                    <span style={{ color: "red" }}>스키마 없음 — 조회 불가</span>
                  )}
                </td>
                <td>
                  <button onClick={() => handleEdit(ds)}>편집</button>
                  <button
                    onClick={() => handleSync(ds.id)}
                    disabled={syncingId === ds.id}
                  >
                    {syncingId === ds.id ? "동기화 중..." : "스키마 읽기"}
                  </button>
                  <button
                    onClick={() => {
                      if (expandedId === ds.id) {
                        setExpandedId(null);
                      } else {
                        openMetrics(ds.id);
                      }
                    }}
                  >
                    {expandedId === ds.id ? "닫기" : "지표"}
                  </button>
                  <button onClick={() => handleDelete(ds.id)}>삭제</button>
                </td>
              </tr>
              {syncMessage[ds.id] && (
                <tr>
                  <td colSpan={5} style={{ ...TONE[syncMessage[ds.id].tone], fontSize: "0.9em" }}>
                    {syncMessage[ds.id].text}
                  </td>
                </tr>
              )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>

      {expandedId && (
        <div style={{ marginTop: "20px", padding: "15px", border: "1px solid #ccc", background: "white" }}>
          <h4>{datasources.find((d) => d.id === expandedId)?.name} - 지표 관리</h4>
          <p style={{ color: "#666", fontSize: "0.9em", marginBottom: "15px" }}>
            스키마에서 자동으로 뽑을 수 없는 개념을 사람이 정의합니다. 고정 필터는
            질문에 언급되지 않아도 SQL에 항상 붙습니다.
          </p>

          {metrics[expandedId]?.length ? (
            <div style={{ marginBottom: "20px" }}>
              {metrics[expandedId].map((m) => {
                const broken = isBroken(expandedId, m);
                return (
                  <div
                    key={m.id}
                    style={{
                      padding: "10px",
                      marginBottom: "8px",
                      background: broken ? TONE.warn.background : "#f5f5f5",
                      border: broken ? "1px solid " + TONE.warn.color : "1px solid #eee",
                    }}
                  >
                    <strong>{m.name}</strong>{" "}
                    <code>
                      {m.kind === "projection"
                        ? `${m.select_columns.join(", ")} FROM ${m.table_name}${api.joinClause(m)}`
                        : `${m.agg_function}(${m.agg_field}) FROM ${m.table_name}${api.joinClause(m)}`}
                    </code>
                    {m.fixed_filters.length > 0 && (
                      <code style={{ marginLeft: "6px" }}>
                        WHERE{" "}
                        {m.fixed_filters
                          .map((f) => [f.field, f.operator, f.value].join(" "))
                          .join(" AND ")}
                      </code>
                    )}
                    {broken && (
                      <span style={{ color: TONE.warn.color, marginLeft: "8px" }}>
                        스키마와 맞지 않음 — 질문에 쓰이지 않습니다
                      </span>
                    )}
                    <button
                      onClick={async () => {
                        if (!confirm("지표 " + m.name + "을(를) 삭제하시겠습니까?")) return;
                        try {
                          await api.deleteMetric(expandedId, m.id);
                          await loadMetrics(expandedId);
                          await loadDatasources();
                        } catch (err) {
                          alert("지표 삭제 실패: " + err);
                        }
                      }}
                      style={{ marginLeft: "10px" }}
                    >
                      삭제
                    </button>
                  </div>
                );
              })}
            </div>
          ) : (
            <p style={{ color: "#666", marginBottom: "20px" }}>
              등록된 지표가 없습니다. 아래에서 추가하십시오.
            </p>
          )}

          <form onSubmit={handleCreateMetric} style={{ border: "1px solid #ddd", padding: "15px", boxShadow: "none" }}>
            <h4 style={{ marginTop: 0 }}>지표 추가</h4>

            <div>
              <label>종류</label>
              <div style={{ display: "flex", flexDirection: "row", gap: "16px" }}>
                <label style={{ flexDirection: "row", alignItems: "center", gap: "5px", fontWeight: 400 }}>
                  <input
                    type="radio"
                    checked={metricForm.kind === "aggregate"}
                    onChange={() =>
                      setMetricForm({ ...metricForm, kind: "aggregate", agg_field: "*", agg_function: "COUNT", select_columns: [] })
                    }
                    style={{ width: "auto" }}
                  />
                  집계 — 숫자 하나 (SUM, COUNT ...)
                </label>
                <label style={{ flexDirection: "row", alignItems: "center", gap: "5px", fontWeight: 400 }}>
                  <input
                    type="radio"
                    checked={metricForm.kind === "projection"}
                    onChange={() =>
                      setMetricForm({ ...metricForm, kind: "projection", agg_field: null, agg_function: null })
                    }
                    style={{ width: "auto" }}
                  />
                  조회 — 컬럼 목록 (SELECT a, b, c)
                </label>
              </div>
            </div>

            <div>
              <label>이름</label>
              <input
                type="text"
                value={metricForm.name}
                onChange={(e) => setMetricForm({ ...metricForm, name: e.target.value })}
                placeholder="active_revenue"
                required
              />
            </div>

            <div>
              <label>설명 (질문과 이 지표를 이어주는 검색 본문입니다)</label>
              <input
                type="text"
                value={metricForm.description}
                onChange={(e) => setMetricForm({ ...metricForm, description: e.target.value })}
                placeholder="계약이 살아있는 건의 매출액 합계"
              />
            </div>

            <div>
              <label>기본 테이블</label>
              <select
                value={metricForm.table_name}
                onChange={(e) =>
                  setMetricForm({
                    ...metricForm,
                    table_name: e.target.value,
                    joins: [],
                    agg_field: "*",
                    select_columns: [],
                    fixed_filters: [],
                  })
                }
                required
              >
                {(schemas[expandedId] ?? []).map((t) => (
                  <option key={t.name} value={t.name}>
                    {t.name}
                    {t.description ? " — " + t.description : ""}
                  </option>
                ))}
              </select>
            </div>

            <div style={{ alignItems: "flex-start" }}>
              <label>
                조인{" "}
                <span style={{ fontWeight: 400, color: "#888" }}>
                  — 1:N 으로 조인하면 기본 테이블의 행이 불어나 SUM·COUNT 가 커집니다
                </span>
              </label>
              {metricForm.joins.map((j, i) => {
                const earlier = [metricForm.table_name, ...metricForm.joins.slice(0, i).map((x) => x.table)];
                const taken = new Set([metricForm.table_name, ...metricForm.joins.map((x) => x.table)]);
                const setJoin = (next: api.JoinSpec) => {
                  const joins = [...metricForm.joins];
                  joins[i] = next;
                  setMetricForm(withJoins(metricForm, joins));
                };
                const leftCols = columnOptions(expandedId, earlier, true);
                const rightCols = columnOptions(expandedId, [j.table], true);
                return (
                  <div key={i} style={{ marginBottom: "10px", padding: "8px", border: "1px solid #eee", width: "100%" }}>
                    <div style={{ display: "flex", flexDirection: "row", gap: "8px" }}>
                      <select
                        value={j.type}
                        onChange={(e) => setJoin({ ...j, type: e.target.value as api.JoinSpec["type"] })}
                        style={{ width: "auto" }}
                      >
                        <option value="inner">INNER JOIN</option>
                        <option value="left">LEFT JOIN</option>
                      </select>
                      <select
                        value={j.table}
                        onChange={(e) =>
                          setJoin({ ...j, table: e.target.value, on: [guessPair(expandedId, earlier, e.target.value)] })
                        }
                      >
                        {(schemas[expandedId] ?? [])
                          .filter((t) => t.name === j.table || !taken.has(t.name))
                          .map((t) => (
                            <option key={t.name} value={t.name}>
                              {t.name}
                              {t.description ? " — " + t.description : ""}
                            </option>
                          ))}
                      </select>
                      <button
                        type="button"
                        onClick={() => setMetricForm(withJoins(metricForm, metricForm.joins.filter((_, k) => k !== i)))}
                        style={{ whiteSpace: "nowrap" }}
                      >
                        조인 빼기
                      </button>
                    </div>
                    {j.on.map((p, pi) => (
                      <div key={pi} style={{ display: "flex", flexDirection: "row", gap: "8px", marginTop: "6px", alignItems: "center" }}>
                        <span style={{ color: "#888", minWidth: "36px" }}>{pi === 0 ? "ON" : "AND"}</span>
                        <select
                          value={p.left}
                          onChange={(e) => setJoin({ ...j, on: j.on.map((x, k) => (k === pi ? { ...x, left: e.target.value } : x)) })}
                        >
                          {leftCols.map((c) => (
                            <option key={c.value} value={c.value}>
                              {c.value}{c.comment ? ` — ${c.comment}` : ""}
                            </option>
                          ))}
                        </select>
                        <span>=</span>
                        <select
                          value={p.right}
                          onChange={(e) => setJoin({ ...j, on: j.on.map((x, k) => (k === pi ? { ...x, right: e.target.value } : x)) })}
                        >
                          {rightCols.map((c) => (
                            <option key={c.value} value={c.value}>
                              {c.value}{c.comment ? ` — ${c.comment}` : ""}
                            </option>
                          ))}
                        </select>
                        <button
                          type="button"
                          onClick={() => setJoin({ ...j, on: j.on.filter((_, k) => k !== pi) })}
                          style={{ whiteSpace: "nowrap" }}
                        >
                          빼기
                        </button>
                      </div>
                    ))}
                    <button
                      type="button"
                      onClick={() => setJoin({ ...j, on: [...j.on, guessPair(expandedId, earlier, j.table)] })}
                      style={{ marginTop: "6px" }}
                    >
                      조건 추가
                    </button>
                    {j.on.length === 0 && (
                      <span style={{ color: TONE.warn.color, marginLeft: "8px" }}>ON 조건이 하나 이상 필요합니다</span>
                    )}
                  </div>
                );
              })}
              <button
                type="button"
                onClick={() => {
                  const taken = new Set([metricForm.table_name, ...metricForm.joins.map((x) => x.table)]);
                  const next = (schemas[expandedId] ?? []).find((t) => !taken.has(t.name));
                  if (!next) return;
                  setMetricForm(
                    withJoins(metricForm, [
                      ...metricForm.joins,
                      { table: next.name, type: "inner", on: [guessPair(expandedId, [...taken], next.name)] },
                    ])
                  );
                }}
                disabled={!metricForm.table_name}
              >
                조인 추가
              </button>
            </div>

            {metricForm.kind === "aggregate" ? (
              <div>
                <label>집계</label>
                <div style={{ display: "flex", flexDirection: "row", gap: "8px" }}>
                  <select
                    value={metricForm.agg_function ?? "COUNT"}
                    onChange={(e) => setMetricForm({ ...metricForm, agg_function: e.target.value })}
                  >
                    {AGG_FUNCTIONS.map((f) => (
                      <option key={f} value={f}>
                        {f}
                      </option>
                    ))}
                  </select>
                  <select
                    value={metricForm.agg_field ?? "*"}
                    onChange={(e) => setMetricForm({ ...metricForm, agg_field: e.target.value })}
                  >
                    <option value="*">*</option>
                    {formColumns().map((c) => (
                      <option key={c.value} value={c.value}>
                        {c.value} ({c.type}){c.comment ? ` — ${c.comment}` : ""}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
            ) : (
              <div style={{ alignItems: "flex-start" }}>
                <label>
                  조회 컬럼 — 체크한 순서대로 SELECT에 들어갑니다
                  {metricForm.select_columns.length > 0 && (
                    <span style={{ fontWeight: 400, color: "#555" }}>
                      {" "}
                      ({metricForm.select_columns.length}개:{" "}
                      {metricForm.select_columns.join(", ")})
                    </span>
                  )}
                </label>
                <div
                  style={{
                    display: "flex",
                    flexDirection: "row",
                    flexWrap: "wrap",
                    gap: "4px 14px",
                    maxHeight: "180px",
                    overflowY: "auto",
                    border: "1px solid #ddd",
                    padding: "10px",
                    width: "100%",
                  }}
                >
                  {formColumns().map((c) => (
                    <label
                      key={c.value}
                      style={{ flexDirection: "row", alignItems: "center", gap: "4px", fontWeight: 400 }}
                    >
                      <input
                        type="checkbox"
                        checked={metricForm.select_columns.includes(c.value)}
                        onChange={(e) =>
                          setMetricForm({
                            ...metricForm,
                            select_columns: e.target.checked
                              ? [...metricForm.select_columns, c.value]
                              : metricForm.select_columns.filter((x) => x !== c.value),
                          })
                        }
                        style={{ width: "auto" }}
                      />
                      {c.value}
                      {c.comment && <span style={{ color: "#1565c0", marginLeft: "4px" }}>{c.comment}</span>}
                      <span style={{ color: "#999", marginLeft: "4px" }}>({c.type})</span>
                    </label>
                  ))}
                  {formColumns().length === 0 && (
                    <span style={{ color: "#666" }}>테이블을 먼저 고르십시오.</span>
                  )}
                </div>
              </div>
            )}

            <div style={{ alignItems: "flex-start" }}>
              <label>고정 필터</label>
              {metricForm.fixed_filters.map((f, i) => (
                <div key={i} style={{ marginBottom: "10px" }}>
                  <div style={{ display: "flex", flexDirection: "row", gap: "8px" }}>
                    <select
                      value={f.field ?? ""}
                      onChange={(e) => {
                        const col = formColumns().find((c) => c.value === e.target.value);
                        const next = [...metricForm.fixed_filters];
                        // 컬럼이 바뀌면 출처 기본값도 다시 정한다
                        next[i] = newFilter(e.target.value, col?.type ?? "");
                        setMetricForm({ ...metricForm, fixed_filters: next });
                      }}
                    >
                      {formColumns().map((c) => (
                        <option key={c.value} value={c.value}>
                          {c.value}{c.comment ? ` — ${c.comment}` : ""}
                        </option>
                      ))}
                    </select>
                    <select
                      value={f.source ?? "literal"}
                      onChange={(e) => {
                        const next = [...metricForm.fixed_filters];
                        const src = e.target.value;
                        next[i] = {
                          ...next[i],
                          source: src,
                          value: src === "question" ? "" : src === "relative" ? "-7d" : "",
                        };
                        setMetricForm({ ...metricForm, fixed_filters: next });
                      }}
                    >
                      {SOURCES.map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                    </select>
                    <select
                      value={f.operator ?? "equals"}
                      onChange={(e) => {
                        const next = [...metricForm.fixed_filters];
                        next[i] = { ...next[i], operator: e.target.value };
                        setMetricForm({ ...metricForm, fixed_filters: next });
                      }}
                    >
                      {OPERATORS.map((o) => (
                        <option key={o} value={o}>
                          {o}
                        </option>
                      ))}
                    </select>
                    {(f.source ?? "literal") === "question" ? (
                      <span style={{ flex: 1, color: "#999", alignSelf: "center", fontSize: "0.9em" }}>
                        값은 질문에서
                      </span>
                    ) : (
                      <input
                        type="text"
                        value={f.value ?? ""}
                        onChange={(e) => {
                          const next = [...metricForm.fixed_filters];
                          next[i] = { ...next[i], value: e.target.value };
                          setMetricForm({ ...metricForm, fixed_filters: next });
                        }}
                        placeholder={(f.source ?? "literal") === "relative" ? "-7d · -1m · 0d" : "값"}
                      />
                    )}
                    <button
                      type="button"
                      onClick={() =>
                        setMetricForm({
                          ...metricForm,
                          fixed_filters: metricForm.fixed_filters.filter((_, j) => j !== i),
                        })
                      }
                      style={{ whiteSpace: "nowrap" }}
                    >
                      빼기
                    </button>
                  </div>
                  <div style={{ fontSize: "0.85em", color: "#888", marginTop: "2px" }}>
                    {SOURCES.find((o) => o.value === (f.source ?? "literal"))?.hint}
                  </div>
                </div>
              ))}
              <button
                type="button"
                onClick={() => {
                  const first = formColumns()[0];
                  setMetricForm({
                    ...metricForm,
                    fixed_filters: [
                      ...metricForm.fixed_filters,
                      newFilter(first?.value ?? "", first?.type ?? ""),
                    ],
                  });
                }}
                disabled={formColumns().length === 0}
              >
                필터 추가
              </button>
            </div>

            <button
              type="submit"
              disabled={
                savingMetric ||
                !metricForm.table_name ||
                metricForm.joins.some((j) => j.on.length === 0) ||
                (metricForm.kind === "projection" && metricForm.select_columns.length === 0)
              }
            >
              {savingMetric ? "등록 중..." : "지표 등록"}
            </button>
          </form>
        </div>
      )}
    </div>
  );
}
