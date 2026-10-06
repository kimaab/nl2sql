import type { ButtonHTMLAttributes, ReactNode } from "react";
import { Link } from "react-router-dom";

type Variant = "primary" | "secondary" | "ghost" | "danger";

export function Button({
  variant = "secondary",
  size,
  className = "",
  type = "button",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: "sm" }) {
  return (
    <button
      type={type}
      className={`btn ${variant} ${size ?? ""} ${className}`}
      {...rest}
    />
  );
}

export function PageHeader({
  title,
  desc,
  actions,
  back,
}: {
  title: string;
  desc?: ReactNode;
  actions?: ReactNode;
  back?: { to: string; label: string };
}) {
  return (
    <>
      {back && (
        <Link to={back.to} className="back-link">
          ← {back.label}
        </Link>
      )}
      <div className="page-header">
        <div>
          <h1 className="page-title">{title}</h1>
          {desc && <p className="page-desc">{desc}</p>}
        </div>
        {actions && <div className="page-actions">{actions}</div>}
      </div>
    </>
  );
}

export function Field({
  label,
  hint,
  error,
  required,
  full,
  children,
}: {
  label: string;
  hint?: ReactNode;
  error?: string | null;
  required?: boolean;
  full?: boolean;
  children: ReactNode;
}) {
  return (
    <div className={`field ${full ? "full" : ""}`}>
      <label className="field-label">
        {label}
        {required && <span className="req">*</span>}
      </label>
      {children}
      {error ? (
        <span className="field-error">{error}</span>
      ) : (
        hint && <span className="field-hint">{hint}</span>
      )}
    </div>
  );
}

export type Tone = "info" | "ok" | "warn" | "danger" | "primary";

export function Badge({ tone = "info", children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

export function Alert({ tone = "info", children }: { tone?: Exclude<Tone, "primary">; children: ReactNode }) {
  return <div className={`alert ${tone}`}>{children}</div>;
}

export function Chip({
  selected,
  order,
  type,
  onClick,
  children,
}: {
  selected?: boolean;
  order?: number;
  type?: string;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button type="button" className={`chip ${selected ? "selected" : ""}`} onClick={onClick}>
      {selected && order !== undefined && <span className="order">{order}</span>}
      {children}
      {type && <span className="type">{type}</span>}
    </button>
  );
}

export function OptionCard({
  selected,
  icon,
  title,
  desc,
  onClick,
}: {
  selected: boolean;
  icon: string;
  title: string;
  desc: string;
  onClick: () => void;
}) {
  return (
    <button type="button" className={`option-card ${selected ? "selected" : ""}`} onClick={onClick}>
      <span className="oc-icon">{icon}</span>
      <span>
        <div className="oc-title">{title}</div>
        <div className="oc-desc">{desc}</div>
      </span>
    </button>
  );
}

export function Segmented<T extends string>({
  options,
  value,
  onChange,
}: {
  options: { value: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div className="segmented">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          className={o.value === value ? "active" : ""}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function EmptyState({
  title,
  desc,
  action,
}: {
  title: string;
  desc?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <div className="empty-title">{title}</div>
      {desc && <p className="empty-desc">{desc}</p>}
      {action}
    </div>
  );
}

export function Loading() {
  return <div className="loading">불러오는 중...</div>;
}

const SQL_KEYWORDS =
  /\b(SELECT|FROM|WHERE|AND|OR|JOIN|LEFT|RIGHT|INNER|OUTER|ON|GROUP BY|ORDER BY|HAVING|LIMIT|AS|IN|NOT|NULL|IS|LIKE|BETWEEN|DISTINCT|COUNT|SUM|AVG|MIN|MAX|CASE|WHEN|THEN|ELSE|END|BY|DESC|ASC|FETCH|FIRST|ROWS|ONLY|OFFSET|UNION|ALL|WITH|OVER|PARTITION)\b/gi;
const TOKEN = new RegExp(
  `(<[^>\n]*>)|('(?:[^']|'')*')|(\b\d+(?:\.\d+)?\b)|(${SQL_KEYWORDS.source})`,
  "gi"
);

/** 의존성 없는 가벼운 SQL 하이라이트. 정확한 파서가 아니라 읽기 편하게 하는 용도다. */
export function SqlBlock({ sql }: { sql: string }) {
  const parts: ReactNode[] = [];
  let last = 0;
  let key = 0;
  for (const m of sql.matchAll(TOKEN)) {
    const idx = m.index ?? 0;
    if (idx > last) parts.push(sql.slice(last, idx));
    const cls = m[1] ? "ph" : m[2] ? "str" : m[3] ? "num" : "kw";
    parts.push(
      <span key={key++} className={cls}>
        {m[0]}
      </span>
    );
    last = idx + m[0].length;
  }
  if (last < sql.length) parts.push(sql.slice(last));
  return <pre className="sql">{parts}</pre>;
}
