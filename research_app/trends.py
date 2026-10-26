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


def _completeness(paper: dict[str, Any]) -> tuple[int, int, int, str]:
    fields = (
        "title",
        "publication_published_year",
        "publication_venue_name_unified",
        "author",
        "subjects",
        "keywords",
        "citation_count",
        "doi",
        "unique_id",
        "doc_id",
        "abstract",
        "chunk",
    )
    filled = sum(1 for field in fields if not _is_missing(paper.get(field)))
    title = str(paper.get("title") or "")
    tie_breaker = "|".join(
        str(paper.get(key) or "")
        for key in ("unique_id", "doc_id", "doi", "title")
    )
    return (len(strong_identifier_keys(paper)), filled, len(title), tie_breaker)


def _merge_group(records: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(records, key=_completeness, reverse=True)
    merged = dict(ordered[0])
    for record in ordered[1:]:
        for field, value in record.items():
            if _is_missing(merged.get(field)) and not _is_missing(value):
                merged[field] = value
    return merged


def _dedupe(papers: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    parent = list(range(len(papers)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    strong_first: dict[str, int] = {}
    for index, paper in enumerate(papers):
        for identifier in strong_identifier_keys(paper):
            if identifier in strong_first:
                union(index, strong_first[identifier])
            else:
                strong_first[identifier] = index

    title_groups: dict[str, list[int]] = {}
    for index, paper in enumerate(papers):
        title = normalize_title(paper.get("title"))
        if title:
            title_groups.setdefault(title, []).append(index)

    for indices in title_groups.values():
        roots = {find(index) for index in indices}
        strong_roots = {
            root
            for root in roots
            if any(
                find(index) == root and strong_identifier_keys(papers[index]) for index in indices
            )
        }
        weak_roots = roots - strong_roots
        if len(strong_roots) == 1:
            anchor = next(iter(strong_roots))
            for root in weak_roots:
                union(root, anchor)
        elif not strong_roots and roots:
            anchor = next(iter(roots))
            for root in roots:
                union(root, anchor)
        elif weak_roots:
            anchor = next(iter(weak_roots))
            for root in weak_roots:
                union(root, anchor)

    grouped: dict[int, list[dict[str, Any]]] = {}
    first_seen: dict[int, int] = {}
    for index, paper in enumerate(papers):
        root = find(index)
        grouped.setdefault(root, []).append(paper)
        first_seen.setdefault(root, index)
    unique = [
        _merge_group(grouped[root])
        for root in sorted(grouped, key=lambda root: first_seen[root])
    ]
    return unique, len(papers) - len(unique)


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
