#!/usr/bin/env python3
"""Read-only benchmark for BDH query rewrite routing.

Compares main's mechanical path with the v2 bridge using local oMLX/Qwen and,
when credentials are available, Ollama Cloud. Only pre_llm_call is exercised;
no post hook or learning request is sent.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import statistics
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLDENSET = ROOT / "benchmarks" / "query_rewrite_goldenset.json"
MAIN_PLUGIN = Path("/Users/albi/Projects/bdh-hermes-bridge/__init__.py")


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load plugin module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def percentile(values: list[float], p: float):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * p
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 3)


def anchor_recall(text: str, anchors: list[str]) -> float | None:
    if not anchors:
        return None
    folded = text.casefold()
    return round(sum(anchor.casefold() in folded for anchor in anchors) / len(anchors), 3)


def run_case(module, prefix: str, item: dict, rewrite_capture=None):
    message = item["message"]
    history = item.get("history")
    if not isinstance(history, list):
        history = [
            {"role": "user", "content": "Stiamo lavorando sull'integrazione BDH e sul bridge."},
            {"role": "assistant", "content": "Il sistema deve preservare il contesto senza inquinare il vault."},
            {"role": "tool", "content": "This tool payload must never reach the rewrite model."},
        ]
    started = time.perf_counter()
    result = None
    error = None
    try:
        result = module._on_pre_llm_call(
            session_id=f"benchmark-{prefix}-{item['id']}",
            user_message=message,
            conversation_history=history,
        )
    except Exception as exc:
        error = repr(exc)
    elapsed = time.perf_counter() - started
    captured = rewrite_capture.get(item["id"]) if rewrite_capture is not None else None
    return {
        "id": item["id"],
        "category": item["category"],
        "elapsed_seconds": round(elapsed, 3),
        "context_injected": bool(result and result.get("context")),
        "context_chars": len(result.get("context", "")) if result else 0,
        "context_text": result.get("context", "") if result else "",
        "rewrite": captured,
        "mechanical_retrieve": module._should_auto_retrieve(message),
        "error": error,
    }


def summarize(rows: list[dict], items_by_id: dict[str, dict], active: bool):
    valid = [row for row in rows if not row["error"]]
    times = [row["elapsed_seconds"] for row in valid]
    context_hits = sum(row["context_injected"] for row in valid)
    route_correct = 0
    route_total = 0
    anchor_values = []
    variant_ok = 0
    variant_total = 0
    rewrite_success = 0

    for row in valid:
        expected = items_by_id[row["id"]]["expected"]
        rewrite = row.get("rewrite")
        if active:
            if rewrite is not None:
                rewrite_success += 1
                actual_retrieve = rewrite.get("should_retrieve")
                actual_store = rewrite.get("store_candidate")
                text = " ".join([
                    rewrite.get("query", ""),
                    rewrite.get("search_query", ""),
                    " ".join(rewrite.get("sub_queries", [])),
                    row.get("context_text", ""),
                ])
                sub_count = len(rewrite.get("sub_queries", []))
            else:
                actual_retrieve = row["mechanical_retrieve"]
                actual_store = row["mechanical_retrieve"]
                text = items_by_id[row["id"]]["message"]
                sub_count = 0
            route_total += 1
            route_correct += int(
                actual_retrieve == expected["should_retrieve"]
                and actual_store == expected["store_candidate"]
            )
            variant_total += 1
            variant_ok += int(sub_count <= expected["max_sub_queries"])
        else:
            # main's legacy path couples retrieval and storage.
            actual = row["mechanical_retrieve"]
            route_total += 1
            route_correct += int(
                actual == expected["should_retrieve"]
                and actual == expected["store_candidate"]
            )
            text = items_by_id[row["id"]]["message"] + " " + row.get("context_text", "")

        recall = anchor_recall(text, expected["anchors"])
        if recall is not None:
            anchor_values.append(recall)

    by_category = {}
    for category in sorted({row["category"] for row in valid}):
        category_rows = [row for row in valid if row["category"] == category]
        by_category[category] = {
            "n": len(category_rows),
            "mean_seconds": round(statistics.mean(r["elapsed_seconds"] for r in category_rows), 3),
            "context_hits": sum(r["context_injected"] for r in category_rows),
        }

    summary = {
        "n": len(valid),
        "errors": len(rows) - len(valid),
        "mean_seconds": round(statistics.mean(times), 3) if times else None,
        "p50_seconds": percentile(times, 0.50),
        "p95_seconds": percentile(times, 0.95),
        "max_seconds": round(max(times), 3) if times else None,
        "context_hits": context_hits,
        "context_hit_rate": round(context_hits / len(valid), 3) if valid else None,
        "route_accuracy": round(route_correct / route_total, 3) if route_total else None,
        "anchor_recall_mean": round(statistics.mean(anchor_values), 3) if anchor_values else None,
        "variant_bound_accuracy": round(variant_ok / variant_total, 3) if variant_total else None,
        "by_category": by_category,
    }
    if active:
        summary["rewrite_successes"] = rewrite_success
        summary["rewrite_success_rate"] = round(rewrite_success / len(valid), 3) if valid else None
    return summary


def run_backend(backend: str, items: list[dict], rewrite_timeout: int):
    if backend == "baseline":
        module = load_module("benchmark_main", MAIN_PLUGIN)
        setattr(module, "_QUERY_REWRITE_ENABLED", False)
        active = False
        prefix = "baseline"
        endpoint = None
        model = None
    elif backend == "omlx":
        module = load_module("benchmark_v2_omlx", ROOT / "__init__.py")
        setattr(module, "_QUERY_REWRITE_ENABLED", True)
        setattr(module, "_REWRITE_API_URL", "http://127.0.0.1:8083/v1")
        setattr(module, "_REWRITE_MODEL", "qwen3.8-27b-oq4e-mtp")
        setattr(module, "_REWRITE_API_KEY", "local")
        setattr(module, "_REWRITE_TIMEOUT", rewrite_timeout)
        active = True
        prefix = "omlx-local"
        endpoint = module._REWRITE_API_URL
        model = module._REWRITE_MODEL
    else:
        module = load_module("benchmark_v2_cloud", ROOT / "__init__.py")
        setattr(module, "_QUERY_REWRITE_ENABLED", True)
        setattr(module, "_REWRITE_API_URL", "https://ollama.com/v1")
        setattr(module, "_REWRITE_MODEL", os.environ.get("BDH_REWRITE_MODEL", "deepseek-v4-flash:cloud"))
        setattr(module, "_REWRITE_API_KEY", os.environ.get("BDH_REWRITE_API_KEY", "") or os.environ.get("OLLAMA_API_KEY", ""))
        setattr(module, "_REWRITE_TIMEOUT", rewrite_timeout)
        if not module._REWRITE_API_KEY:
            raise SystemExit("OLLAMA_API_KEY is unavailable; cloud benchmark not run")
        active = True
        prefix = "ollama-cloud"
        endpoint = module._REWRITE_API_URL
        model = module._REWRITE_MODEL

    captured = {}
    current_id = [None]
    if active:
        original = module._rewrite_query

        def wrapped(message, context=""):
            started = time.perf_counter()
            result = original(message, context)
            if result is not None:
                result = dict(result)
                result["provider_elapsed_seconds"] = round(time.perf_counter() - started, 3)
            captured[current_id[0]] = result
            return result

        module._rewrite_query = wrapped

    rows = []
    for item in items:
        if active:
            current_id[0] = item["id"]
            row = run_case(module, prefix, item, captured)
        else:
            row = run_case(module, prefix, item)
        rows.append(row)

    items_by_id = {item["id"]: item for item in items}
    summary = summarize(rows, items_by_id, active)
    for row in rows:
        row.pop("context_text", None)
    return {
        "backend": backend,
        "endpoint": endpoint,
        "model": model,
        "goldenset_slice": {"start": items[0]["id"], "count": len(items)},
        "read_only": True,
        "summary": summary,
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("baseline", "omlx", "cloud"), required=True)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--rewrite-timeout", type=int, default=15)
    parser.add_argument("--goldenset", type=Path, default=GOLDENSET)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    dataset = json.loads(args.goldenset.read_text(encoding="utf-8"))
    all_items = dataset["items"]
    if args.start < 0 or args.count <= 0 or args.start >= len(all_items):
        raise SystemExit("invalid benchmark slice")
    items = all_items[args.start:args.start + args.count]
    report = run_backend(args.backend, items, args.rewrite_timeout)
    report["rewrite_timeout_seconds"] = args.rewrite_timeout
    report["goldenset"] = {
        "path": str(args.goldenset),
        "version": dataset.get("version"),
        "size": len(all_items),
        "slice_start_index": args.start,
        "slice_count": len(items),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "summary": report["summary"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
