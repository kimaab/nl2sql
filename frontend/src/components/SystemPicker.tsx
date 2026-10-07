import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import * as api from "../api";

/** ?ds= 로 고른 시스템. 동기화된 시스템이 하나뿐이면 저절로 고른다. */
export function useSystemParam() {
  const [params, setParams] = useSearchParams();
  const sysId = params.get("ds") ?? "";
  const [systems, setSystems] = useState<api.System[] | null>(null);

  useEffect(() => {
    api
      .listSystems()
      .then((list) => {
        const ready = list.filter((s) => s.synced_at);
        setSystems(ready);
        if (!sysId && ready.length === 1) setParams({ ds: ready[0].id });
      })
      .catch(() => setSystems([]));
  }, []);

  const setSysId = (id: string) => setParams(id ? { ds: id } : {});
  return { systems, sysId, setSysId, system: systems?.find((s) => s.id === sysId) ?? null };
}

export function SystemPicker({
  systems,
  value,
  onChange,
}: {
  systems: api.System[] | null;
  value: string;
  onChange: (id: string) => void;
}) {
  return (
    <select className="select" value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">시스템 선택</option>
      {(systems ?? []).map((s) => (
        <option key={s.id} value={s.id}>
          {s.name}
        </option>
      ))}
    </select>
  );
}
