from __future__ import annotations

from typing import Any


GOLD_STATUSES = {"included", "excluded"}


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def evaluate_screening_suggestions(
    suggestions: list[dict[str, Any]],
    gold_by_key: dict[str, str],
) -> dict[str, Any]:
    suggestion_by_key = {
        str(item.get("candidate_key") or ""): item for item in suggestions
    }
    gold_keys = [
        key for key, status in gold_by_key.items() if status in GOLD_STATUSES
    ]
    counts = {
        "gold_included": 0,
        "gold_excluded": 0,
        "ai_include": 0,
        "ai_exclude": 0,
        "ai_uncertain": 0,
        "true_include": 0,
        "false_exclusion": 0,
        "excluded_correct": 0,
        "missing_suggestions": 0,
        "ai_errors": 0,
    }
    confidence_counts: dict[str, dict[str, int]] = {}

    for key in gold_keys:
        gold = gold_by_key[key]
        item = suggestion_by_key.get(key)
        if gold == "included":
            counts["gold_included"] += 1
        else:
            counts["gold_excluded"] += 1
        if item is None:
            ai = "uncertain"
            confidence = "unknown"
            counts["missing_suggestions"] += 1
        else:
            if item.get("error"):
                counts["ai_errors"] += 1
                ai = "uncertain"
            else:
                ai = str(item.get("suggestion") or "uncertain")
                if ai not in {"include", "exclude", "uncertain"}:
                    ai = "uncertain"
            confidence = str(item.get("confidence") or "unknown").casefold()
            if confidence not in {"high", "medium", "low"}:
                confidence = "unknown"

        counts[f"ai_{ai}"] += 1
        if gold == "included" and ai == "include":
            counts["true_include"] += 1
        if gold == "included" and ai == "exclude":
            counts["false_exclusion"] += 1
        if gold == "excluded" and ai == "exclude":
            counts["excluded_correct"] += 1

        bucket = confidence_counts.setdefault(
            confidence,
            {
                "total": 0,
                "gold_included": 0,
                "true_include": 0,
                "false_exclusion": 0,
                "ai_exclude": 0,
                "ai_uncertain": 0,
            },
        )
        bucket["total"] += 1
        if gold == "included":
            bucket["gold_included"] += 1
        if ai == "exclude":
            bucket["ai_exclude"] += 1
        if ai == "uncertain":
            bucket["ai_uncertain"] += 1
        if gold == "included" and ai == "include":
            bucket["true_include"] += 1
        if gold == "included" and ai == "exclude":
            bucket["false_exclusion"] += 1

    total = len(gold_keys)
    metrics = {
        "include_precision": _rate(counts["true_include"], counts["ai_include"]),
        "include_recall": _rate(counts["true_include"], counts["gold_included"]),
        "exclusion_precision": _rate(
            counts["excluded_correct"], counts["ai_exclude"]
        ),
        "false_exclusion_rate": _rate(
            counts["false_exclusion"], counts["ai_exclude"]
        ),
        "missed_inclusion_rate": _rate(
            counts["false_exclusion"], counts["gold_included"]
        ),
        "non_exclusion_recall": _rate(
            counts["gold_included"] - counts["false_exclusion"],
            counts["gold_included"],
        ),
        "uncertain_rate": _rate(counts["ai_uncertain"], total),
    }
    confidence_rows = []
    for label in ("high", "medium", "low", "unknown"):
        bucket = confidence_counts.get(label)
        if not bucket:
            continue
        confidence_rows.append(
            {
                "confidence": label,
                **bucket,
                "include_recall": _rate(
                    bucket["true_include"], bucket["gold_included"]
                ),
                "false_exclusion_rate": _rate(
                    bucket["false_exclusion"], bucket["ai_exclude"]
                ),
            }
        )
    return {
        "total": total,
        "counts": counts,
        "metrics": metrics,
        "confidence": confidence_rows,
    }


def review_summary(suggestions: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {
        "pending": 0,
        "accepted": 0,
        "modified": 0,
        "rejected": 0,
    }
    for item in suggestions:
        status = str(item.get("review_status") or "pending")
        if status not in counts:
            status = "pending"
        counts[status] += 1
    reviewed = counts["accepted"] + counts["modified"] + counts["rejected"]
    return {
        "counts": counts,
        "reviewed": reviewed,
        "accepted_rate": _rate(counts["accepted"], reviewed),
        "modified_rate": _rate(counts["modified"], reviewed),
        "rejected_rate": _rate(counts["rejected"], reviewed),
    }
