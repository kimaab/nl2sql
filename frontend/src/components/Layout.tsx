import { NavLink, Outlet } from "react-router-dom";

const GROUPS: { label: string; links: { to: string; text: string; end?: boolean }[] }[] = [
  {
    label: "질문",
    links: [
      { to: "/ask", text: "SQL 생성" },
      { to: "/history", text: "질문 기록" },
    ],
  },
  {
    label: "시스템",
    links: [
      { to: "/systems", text: "목록", end: true },
      { to: "/systems/new", text: "등록" },
      { to: "/sync", text: "동기화", end: true },
      { to: "/sync/history", text: "동기화 현황" },
      { to: "/tables", text: "테이블 용도·카드" },
      { to: "/relations", text: "관계" },
      { to: "/dictionary", text: "컬럼 사전" },
      { to: "/glossary", text: "업무 용어" },
    ],
  },
  {
    label: "지표",
    links: [
      { to: "/metrics", text: "목록", end: true },
      { to: "/metrics/new", text: "등록" },
    ],
  },
  {
    label: "보강",
    links: [{ to: "/review", text: "보강 · 검수 · 평가" }],
  },
];

export function Layout() {
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">n</span>
          nl2sql Studio
        </div>
        {GROUPS.map((g) => (
          <nav key={g.label} className="nav-group">
            <div className="nav-label">{g.label}</div>
            {g.links.map((l) => (
              <NavLink
                key={l.to}
                to={l.to}
                end={l.end}
                className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}
              >
                {l.text}
              </NavLink>
            ))}
          </nav>
        ))}
      </aside>
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}
