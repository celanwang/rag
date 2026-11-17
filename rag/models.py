import os
from collections.abc import Iterator
from contextlib import contextmanager
from math import isfinite
from operator import itemgetter

import httpx

EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_DIMENSIONS = 512
EMBEDDING_BATCH_SIZE = 10
RERANK_MODEL = "qwen3-rerank"
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/api/v1/"


class ModelError(RuntimeError):
    pass


def ordered_values(rows: list[dict], index: str, field: str, count: int) -> list:
    try:
        ordered = sorted(rows, key=itemgetter(index))
        if [row[index] for row in ordered] == list(range(count)):
            return [row[field] for row in ordered]
    except (KeyError, TypeError):
        pass
    raise ModelError("Bailian returned invalid result indices or values.")


def finite_number(value: object) -> bool:
    return type(value) in (int, float) and isfinite(value)


class BailianModels:
    def __init__(self, client: httpx.Client):
        self.client = client

    @classmethod
    @contextmanager
    def from_env(cls) -> Iterator["BailianModels"]:
        key = os.environ.get("DASHSCOPE_API_KEY")
        if not key:
            raise ModelError("Set DASHSCOPE_API_KEY before calling Bailian.")
        base_url = os.environ.get("DASHSCOPE_HTTP_BASE_URL", DASHSCOPE_BASE_URL)
        with httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {key}"}, timeout=20) as client:
            yield cls(client)

    def request(self, path: str, model: str, inputs: dict, parameters: dict, field: str,
                timeout: float | None = None) -> list[dict]:
        options = {"timeout": timeout} if timeout is not None else {}
        response = self.client.post(path, json={"model": model, "input": inputs, "parameters": parameters}, **options)
        response.raise_for_status()
        try:
            return response.json()["output"][field]
        except (KeyError, TypeError, ValueError):
            raise ModelError(f"Bailian returned an invalid response for {model}.") from None

    def _embed(self, texts: list[str], text_type: str, timeout: float | None = None) -> list[list[float]]:
        vectors = []
        for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
            batch = texts[start:start + EMBEDDING_BATCH_SIZE]
            rows = self.request(
                "services/embeddings/text-embedding/text-embedding",
                EMBEDDING_MODEL,
                {"texts": batch},
                {"dimension": EMBEDDING_DIMENSIONS, "text_type": text_type},
                "embeddings",
                timeout,
            )
            batch_vectors = ordered_values(rows, "text_index", "embedding", len(batch))
            if any(
                not isinstance(vector, list) or len(vector) != EMBEDDING_DIMENSIONS
                or not all(finite_number(component) for component in vector)
                for vector in batch_vectors
            ):
                raise ModelError(f"Bailian returned an invalid vector for {EMBEDDING_MODEL}.")
            vectors.extend(batch_vectors)
        return vectors

    def embed(self, passages: list[str]) -> list[list[float]]:
        return self._embed(passages, "document")

    def query(self, question: str, *, timeout: float | None = None) -> list[float]:
        return self._embed([question], "query", timeout)[0]

    def rerank(self, question: str, passages: list[str], *, timeout: float | None = None) -> list[float]:
        if not passages:
            return []
        rows = self.request(
            "services/rerank/text-rerank/text-rerank",
            RERANK_MODEL,
            {"query": question, "documents": passages},
            {"top_n": len(passages), "return_documents": False},
            "results",
            timeout,
        )
        scores = ordered_values(rows, "index", "relevance_score", len(passages))
        if not all(finite_number(score) for score in scores):
            raise ModelError(f"Bailian returned invalid scores for {RERANK_MODEL}.")
        return scores


def main() -> None:
    question = "怎样重置密码？"
    passages = ["差旅报销需要发票。", "联系服务台重置密码。"]
    try:
        with BailianModels.from_env() as models:
            vectors = models.embed(passages)
            query = models.query(question)
            scores = models.rerank(question, passages)
    except httpx.HTTPStatusError as error:
        raise SystemExit(f"Bailian returned HTTP {error.response.status_code}.") from None
    except httpx.RequestError as error:
        raise SystemExit(f"Bailian request failed ({type(error).__name__}).") from None
    except ModelError as error:
        raise SystemExit(str(error)) from None
    print(f"{EMBEDDING_MODEL}: {len(vectors)} document vectors and one query vector with {len(query)} dimensions")
    print(f"{RERANK_MODEL}: {len(scores)} relevance scores")


if __name__ == "__main__":
    main()
