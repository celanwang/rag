from statistics import mean
from uuid import UUID

from poc.benchmark import metrics

from .retrieval import rrf


def evidence_ranks(row: dict, sources: dict, stages: dict[str, list[str]]) -> dict:
    indexed = {stage: {chunk: rank for rank, chunk in enumerate(ids, 1)} for stage, ids in stages.items()}
    return {
        chunk: {
            "source": sources.get(chunk),
            "gold": chunk in row["gold_ids"],
            "ranks": {stage: ranks.get(chunk) for stage, ranks in indexed.items()},
        }
        for chunk in sorted(set(row["gold_ids"]) | set(row.get("forbidden_top1_ids", [])))
    }


def replay(report: dict, configuration: list[int]) -> dict:
    metadata = report["metadata"]
    recorded_k = metadata["rrf_k"]
    sources = report.get("evidence_sources", {})
    observed = []
    for row in report["requests"]:
        if row["mode"] not in ("rrf", "rerank"):
            continue
        tuning = row.get("parameters", metadata["tuning"])
        top_k, limit = tuning["top_k"], tuning["rerank_limit"]
        gold = set(row["gold_ids"])
        branches = row["recall"]
        uuid_ranks = [list(map(UUID, ids)) for ids in branches.values()]
        expected = list(map(str, rrf(uuid_ranks, k=recorded_k)))
        if expected != row["prefinal"]:
            raise ValueError(f"Cannot reproduce recorded fusion for {row['id']} trial {row['trial']} ({row['mode']})")
        recorded_stages = {"fusion": expected, "top5": row["ranked_ids"], **branches}
        if row["mode"] == "rerank":
            recorded_stages["rerank_input"] = row.get("rerank_input", expected[:limit])
        comparisons = {}
        for k in configuration:
            fused = list(map(str, rrf(uuid_ranks, k=k)))
            top = fused[:top_k]
            shortlist = fused[:limit]
            stages = {**branches, "fusion": fused, "fusion_top5": top, "rerank_window": shortlist}
            comparisons[str(k)] = {
                "metrics": metrics(top, gold),
                "all_gold_in_top5": gold.issubset(top),
                "rerank_window_recall": len(gold.intersection(shortlist)) / len(gold),
                "first_source": sources.get(top[0]) if top else None,
                "first_chunk_id": top[0] if top else None,
                "ranks": evidence_ranks(row, sources, stages),
            }
        observed.append({
            "id": row["id"], "trial": row["trial"], "mode": row["mode"],
            "profile": row.get("profile", row["mode"]), "recorded_k": recorded_k,
            "recorded_metrics": row["metrics"],
            "recorded_first_source": row.get("first_source") or (sources.get(row["ranked_ids"][0]) if row["ranked_ids"] else None),
            "recorded_ranks": evidence_ranks(row, sources, recorded_stages),
            "comparisons": comparisons,
        })
    if not observed:
        raise ValueError("Report contains no hybrid retrieval requests")
    fused_rows = [row for row in observed if row["mode"] == "rrf"]
    overall = {
        str(k): {
            name: mean(row["comparisons"][str(k)]["metrics"][name] for row in fused_rows)
            for name in ("recall@1", "recall@3", "recall@5")
        } for k in configuration
    } if fused_rows else {}
    return {"metadata": metadata, "verified_fusion_rows": len(observed), "overall": overall, "requests": observed}
