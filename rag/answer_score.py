"""Score captured synthetic answers. Ragas and its judge are only loaded here."""
import argparse
import asyncio
import hashlib
import json
import math
import os
from importlib.metadata import version
from pathlib import Path

from .generation import ANSWER_BASE_URL, ANSWER_MODEL

METRICS = ("faithfulness", "factual_correctness")


async def evaluate(capture: dict, scorer, *, jobs: int = 1) -> dict:
    limit = asyncio.Semaphore(jobs)

    async def grade(row):
        answer, case = row["answer"], row["case"]
        response = "\n".join(claim["text"] for claim in answer["claims"])
        scores = {}
        for name in METRICS:
            if not response:
                record = {"status": "skipped", "reason": "no_factual_claims"}
            elif name == "faithfulness" and not answer.get("context"):
                record = {"status": "failed", "reason": "missing_captured_context"}
            elif name == "factual_correctness" and not case.get("reference_answer"):
                record = {"status": "skipped", "reason": "no_reviewed_reference"}
            elif not all(row["checks"][key] for key in ("authorization", "citations", "policy")):
                record = {"status": "skipped", "reason": "deterministic_check_failed"}
            else:
                try:
                    async with limit:
                        value = await asyncio.wait_for(scorer(name, case, answer, response), timeout=60)
                    if not math.isfinite(value) or not 0 <= value <= 1:
                        raise ValueError("Judge score outside unit interval")
                    record = {"status": "scored", "score": value}
                except Exception as error:
                    record = {"status": "failed", "reason": type(error).__name__}
            scores[name] = record
        return {"id": case["id"], "split": case["split"], "strategy": row["strategy"],
                "trial": row["trial"], "scores": scores}

    rows = await asyncio.gather(*(grade(row) for row in capture["results"]))
    summary = {name: {"scored": 0, "skipped": 0, "failed": 0} for name in METRICS}
    for row in rows:
        scores = row["scores"]
        for name, record in scores.items():
            summary[name][record["status"]] += 1
        print(f"{row['id']} {row['strategy']} " + " ".join(
            f"{key}={value.get('score', value['status'])}" for key, value in scores.items()
        ), flush=True)
    return {"dataset_sha256": capture["dataset_sha256"], "summary": summary, "results": rows}


async def real_scores(capture, *, jobs):
    os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")
    from instructor import Mode
    from openai import AsyncOpenAI
    from ragas.llms import llm_factory
    from ragas.metrics.collections import Faithfulness, FactualCorrectness

    key = os.environ.get("RAG_JUDGE_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        raise SystemExit("Set RAG_JUDGE_API_KEY or DASHSCOPE_API_KEY")
    model = os.environ.get("RAG_JUDGE_MODEL", ANSWER_MODEL)
    endpoint = os.environ.get("RAG_JUDGE_BASE_URL", ANSWER_BASE_URL)
    async with AsyncOpenAI(api_key=key, base_url=endpoint, max_retries=0, timeout=15) as client:
        llm = llm_factory(model, client=client, mode=Mode.JSON, temperature=0, max_tokens=2048)
        faith = Faithfulness(llm=llm)
        fact = FactualCorrectness(llm=llm, mode="f1")

        async def score(name, case, answer, response):
            if name == "faithfulness":
                contexts = [json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in answer["context"]]
                item = await faith.ascore(user_input=case["question"], response=response, retrieved_contexts=contexts)
            else:
                item = await fact.ascore(response=response, reference=case["reference_answer"])
            return float(item.value)

        report = await evaluate(capture, score, jobs=jobs)
        report["judge"] = {"model": model, "base_url": endpoint, "temperature": 0,
                           "ragas": version("ragas"), "mode": "json", "factual_correctness": "f1", "jobs": jobs}
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=1, help="Maximum concurrent metric scores")
    args = parser.parse_args()
    if not 1 <= args.jobs <= 8:
        parser.error("--jobs must be between 1 and 8")
    raw = args.capture.read_bytes()
    report = asyncio.run(real_scores(json.loads(raw), jobs=args.jobs))
    report["capture_sha256"] = hashlib.sha256(raw).hexdigest()
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(args.out.resolve())


if __name__ == "__main__":
    main()
