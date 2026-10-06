import { useEffect, useState, type FormEvent } from "react";
import { useNavigate, useParams } from "react-router-dom";
import * as api from "../../api";
import { Button, Field, Loading, PageHeader } from "../../components/ui";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/format";

const EMPTY: api.DatasourceInput = {
  name: "",
  description: "",
  driver: "postgresql",
  host: "",
  port: 0,
  db_name: "",
  db_schema: "",
  username: "",
  password: "",
};

export function DatasourceForm() {
  const { id } = useParams();
  const editing = Boolean(id);
  const [form, setForm] = useState<api.DatasourceInput>(EMPTY);
  const [loading, setLoading] = useState(editing);
  const [saving, setSaving] = useState(false);
  const toast = useToast();
  const navigate = useNavigate();

  useEffect(() => {
    if (!id) return;
    api
      .getDatasource(id)
      .then((ds) =>
        setForm({
          name: ds.name,
          description: ds.description,
          driver: ds.driver,
          host: ds.host,
          port: ds.port,
          db_name: ds.db_name,
          db_schema: ds.db_schema,
          username: ds.username,
          password: "",
        })
      )
      .catch((err) => {
        toast("데이터소스를 불러오지 못했습니다: " + errorMessage(err), "error");
        navigate("/datasources");
      })
      .finally(() => setLoading(false));
  }, [id]);

  const set = <K extends keyof api.DatasourceInput>(key: K, value: api.DatasourceInput[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    try {
      setSaving(true);
      if (id) {
        await api.updateDatasource(id, form);
        toast("수정했습니다", "ok");
        navigate("/datasources");
      } else {
        const created = await api.createDatasource(form);
        toast("등록했습니다. 이어서 스키마를 동기화하세요", "ok");
        navigate(`/sync?ds=${created.id}`);
      }
    } catch (err) {
      toast("저장 실패: " + errorMessage(err), "error");
    } finally {
      setSaving(false);
    }
  }

  if (loading) return <Loading />;

  return (
    <div className="page narrow">
      <PageHeader
        back={{ to: "/datasources", label: "데이터소스 목록" }}
        title={editing ? "데이터소스 편집" : "새 데이터소스 등록"}
        desc="읽기 전용 계정을 권장합니다. 비밀번호는 저장 후 다시 표시되지 않습니다."
      />

      <form className="card" onSubmit={handleSubmit} autoComplete="off">
        <div className="card-pad">
          <div className="form-grid">
            <Field label="이름" required>
              <input
                className="input"
                value={form.name}
                onChange={(e) => set("name", e.target.value)}
                required
              />
            </Field>
            <Field label="드라이버" required>
              <select
                className="select"
                value={form.driver}
                onChange={(e) => set("driver", e.target.value as api.DatasourceInput["driver"])}
              >
                <option value="postgresql">PostgreSQL</option>
                <option value="mysql">MySQL</option>
                <option value="oracle">Oracle</option>
              </select>
            </Field>
            <Field label="설명" full>
              <input
                className="input"
                value={form.description}
                onChange={(e) => set("description", e.target.value)}
              />
            </Field>

            <Field label="호스트" required>
              <input
                className="input"
                value={form.host}
                onChange={(e) => set("host", e.target.value)}
                required
              />
            </Field>
            <Field label="포트" hint="비우면 드라이버 기본값을 씁니다">
              <input
                className="input"
                type="number"
                value={form.port || ""}
                onChange={(e) => set("port", e.target.value ? parseInt(e.target.value) : 0)}
              />
            </Field>

            <Field label="데이터베이스 이름" required>
              <input
                className="input"
                value={form.db_name}
                onChange={(e) => set("db_name", e.target.value)}
                required
              />
            </Field>
            {form.driver !== "mysql" && (
              <Field
                label="스키마"
                hint={form.driver === "postgresql" ? "기본값: public" : "기본값: 계정명"}
              >
                <input
                  className="input"
                  value={form.db_schema}
                  onChange={(e) => set("db_schema", e.target.value)}
                />
              </Field>
            )}

            <Field label="사용자명">
              <input
                className="input"
                value={form.username}
                onChange={(e) => set("username", e.target.value)}
                autoComplete="off"
              />
            </Field>
            <Field
              label="비밀번호"
              hint={editing ? "비워두면 저장된 비밀번호를 그대로 씁니다" : undefined}
            >
              <input
                className="input"
                type="password"
                value={form.password}
                onChange={(e) => set("password", e.target.value)}
                autoComplete="new-password"
              />
            </Field>
          </div>
        </div>
        <div className="form-actions">
          <Button variant="ghost" onClick={() => navigate("/datasources")}>
            취소
          </Button>
          <Button type="submit" variant="primary" disabled={saving}>
            {saving ? "저장 중..." : editing ? "수정" : "등록"}
          </Button>
        </div>
      </form>
    </div>
  );
}
