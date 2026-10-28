import pytest

from research_app.screening_eval import evaluate_screening_suggestions, review_summary


def _suggestion(key, suggestion, confidence="high", error=None):
    return {
        "candidate_key": key,
        "suggestion": suggestion,
        "confidence": confidence,
        "error": error,
    }


def test_evaluate_screening_suggestions_reports_denominators_and_confidence_bins():
    gold = {
        "paper:a": "included",
        "paper:b": "included",
        "paper:c": "included",
        "paper:d": "excluded",
        "paper:e": "excluded",
        "paper:f": "excluded",
        "paper:g": "pending",
    }
    suggestions = [
        _suggestion("paper:a", "include", "high"),
        _suggestion("paper:b", "include", "medium"),
        _suggestion("paper:c", "exclude", "high"),
        _suggestion("paper:d", "exclude", "high"),
        _suggestion("paper:e", "exclude", "low"),
        _suggestion("paper:f", "uncertain", "low"),
    ]

    result = evaluate_screening_suggestions(suggestions, gold)

    assert result["total"] == 6
    assert result["counts"] == {
        "gold_included": 3,
        "gold_excluded": 3,
        "ai_include": 2,
        "ai_exclude": 3,
        "ai_uncertain": 1,
        "true_include": 2,
        "false_exclusion": 1,
        "excluded_correct": 2,
        "missing_suggestions": 0,
        "ai_errors": 0,
    }
    assert result["metrics"]["include_recall"] == pytest.approx(2 / 3)
    assert result["metrics"]["false_exclusion_rate"] == pytest.approx(1 / 3)
    assert result["metrics"]["missed_inclusion_rate"] == pytest.approx(1 / 3)
    assert result["metrics"]["non_exclusion_recall"] == pytest.approx(2 / 3)
    assert result["metrics"]["uncertain_rate"] == pytest.approx(1 / 6)

    high = next(row for row in result["confidence"] if row["confidence"] == "high")
    assert high["total"] == 3
    assert high["false_exclusion_rate"] == pytest.approx(1 / 2)


def test_evaluate_screening_suggestions_handles_missing_errors_and_empty_denominators():
    gold = {"paper:a": "included", "paper:b": "excluded"}
    suggestions = [
        _suggestion("paper:a", "include", "high", error="invalid response"),
    ]

    result = evaluate_screening_suggestions(suggestions, gold)

    assert result["counts"]["missing_suggestions"] == 1
    assert result["counts"]["ai_errors"] == 1
    assert result["counts"]["ai_uncertain"] == 2
    assert result["metrics"]["include_recall"] == pytest.approx(0)
    assert result["metrics"]["false_exclusion_rate"] is None
    assert result["metrics"]["exclusion_precision"] is None


def test_review_summary_uses_reviewed_items_as_denominator():
    summary = review_summary(
        [
            {"review_status": "accepted"},
            {"review_status": "accepted"},
            {"review_status": "modified"},
            {"review_status": "rejected"},
            {"review_status": "pending"},
        ]
    )

    assert summary["counts"] == {
        "pending": 1,
        "accepted": 2,
        "modified": 1,
        "rejected": 1,
    }
    assert summary["reviewed"] == 4
    assert summary["accepted_rate"] == pytest.approx(0.5)
    assert summary["modified_rate"] == pytest.approx(0.25)
    assert summary["rejected_rate"] == pytest.approx(0.25)
