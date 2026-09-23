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

const EMPTY_METRIC: api.MetricInput = {
  name: "",
  description: "",
  kind: "aggregate",
  table_name: "",
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

  function columnsOf(datasourceId: string, tableName: string): api.SchemaColumn[] {
    return (
      schemas[datasourceId]?.find((t) => t.name === tableName)?.columns ?? []
    );
  }

  /** 동기화로 테이블·컬럼이 사라진 지표. 모델에게 전달되지 않으므로 화면에서도 알려야 한다. */
  function isBroken(datasourceId: string, m: api.Metric): boolean {
    const tables = schemas[datasourceId];
    if (!tables) return false;
    const table = tables.find((t) => t.name === m.table_name);
    if (!table) return true;
    const has = (name: string) => table.columns.some((c) => c.name === name);
    if (m.kind === "projection") {
      if (m.select_columns.length === 0 || !m.select_columns.every(has)) return true;
    } else if (m.agg_field !== "*" && !has(m.agg_field ?? "")) {
      return true;
    }
    return m.fixed_filters.some((f) => f.field && !has(f.field));
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
                        ? `${m.select_columns.join(", ")} FROM ${m.table_name}`
                        : `${m.agg_function}(${m.agg_field}) FROM ${m.table_name}`}
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
              <label>테이블</label>
              <select
                value={metricForm.table_name}
                onChange={(e) =>
                  setMetricForm({
                    ...metricForm,
                    table_name: e.target.value,
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
                    {columnsOf(expandedId, metricForm.table_name).map((c) => (
                      <option key={c.name} value={c.name}>
                        {c.name} ({c.type})
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
                  {columnsOf(expandedId, metricForm.table_name).map((c) => (
                    <label
                      key={c.name}
                      style={{ flexDirection: "row", alignItems: "center", gap: "4px", fontWeight: 400 }}
                    >
                      <input
                        type="checkbox"
                        checked={metricForm.select_columns.includes(c.name)}
                        onChange={(e) =>
                          setMetricForm({
                            ...metricForm,
                            select_columns: e.target.checked
                              ? [...metricForm.select_columns, c.name]
                              : metricForm.select_columns.filter((x) => x !== c.name),
                          })
                        }
                        style={{ width: "auto" }}
                      />
                      {c.name}
                      <span style={{ color: "#999" }}>({c.type})</span>
                    </label>
                  ))}
                  {columnsOf(expandedId, metricForm.table_name).length === 0 && (
                    <span style={{ color: "#666" }}>테이블을 먼저 고르십시오.</span>
                  )}
                </div>
              </div>
            )}

            <div style={{ alignItems: "flex-start" }}>
              <label>고정 필터</label>
              {metricForm.fixed_filters.map((f, i) => (
                <div key={i} style={{ display: "flex", flexDirection: "row", gap: "8px", marginBottom: "6px" }}>
                  <select
                    value={f.field ?? ""}
                    onChange={(e) => {
                      const next = [...metricForm.fixed_filters];
                      next[i] = { ...next[i], field: e.target.value };
                      setMetricForm({ ...metricForm, fixed_filters: next });
                    }}
                  >
                    {columnsOf(expandedId, metricForm.table_name).map((c) => (
                      <option key={c.name} value={c.name}>
                        {c.name}
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
                  <input
                    type="text"
                    value={f.value ?? ""}
                    onChange={(e) => {
                      const next = [...metricForm.fixed_filters];
                      next[i] = { ...next[i], value: e.target.value };
                      setMetricForm({ ...metricForm, fixed_filters: next });
                    }}
                    placeholder="값"
                  />
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
              ))}
              <button
                type="button"
                onClick={() => {
                  const first = columnsOf(expandedId, metricForm.table_name)[0];
                  setMetricForm({
                    ...metricForm,
                    fixed_filters: [
                      ...metricForm.fixed_filters,
                      { field: first?.name ?? "", operator: "equals", value: "" },
                    ],
                  });
                }}
                disabled={columnsOf(expandedId, metricForm.table_name).length === 0}
              >
                필터 추가
              </button>
            </div>

            <button
              type="submit"
              disabled={
                savingMetric ||
                !metricForm.table_name ||
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
