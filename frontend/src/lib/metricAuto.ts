import { useEffect, useRef } from "react";
import type { Tone } from "../components/ui";

export const RUN_STATUS_LABEL: Record<string, string> = {
  running: "진행 중",
  done: "완료",
  error: "오류",
  interrupted: "중단됨",
};
export const RUN_STATUS_TONE: Record<string, Tone> = {
  running: "primary",
  done: "ok",
  error: "danger",
  interrupted: "warn",
};

export const ITEM_STATUS_LABEL: Record<string, string> = {
  registered: "등록됨",
  failed: "실패",
  skipped: "건너뜀",
};
export const ITEM_STATUS_TONE: Record<string, Tone> = {
  registered: "ok",
  failed: "danger",
  skipped: "warn",
};

/** 자동 등록 후보가 멈춘 단계 */
export const AUTO_STAGE_LABEL: Record<string, string> = {
  llm: "LLM 응답",
  duplicate: "이름 중복",
  simple: "단순 집계",
  parse: "AST 변환",
  compile: "컴파일",
  guard: "실행 전 확인",
  verify: "결과 비교",
  save: "저장",
};

export const VERDICT_LABEL: Record<string, string> = {
  pass: "통과",
  warn: "주의",
  fail: "실패",
  skip: "건너뜀",
};
export const VERDICT_TONE: Record<string, Tone> = {
  pass: "ok",
  warn: "warn",
  fail: "danger",
  skip: "info",
};

/** active 인 동안 fn 을 주기적으로 부른다 (진행 중인 실행을 다시 읽을 때) */
export function usePolling(active: boolean, fn: () => void, ms = 2000) {
  const saved = useRef(fn);
  saved.current = fn;
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => saved.current(), ms);
    return () => clearInterval(timer);
  }, [active, ms]);
}
