import os
from functools import cached_property

import numpy as np
import onnxruntime as ort
from huggingface_hub import hf_hub_download
from tokenizers import Tokenizer

EMBEDDING = ("Xenova/bge-small-zh-v1.5", "75c43b069aac4d136ba6bc1122f995fedcfd2781", "onnx/model.onnx")
RERANKER = ("Xenova/bge-reranker-base", "280bcc27a84e0b898c251e06fddb25171bd9b101", "onnx/model_quantized.onnx")
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
MODEL_THREADS = int(os.getenv("RAG_MODEL_THREADS", "4"))


class OnnxModel:
    def __init__(self, repo: str, revision: str, weights: str):
        def download(filename: str) -> str:
            return hf_hub_download(repo, filename, revision=revision)

        self.tokenizer = Tokenizer.from_file(download("tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=512)
        pad_token = "[PAD]" if self.tokenizer.token_to_id("[PAD]") is not None else "<pad>"
        self.tokenizer.enable_padding(pad_id=self.tokenizer.token_to_id(pad_token), pad_token=pad_token)

        options = ort.SessionOptions()
        options.intra_op_num_threads = MODEL_THREADS
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(download(weights), options, providers=["CPUExecutionProvider"])
        self.inputs = {entry.name for entry in self.session.get_inputs()}

    def run(self, texts: list[str] | list[tuple[str, str]]) -> np.ndarray:
        tokens = self.tokenizer.encode_batch(texts)
        features = {
            "input_ids": [item.ids for item in tokens],
            "attention_mask": [item.attention_mask for item in tokens],
            "token_type_ids": [item.type_ids for item in tokens],
        }
        return self.session.run(None, {
            name: np.asarray(features[name], dtype=np.int64) for name in self.inputs
        })[0]


class Models:
    @cached_property
    def embedding(self) -> OnnxModel:
        return OnnxModel(*EMBEDDING)

    @cached_property
    def reranker(self) -> OnnxModel:
        return OnnxModel(*RERANKER)

    def embed(self, passages: list[str]) -> list[list[float]]:
        vectors = []
        for start in range(0, len(passages), 16):
            cls = self.embedding.run(passages[start:start + 16])[:, 0, :]
            normalized = cls / np.linalg.norm(cls, axis=1, keepdims=True)
            vectors.extend(normalized.tolist())
        return vectors

    def query(self, question: str) -> list[float]:
        return self.embed([QUERY_PREFIX + question])[0]

    def rerank(self, question: str, passages: list[str]) -> list[float]:
        scores = []
        for start in range(0, len(passages), 8):
            pairs = [(question, passage) for passage in passages[start:start + 8]]
            scores.extend(self.reranker.run(pairs).reshape(-1).tolist())
        return scores


if __name__ == "__main__":
    models = Models()
    print(f"Embedding dimensions: {len(models.query('怎样重置账号？'))}", flush=True)
    print(f"Reranker scores: {models.rerank('怎样重置账号？', ['联系服务台重置密码。', '差旅每天报销两百元。'])}")
