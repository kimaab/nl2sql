export function formatAgo(dateStr: string | null): string {
  if (!dateStr) return "동기화 안 됨";
  const diff = Date.now() - new Date(dateStr).getTime();
  const minutes = Math.floor(diff / 60000);
  const hours = Math.floor(diff / 3600000);
  const days = Math.floor(diff / 86400000);
  if (minutes < 1) return "방금 전";
  if (minutes < 60) return `${minutes}분 전`;
  if (hours < 24) return `${hours}시간 전`;
  return `${days}일 전`;
}

export function formatDateTime(dateStr: string | null): string {
  if (!dateStr) return "-";
  return new Date(dateStr).toLocaleString("ko-KR", { hour12: false });
}

export function errorMessage(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err);
  // 백엔드는 {"detail": "..."} 형태로 내려주는 경우가 많다
  try {
    const parsed = JSON.parse(raw);
    if (typeof parsed?.detail === "string") return parsed.detail;
    // 입력 검증 실패(422)는 [{msg: "Value error, ..."}] 목록이다
    if (Array.isArray(parsed?.detail))
      return parsed.detail
        .map((d: { msg?: string }) => String(d?.msg ?? "").replace(/^Value error, /, ""))
        .filter(Boolean)
        .join("; ");
  } catch {
    /* 평문 */
  }
  return raw;
}
