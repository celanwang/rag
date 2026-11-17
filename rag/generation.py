import os
from collections.abc import Iterator
from contextlib import contextmanager

import httpx

from .models import ModelError

ANSWER_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/"
ANSWER_MODEL = "qwen-plus"


class AnswerModel:
    def __init__(self, client: httpx.Client, name: str):
        self.client = client
        self.name = name

    @classmethod
    @contextmanager
    def from_env(cls) -> Iterator["AnswerModel | None"]:
        key = os.environ.get("RAG_ANSWER_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")
        if not key:
            yield None
            return
        url = os.environ.get("RAG_ANSWER_BASE_URL", ANSWER_BASE_URL).rstrip("/") + "/"
        with httpx.Client(base_url=url, headers={"Authorization": f"Bearer {key}"}, timeout=20) as client:
            yield cls(client, os.environ.get("RAG_ANSWER_MODEL", ANSWER_MODEL))

    def generate(self, system: str, prompt: str, *, timeout: float) -> tuple[str, dict[str, int]]:
        response = self.client.post("chat/completions", timeout=timeout, json={
            "model": self.name,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 850,
        })
        response.raise_for_status()
        try:
            payload = response.json()
            choice = payload["choices"][0]
            content = choice["message"]["content"]
            if choice.get("finish_reason") != "stop" or not isinstance(content, str):
                raise ValueError("Incomplete generation")
            usage = payload.get("usage") or {}
            return content, {key: usage[key] for key in ("prompt_tokens", "completion_tokens")
                             if type(usage.get(key)) is int and usage[key] >= 0}
        except (IndexError, KeyError, TypeError, ValueError) as error:
            raise ModelError("Invalid generation response") from error
