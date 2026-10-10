import logging
import re
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

log = logging.getLogger("nl2sql.pruner")


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
    """컬럼 하나가 검색에 기여하는 말 — 이름, 설명."""
    return [column["name"], column.get("description", "")]


class Pruner:
    """질문과 관련된 테이블/지표를 TF-IDF로 선택"""

    def __init__(self, contract: dict):
        self.entries = []

        # 테이블 항목 추가
        for table in contract["tables"]:
            words = [table["name"], table.get("description", "")]
            for column in table["columns"]:
                words.extend(_column_text(column))
            self.entries.append(("table", table, " ".join(words)))

        # 지표 항목 추가
        for metric in contract.get("metrics", []):
            text = f"{metric['name']} {metric.get('description', '')} {metric['table']}"
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

    def prune(self, question: str, top_k: int = 3) -> dict:
        """질문과 관련된 항목 선택"""
        query_vec = self.vectorizer.transform([question])
        scores = cosine_similarity(query_vec, self.matrix).flatten()
        order = np.argsort(scores)[::-1][:top_k]
        self._log_search(question, scores, order)

        tables, metrics, seen = [], [], set()

        def add_table(name):
            if name not in seen and name in self._tables_by_name:
                tables.append(self._tables_by_name[name])
                seen.add(name)

        for idx in order:
            if scores[idx] <= 0:
                continue
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
            for idx in order[:top_k]:
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

    def _log_search(self, question: str, scores, order) -> None:
        """질문 토큰 중 사전에 있어 비교에 쓰인 것과 사전에 없어 무시된 것, 상위 후보를 남긴다."""
        vocab = self.vectorizer.vocabulary_
        tokens = korean_aware_tokens(question)
        used = [t for t in tokens if t in vocab]
        log.info("질문=%r 토큰=%s 사용=%s 무시(사전에 없음)=%s", question, tokens, used,
                 [t for t in tokens if t not in vocab])
        for rank, idx in enumerate(order, start=1):
            kind, item, text = self.entries[idx]
            doc = set(korean_aware_tokens(text))
            log.info("후보 %d: [%s] %s 점수=%.3f 겹친 토큰=%s", rank, kind, item["name"], scores[idx],
                     [t for t in dict.fromkeys(used) if t in doc])
