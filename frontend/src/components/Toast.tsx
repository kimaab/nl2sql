import { createContext, useCallback, useContext, useState, type ReactNode } from "react";

type ToastKind = "info" | "ok" | "error";
interface ToastItem {
  id: number;
  kind: ToastKind;
  text: string;
}

const ToastContext = createContext<(text: string, kind?: ToastKind) => void>(() => {});

export function useToast() {
  return useContext(ToastContext);
}

let nextId = 1;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);

  const push = useCallback((text: string, kind: ToastKind = "info") => {
    const id = nextId++;
    setItems((prev) => [...prev, { id, kind, text }]);
    // 오류는 읽을 시간이 더 필요하다
    setTimeout(() => setItems((prev) => prev.filter((t) => t.id !== id)), kind === "error" ? 6000 : 3000);
  }, []);

  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="toasts">
        {items.map((t) => (
          <div key={t.id} className={`toast ${t.kind}`}>
            {t.text}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
