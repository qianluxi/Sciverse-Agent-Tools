from __future__ import annotations

import csv
import io
import zipfile
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable

from research_app.csv_safe import safe_csv_row
from research_app.exports import author_names
from research_app.screening import normalize_title, strong_identifier_keys


MISSING_FIELDS = (
    ("year", "年份"),
    ("venue", "期刊/会议"),
    ("authors", "作者"),
    ("subjects", "学科"),
    ("keywords", "关键词"),
    ("citation_count", "引用量"),
)


def _year(paper: dict[str, Any]) -> int | None:
    value = paper.get("publication_published_year")
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1000 <= year <= 3000 else None


def _venue(paper: dict[str, Any]) -> str:
    return str(paper.get("publication_venue_name_unified") or "").strip()


def _subjects(paper: dict[str, Any]) -> list[str]:
    value = paper.get("subjects") or []
    if isinstance(value, str):
        value = [value]
    return [str(item).strip() for item in value if str(item).strip()]


def _keywords(paper: dict[str, Any]) -> list[str]:
    value = paper.get("keywords") or []
    if isinstance(value, str):
        value = [value]
    return [str(item).strip() for item in value if str(item).strip()]


def _citation_count(paper: dict[str, Any]) -> int | None:
    value = paper.get("citation_count")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _dedupe(papers: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    seen_strong: set[str] = set()
    title_has_strong: dict[str, bool] = {}
    unique: list[dict[str, Any]] = []
    duplicates = 0
    for paper in papers:
        strong = set(strong_identifier_keys(paper))
        title = normalize_title(paper.get("title"))
        title_key = f"title:{title}" if title else ""
        duplicate = bool(strong & seen_strong)
        if not duplicate and title_key and title_key in title_has_strong:
            duplicate = not strong or not title_has_strong[title_key]
        seen_strong.update(strong)
        if title_key:
            title_has_strong[title_key] = title_has_strong.get(title_key, False) or bool(strong)
        if duplicate:
            duplicates += 1
            continue
        unique.append(paper)
    return unique, duplicates


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == []


def _missing_row(
    papers: list[dict[str, Any]],
    field: str,
    label: str,
    getter: Callable[[dict[str, Any]], Any],
) -> dict[str, Any]:
    missing = sum(1 for paper in papers if _is_missing(getter(paper)))
    total = len(papers)
    return {
        "field": field,
        "label": label,
        "missing": missing,
        "total": total,
        "missing_rate": round(missing / total, 4) if total else 0.0,
    }


def analyze_papers(
    papers: list[dict[str, Any]],
    *,
    top_n: int = 10,
    recent_years: int = 5,
) -> dict[str, Any]:
    unique, duplicates_removed = _dedupe(papers)
    year_counter: Counter[int] = Counter()
    venue_counter: Counter[str] = Counter()
    author_counter: Counter[str] = Counter()
    subject_counter: Counter[str] = Counter()
    keyword_counter: Counter[str] = Counter()
    keyword_pair_counter: Counter[tuple[str, str]] = Counter()

    for paper in unique:
        year = _year(paper)
        if year is not None:
            year_counter[year] += 1
        venue = _venue(paper)
        if venue:
            venue_counter[venue] += 1
        for author in author_names(paper):
            author_counter[author] += 1
        for subject in _subjects(paper):
            subject_counter[subject] += 1
        keywords = sorted({keyword.casefold() for keyword in _keywords(paper)})
        for keyword in keywords:
            keyword_counter[keyword] += 1
        for left_index, left in enumerate(keywords):
            for right in keywords[left_index + 1 :]:
                keyword_pair_counter[(left, right)] += 1

    year_range = None
    if year_counter:
        year_range = [min(year_counter), max(year_counter)]

    growing = []
    if year_range:
        recent_cutoff = year_range[1] - recent_years + 1
        recent_counts: Counter[str] = Counter()
        earlier_counts: Counter[str] = Counter()
        for paper in unique:
            year = _year(paper)
            if year is None:
                continue
            for keyword in {keyword.casefold() for keyword in _keywords(paper)}:
                if year >= recent_cutoff:
                    recent_counts[keyword] += 1
                else:
                    earlier_counts[keyword] += 1
        for keyword, recent in recent_counts.items():
            earlier = earlier_counts.get(keyword, 0)
            growing.append(
                {
                    "keyword": keyword,
                    "recent": recent,
                    "earlier": earlier,
                    "delta": recent - earlier,
                }
            )
        growing.sort(
            key=lambda item: (item["delta"], item["recent"], item["keyword"]), reverse=True
        )
        growing = growing[:top_n]

    top_cited = sorted(
        (
            {
                "title": paper.get("title") or "（暂无标题）",
                "year": _year(paper),
                "venue": _venue(paper),
                "citation_count": _citation_count(paper) or 0,
                "doi": paper.get("doi") or "",
                "unique_id": paper.get("unique_id") or "",
            }
            for paper in unique
        ),
        key=lambda item: (item["citation_count"], item["title"].casefold()),
        reverse=True,
    )[:top_n]

    missing = [
        _missing_row(
            unique,
            field,
            label,
            {
                "year": _year,
                "venue": _venue,
                "authors": lambda paper: author_names(paper),
                "subjects": _subjects,
                "keywords": _keywords,
                "citation_count": _citation_count,
            }[field],
        )
        for field, label in MISSING_FIELDS
    ]

    return {
        "sample_size": len(unique),
        "duplicates_removed": duplicates_removed,
        "year_range": year_range,
        "year_counts": [
            {"year": year, "count": count} for year, count in sorted(year_counter.items())
        ],
        "venue_counts": [
            {"venue": venue, "count": count}
            for venue, count in sorted(venue_counter.items(), key=lambda item: (-item[1], item[0]))
        ],
        "author_counts": [
            {"author": author, "count": count}
            for author, count in sorted(
                author_counter.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "subject_counts": [
            {"subject": subject, "count": count}
            for subject, count in sorted(
                subject_counter.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "keyword_counts": [
            {"keyword": keyword, "count": count}
            for keyword, count in sorted(
                keyword_counter.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "keyword_pairs": [
            {"left": left, "right": right, "count": count}
            for (left, right), count in sorted(
                keyword_pair_counter.items(), key=lambda item: (-item[1], item[0])
            )[:50]
        ],
        "top_cited": top_cited,
        "growing_keywords": growing,
        "missing": missing,
    }


def generated_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_csv(rows: list[dict[str, Any]], columns: list[str]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow(safe_csv_row(row))
    return output.getvalue().encode("utf-8-sig")


def export_trends_zip(analysis: dict[str, Any], scope: dict[str, Any] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "years.csv",
            _write_csv(analysis["year_counts"], ["year", "count"]),
        )
        archive.writestr(
            "venues.csv",
            _write_csv(analysis["venue_counts"], ["venue", "count"]),
        )
        archive.writestr(
            "authors.csv",
            _write_csv(analysis["author_counts"], ["author", "count"]),
        )
        archive.writestr(
            "subjects.csv",
            _write_csv(analysis["subject_counts"], ["subject", "count"]),
        )
        archive.writestr(
            "keywords.csv",
            _write_csv(analysis["keyword_counts"], ["keyword", "count"]),
        )
        archive.writestr(
            "keyword_pairs.csv",
            _write_csv(analysis["keyword_pairs"], ["left", "right", "count"]),
        )
        archive.writestr(
            "top_cited.csv",
            _write_csv(
                analysis["top_cited"],
                ["title", "year", "venue", "citation_count", "doi", "unique_id"],
            ),
        )
        archive.writestr(
            "missing.csv",
            _write_csv(
                analysis["missing"],
                ["field", "label", "missing", "total", "missing_rate"],
            ),
        )
        summary = analysis.get("summary", {})
        if scope:
            summary = {**scope, **summary}
        archive.writestr(
            "summary.csv",
            _write_csv([summary], list(summary)),
        )
    return buffer.getvalue()


def export_trends_png(analysis: dict[str, Any]) -> bytes:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    charts = (
        ("Publications by year", analysis["year_counts"], "year", "count"),
        ("Top venues", analysis["venue_counts"][:10], "venue", "count"),
        ("Top authors", analysis["author_counts"][:10], "author", "count"),
        ("Top keywords", analysis["keyword_counts"][:10], "keyword", "count"),
    )
    for axis, (title, rows, label_key, value_key) in zip(axes.flat, charts):
        labels = [str(row[label_key]) for row in rows][::-1]
        values = [row[value_key] for row in rows][::-1]
        axis.barh(labels, values, color="#236b56")
        axis.set_title(title)
        axis.tick_params(axis="y", labelsize=8)
    fig.tight_layout()
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=150)
    plt.close(fig)
    return buffer.getvalue()
