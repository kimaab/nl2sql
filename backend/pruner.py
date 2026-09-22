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


class Pruner:
    """질문과 관련된 테이블/지표를 TF-IDF로 선택"""

    def __init__(self, contract: dict):
        self.entries = []

        # 테이블 항목 추가
        for table in contract["tables"]:
            text = " ".join(
                [table["name"], table.get("description", "")]
                + [c.get("description", "") for c in table["columns"]]
                + [c["name"] for c in table["columns"]]
            )
            self.entries.append(("table", table, text))

        # 지표 항목 추가
        for metric in contract.get("metrics", []):
            text = f"{metric['name']} {metric.get('description', '')} {metric['table']}"
            self.entries.append(("metric", metric, text))

        self._tables_by_name = {t["name"]: t for t in contract["tables"]}

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
        for idx in order:
            if scores[idx] <= 0:
                continue
            kind, item, _ = self.entries[idx]
            if kind == "table" and item["name"] not in seen:
                tables.append(item)
                seen.add(item["name"])
            elif kind == "metric":
                metrics.append(item)
                if item["table"] not in seen:
                    tables.append(self._tables_by_name[item["table"]])
                    seen.add(item["table"])

        # 아무것도 걸리지 않으면 상위 몇 개라도 돌려준다
        if not tables:
            for idx in order[:top_k]:
                kind, item, _ = self.entries[idx]
                if kind == "table":
                    tables.append(item)

        return {"tables": tables, "metrics": metrics}
