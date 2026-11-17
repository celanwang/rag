"""Local embedding endpoint for the RAGFlow baseline."""
import os
import secrets
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from .models import Models

MODEL_ID = "bge-small-zh-v1.5"
models = Models()
app = FastAPI(title="RAGFlow PoC embedding endpoint", docs_url=None, redoc_url=None, openapi_url=None)


def authorize(authorization: str | None) -> None:
    key = os.environ.get("RAGFLOW_EMBEDDING_KEY")
    if not key or not secrets.compare_digest((authorization or "").encode(), f"Bearer {key}".encode()):
        raise HTTPException(401, "Invalid embedding key")


class EmbeddingInput(BaseModel):
    model: str
    input: str | list[str]
    encoding_format: str = "float"
    dimensions: int | None = None


@app.get("/v1/models")
def list_models(authorization: Annotated[str | None, Header()] = None):
    authorize(authorization)
    return {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "local-poc"}]}


@app.post("/v1/embeddings")
def embeddings(body: EmbeddingInput, authorization: Annotated[str | None, Header()] = None):
    authorize(authorization)
    if body.model != MODEL_ID or body.encoding_format != "float" or body.dimensions not in (None, 512):
        raise HTTPException(400, "Expected the local 512-dimensional float embedding model")
    texts = [body.input] if isinstance(body.input, str) else body.input
    if not 1 <= len(texts) <= 128:
        raise HTTPException(400, "Embedding batch must have 1 to 128 inputs")
    vectors = models.embed(texts)
    tokens = sum(sum(item.attention_mask) for item in models.embedding.tokenizer.encode_batch(texts))
    return {
        "object": "list", "model": MODEL_ID,
        "data": [
            {"object": "embedding", "index": number, "embedding": vector}
            for number, vector in enumerate(vectors)
        ],
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
    }
