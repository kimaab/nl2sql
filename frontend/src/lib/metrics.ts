import type { Metric, MetricInput } from "../api";

export const EMPTY_METRIC: MetricInput = {
  name: "",
  description: "",
  synonyms: [],
  sql: "",
};

export const STATUS_LABEL: Record<string, string> = {
  active: "사용 중",
  broken: "깨짐",
};
export const STATUS_TONE: Record<string, "ok" | "warn" | "danger" | undefined> = {
  active: "ok",
  broken: "danger",
};

/** 등록 확인에서 실패한 단계 */
export const CHECK_STAGE_LABEL: Record<string, string> = {
  parse: "AST 로 옮기지 못했습니다",
  compile: "옮긴 AST 를 컴파일하지 못했습니다",
  verify: "원본과 결과를 비교하지 못했습니다",
};

/** 저장된 지표를 수정 폼의 입력으로 */
export function toInput(m: Metric): MetricInput {
  return { name: m.name, description: m.description, synonyms: m.synonyms ?? [], sql: m.sql };
}

/** 서버 SQL 을 읽기 좋게 절마다 줄을 바꾼다 */
export const formatSql = (sql: string) =>
  sql.replace(/ (FROM|WHERE|GROUP BY|HAVING|ORDER BY|LIMIT|FETCH FIRST|LEFT JOIN|JOIN) /g, "\n$1 ");
