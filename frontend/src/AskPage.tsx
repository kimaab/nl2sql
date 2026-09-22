import { useState, useEffect } from "react";
import * as api from "./api";

interface Entry {
  question: string;
  sql: string | null;
  error: string | null;
  attempts: number;
}

export function AskPage() {
  const [datasources, setDatasources] = useState<api.Datasource[]>([]);
  const [datasourceId, setDatasourceId] = useState<string>("");
  const [question, setQuestion] = useState("");
  const [history, setHistory] = useState<Entry[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    loadDatasources();
  }, []);

  async function loadDatasources() {
    try {
      const data = await api.listDatasources();
      setDatasources(data.filter((d) => d.synced_at)); // 동기화된 것만
    } catch (err) {
      alert("데이터소스 로드 실패: " + err);
    }
  }

  async function handleSubmit() {
    if (!datasourceId || !question.trim()) {
      alert("데이터소스와 질문을 입력하세요");
      return;
    }

    const asked = question;
    setLoading(true);
    try {
      const res = await api.ask(datasourceId, asked);
      setHistory((prev) => [...prev, { question: asked, ...res }]);
      setQuestion("");
    } catch (err) {
      alert("질문 처리 실패: " + err);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div style={{ padding: "20px" }}>
      <h2>SQL 생성</h2>

      <div style={{ marginBottom: "20px", border: "1px solid #ccc", padding: "15px" }}>
        <div>
          <label>데이터소스: </label>
          <select
            value={datasourceId}
            onChange={(e) => setDatasourceId(e.target.value)}
            disabled={loading}
          >
            <option value="">선택하세요</option>
            {datasources.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name} ({d.driver})
              </option>
            ))}
          </select>
        </div>

        <div style={{ marginTop: "10px" }}>
          <label>질문: </label>
          <textarea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            disabled={loading}
            rows={3}
            style={{ width: "100%", marginTop: "5px" }}
          />
        </div>

        <button
          onClick={handleSubmit}
          disabled={loading || !datasourceId}
          style={{ marginTop: "10px" }}
        >
          {loading ? "생성 중..." : "질문"}
        </button>
      </div>

      <div>
        <h3>결과</h3>
        {history.length === 0 ? (
          <p>질문을 입력하면 결과가 여기에 표시됩니다.</p>
        ) : (
          <ul style={{ listStyleType: "none", padding: 0 }}>
            {history.map((entry, i) => (
              <li
                key={i}
                style={{
                  marginBottom: "20px",
                  padding: "15px",
                  border: "1px solid #ccc",
                  backgroundColor: entry.sql ? "#e8f5e9" : "#ffebee",
                }}
              >
                <p style={{ fontWeight: "bold" }}>Q. {entry.question}</p>
                {entry.sql ? (
                  <div>
                    <p style={{ color: "green" }}>✓ 성공 ({entry.attempts}번 시도)</p>
                    <pre
                      style={{
                        backgroundColor: "#f5f5f5",
                        padding: "10px",
                        borderRadius: "4px",
                        overflow: "auto",
                      }}
                    >
                      {entry.sql}
                    </pre>
                  </div>
                ) : (
                  <div>
                    <p style={{ color: "red" }}>
                      ✗ 실패 ({entry.attempts}번 시도)
                    </p>
                    <p style={{ color: "#d32f2f", fontSize: "0.9em" }}>{entry.error}</p>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
