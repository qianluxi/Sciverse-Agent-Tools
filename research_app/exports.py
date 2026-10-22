from __future__ import annotations

import csv
import io
from typing import Any


def format_authors(paper: dict[str, Any]) -> str:
    authors = paper.get("author") or paper.get("authors") or []
    if isinstance(authors, str):
        return authors
    names = [
        author.get("name", "") if isinstance(author, dict) else str(author) for author in authors
    ]
    return ", ".join(name for name in names if name)


def export_results_csv(papers: list[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    columns = ["title", "authors", "year", "journal", "doi", "abstract", "unique_id", "doc_id"]
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    for paper in papers:
        writer.writerow(
            {
                "title": paper.get("title", ""),
                "authors": format_authors(paper),
                "year": paper.get("publication_published_year", ""),
                "journal": paper.get("publication_venue_name_unified", ""),
                "doi": paper.get("doi", ""),
                "abstract": paper.get("abstract") or paper.get("chunk", ""),
                "unique_id": paper.get("unique_id", ""),
                "doc_id": paper.get("doc_id", ""),
            }
        )
    return output.getvalue().encode("utf-8-sig")
