import re
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


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
    """질문과 관련된 테이블/지표를 TF-IDF로 선택"""

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

    def prune(self, question: str, top_k: int = 3) -> dict:
        """질문과 관련된 항목 선택"""
        query_vec = self.vectorizer.transform([question])
        scores = cosine_similarity(query_vec, self.matrix).flatten()
        order = np.argsort(scores)[::-1][:top_k]

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
        return {"tables": tables, "metrics": metrics, "relations": relations}
