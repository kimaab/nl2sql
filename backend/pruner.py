import logging
import re
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

log = logging.getLogger("nl2sql.pruner")

# RRF 상수. 순위 차이를 완만하게 만든다 — 1위와 2위의 점수 차가 지나치게 벌어지지 않게.
RRF_K = 60
# 후보 표 로그의 최대 줄 수 (테이블이 많으면 TF-IDF 에 조금씩 걸린 후보가 길게 이어진다)
MAX_LOG_ROWS = 15


def korean_aware_tokens(text: str) -> list[str]:
    """영문/숫자는 통째로, 한글은 2-gram으로 토큰화"""
    found = []
    for match in re.finditer(r"[a-zA-Z0-9_]+|[가-힣]+", text.lower()):
        chunk = match.group()
        if chunk[0].isascii() or len(chunk) == 1:
            found.append(chunk)
        else:
            found.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
    return found


def _column_text(column: dict) -> list[str]:
    """컬럼 하나가 검색에 기여하는 말 — 이름, 설명, 동의어, 코드값 이름."""
    return (
        [column["name"], column.get("description", "")]
        + list(column.get("synonyms", []))
        + [str(c.get("label", "")) for c in column.get("codes", [])]
    )


class Pruner:
    """질문과 관련된 테이블/지표를 TF-IDF로 선택 (의미 검색 점수가 오면 지표 순위에 합친다)"""

    def __init__(self, contract: dict):
        self.entries = []

        # 테이블 항목 추가
        for table in contract["tables"]:
            words = [table["name"], table.get("description", "")]
            for column in table["columns"]:
                words.extend(_column_text(column))
            self.entries.append(("table", table, " ".join(words)))

        # 지표 항목 추가 — 예시 질문도 검색 본문이다
        for metric in contract.get("metrics", []):
            examples = " ".join(e.get("question", "") for e in metric.get("examples", []))
            text = f"{metric['name']} {metric.get('description', '')} {metric['table']} {examples}"
            self.entries.append(("metric", metric, text))

        self._tables_by_name = {t["name"]: t for t in contract["tables"]}
        self._relations = contract.get("relations", [])

        # TF-IDF 벡터화
        self.vectorizer = TfidfVectorizer(
            tokenizer=korean_aware_tokens,
            token_pattern=None
        )
        self.matrix = self.vectorizer.fit_transform([e[2] for e in self.entries])

        vocab_size = len(self.vectorizer.vocabulary_)
        log.info("단어 사전 %d개 (테이블 %d, 지표 %d)", vocab_size,
                 sum(1 for e in self.entries if e[0] == "table"), sum(1 for e in self.entries if e[0] == "metric"))
        for kind, item, text in self.entries:
            words = sorted(set(korean_aware_tokens(text)))
            if kind == "metric":
                log.info("지표 사전: %s -> %s", item["name"], words)
            else:
                log.info("테이블 사전: %s -> 단어 %d개 (컬럼 %d개) %s",
                         item["name"], len(words), len(item["columns"]), words)

    def prune(self, question: str, top_k: int = 3, semantic: dict[str, float] | None = None,
              semantic_min: float = 0.0) -> dict:
        """질문과 관련된 항목 선택.

        semantic: 의미 검색이 낸 {지표 이름: 유사도}. semantic_min 이상인 지표만 순위에 들고, TF-IDF 순위와
        RRF 로 합친다 — 점수는 단위가 달라 더할 수 없으니 순위만 쓴다. None 이면 TF-IDF 만 쓴다.
        """
        query_vec = self.vectorizer.transform([question])
        scores = cosine_similarity(query_vec, self.matrix).flatten()
        passed = {k: v for k, v in (semantic or {}).items() if v >= semantic_min}
        ranked, fused = self._rank(scores, passed)
        order = ranked[:top_k]
        self._log_search(question, scores, ranked, top_k, semantic, semantic_min, fused)

        tables, metrics, seen = [], [], set()

        def add_table(name):
            if name not in seen and name in self._tables_by_name:
                tables.append(self._tables_by_name[name])
                seen.add(name)

        for idx in order:
            kind, item, _ = self.entries[idx]
            if kind == "table":
                add_table(item["name"])
            elif kind == "metric":
                metrics.append(item)
                add_table(item["table"])
                # 조인 지표는 조인한 테이블도 함께 실어야 "테이블.컬럼" 을 쓸 수 있다
                for join in item.get("joins", []):
                    add_table(join.get("table"))

        # 아무것도 걸리지 않으면 상위 몇 개라도 돌려준다
        if not tables:
            for idx in np.argsort(scores)[::-1][:top_k]:
                kind, item, _ = self.entries[idx]
                if kind == "table":
                    add_table(item["name"])

        # 고른 테이블과 관계로 이어진 이웃도 질문과 겹치면 싣는다. "등급별 주문 금액" 에서
        # 주문 테이블만 실리면 등급이 있는 회원 테이블로 조인할 길이 없다.
        table_scores = {
            self.entries[i][1]["name"]: scores[i] for i in range(len(self.entries)) if self.entries[i][0] == "table"
        }
        for r in self._relations:
            for here, there in ((r["left_table"], r["right_table"]), (r["right_table"], r["left_table"])):
                if here in seen and there not in seen and table_scores.get(there, 0) > 0:
                    add_table(there)

        relations = [
            r for r in self._relations
            if r["left_table"] in seen and r["right_table"] in seen
        ]
        log.info("선택: 지표=%s 테이블=%s", [m["name"] for m in metrics], [t["name"] for t in tables])
        return {"tables": tables, "metrics": metrics, "relations": relations}

    def _rank(self, scores, semantic: dict[str, float]) -> tuple[list[int], dict[int, float]]:
        """후보 순서와 RRF 점수. 어느 한쪽 검색에라도 걸린 항목만 후보가 된다."""
        keyword = [int(i) for i in np.argsort(scores)[::-1] if scores[i] > 0]
        if not semantic:
            return keyword, {}
        meaning = sorted(
            (i for i, (kind, item, _) in enumerate(self.entries) if kind == "metric" and item["name"] in semantic),
            key=lambda i: -semantic[self.entries[i][1]["name"]],
        )
        fused: dict[int, float] = {}
        for ranking in (keyword, meaning):
            for rank, idx in enumerate(ranking, start=1):
                fused[idx] = fused.get(idx, 0.0) + 1 / (RRF_K + rank)
        return sorted(fused, key=lambda i: (-fused[i], -scores[i])), fused

    def _log_search(self, question: str, scores, ranked: list[int], top_k: int,
                    semantic: dict[str, float] | None, semantic_min: float, fused: dict[int, float]) -> None:
        """질문 토큰(사전에 있어 쓰인 것·없어 무시된 것)과 후보 표를 남긴다.

        후보 한 줄: TF-IDF 점수(순위) | 의미 점수(순위) | 결합 점수(=두 순위 몫의 합) | 겹친 토큰.
        의미 검색 기준 미만으로 빠진 지표도 '기준 미만' 으로 함께 남긴다 — 왜 못 찾았는지 보려고.
        """
        vocab = self.vectorizer.vocabulary_
        tokens = korean_aware_tokens(question)
        used = list(dict.fromkeys(t for t in tokens if t in vocab))
        log.info("질문=%r 토큰=%s 사용=%s 무시(사전에 없음)=%s", question, tokens, used,
                 [t for t in tokens if t not in vocab])

        keyword_rank = {idx: r for r, idx in enumerate((i for i in np.argsort(scores)[::-1] if scores[i] > 0), 1)}
        passed = sorted((k for k, v in (semantic or {}).items() if v >= semantic_min), key=lambda k: -semantic[k])
        meaning_rank = {name: r for r, name in enumerate(passed, start=1)}
        below = {
            i for i, (kind, item, _) in enumerate(self.entries)
            if kind == "metric" and item["name"] in (semantic or {}) and item["name"] not in meaning_rank
        }

        if semantic is None:
            log.info("후보 (TF-IDF 만, 의미 검색 꺼짐) — 상위 %d개 선택", top_k)
        else:
            log.info("후보 (결합 = 1/(%d+TF-IDF 순위) + 1/(%d+의미 순위), 의미 기준 %.2f) — 상위 %d개 선택",
                     RRF_K, RRF_K, semantic_min, top_k)

        # 기준 미만이어도 TF-IDF 로 걸렸으면 이미 순위 안에 있다 — 순위 밖인 것만 아래에 붙인다
        rows = ranked + sorted(below - set(ranked), key=lambda i: -semantic[self.entries[i][1]["name"]])
        for pos, idx in enumerate(rows[:MAX_LOG_ROWS]):
            kind, item, text = self.entries[idx]
            mark = f"#{pos + 1:<2d} {'✔' if pos < top_k else ' '}" if pos < len(ranked) else "-    "
            line = f"{mark} [{kind}] {item['name']}"
            line += f"  TF-IDF {scores[idx]:.3f} ({keyword_rank[idx]}위)" if idx in keyword_rank else "  TF-IDF -"
            if semantic is not None:
                meaning = semantic.get(item["name"]) if kind == "metric" else None
                if meaning is None:
                    line += " | 의미 -"
                elif item["name"] in meaning_rank:
                    line += f" | 의미 {meaning:.3f} ({meaning_rank[item['name']]}위)"
                else:
                    line += f" | 의미 {meaning:.3f} (기준 미만)"
                parts = []
                if idx in keyword_rank:
                    parts.append(1 / (RRF_K + keyword_rank[idx]))
                if item["name"] in meaning_rank and kind == "metric":
                    parts.append(1 / (RRF_K + meaning_rank[item["name"]]))
                line += (f" | 결합 {sum(parts):.4f} (" + " + ".join(f"{p:.4f}" for p in parts) + ")") if parts else " | 결합 -"
            overlap = [t for t in used if t in set(korean_aware_tokens(text))]
            if overlap:
                line += f" | 겹친 토큰 {overlap}"
            log.info("  %s", line)
        if len(rows) > MAX_LOG_ROWS:
            log.info("  … 외 %d개", len(rows) - MAX_LOG_ROWS)
