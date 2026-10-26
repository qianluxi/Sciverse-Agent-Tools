from __future__ import annotations

import csv
import io
import re
from typing import Any

from research_app.csv_safe import safe_csv_row
from research_app.screening import STATUS_LABELS


def author_names(paper: dict[str, Any]) -> list[str]:
    authors = paper.get("author") or paper.get("authors") or []
    if isinstance(authors, str):
        return [authors] if authors else []
    names = [
        author.get("name", "") if isinstance(author, dict) else str(author) for author in authors
    ]
    return [name for name in names if name]


def format_authors(paper: dict[str, Any]) -> str:
    return ", ".join(author_names(paper))


def export_results_csv(papers: list[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    columns = ["title", "authors", "year", "journal", "doi", "abstract", "unique_id", "doc_id"]
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    for paper in papers:
        writer.writerow(
            safe_csv_row(
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
        )
    return output.getvalue().encode("utf-8-sig")


def export_ris(papers: list[dict[str, Any]]) -> bytes:
    records = []
    for paper in papers:
        venue_type = str(paper.get("publication_venue_type", "")).casefold()
        record_type = "CONF" if "conference" in venue_type else "JOUR"
        fields = [f"TY  - {record_type}"]
        for tag, value in (
            ("TI", paper.get("title")),
            ("PY", paper.get("publication_published_year")),
            ("JO", paper.get("publication_venue_name_unified")),
            ("DO", paper.get("doi")),
            ("AB", paper.get("abstract") or paper.get("chunk")),
        ):
            if value not in (None, ""):
                text = str(value).replace("\r", " ").replace("\n", " ")
                fields.append(f"{tag}  - {text}")
        fields.extend(f"AU  - {name}" for name in author_names(paper))
        if paper.get("doi"):
            fields.append(f"UR  - https://doi.org/{paper['doi']}")
        if paper.get("unique_id"):
            fields.append(f"ID  - {paper['unique_id']}")
        fields.append("ER  - ")
        records.append("\n".join(fields))
    return ("\n\n".join(records) + ("\n" if records else "")).encode("utf-8")


def _bibtex_escape(value: Any) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "{": r"\{",
        "}": r"\}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
    }
    text = "".join(replacements.get(char, char) for char in str(value))
    return " ".join(text.split())


def export_bibtex(papers: list[dict[str, Any]]) -> bytes:
    entries = []
    used_keys: set[str] = set()
    for index, paper in enumerate(papers, start=1):
        doi = str(paper.get("doi") or "")
        if doi:
            source = doi
        else:
            author = author_names(paper)[:1]
            source = "_".join(author + [str(paper.get("publication_published_year") or "paper")])
            source += "_" + str(paper.get("title") or "")
        base_key = re.sub(r"[^A-Za-z0-9]+", "_", source).strip("_") or f"paper_{index}"
        key = base_key
        suffix = 2
        while key.casefold() in used_keys:
            key = f"{base_key}_{suffix}"
            suffix += 1
        used_keys.add(key.casefold())

        venue = paper.get("publication_venue_name_unified")
        venue_type = str(paper.get("publication_venue_type", "")).casefold()
        entry_type = "inproceedings" if "conference" in venue_type else "article"
        if not venue:
            entry_type = "misc"
        fields: list[tuple[str, Any]] = [
            ("title", paper.get("title")),
            ("author", " and ".join(author_names(paper)) or None),
            ("year", paper.get("publication_published_year")),
            ("booktitle" if entry_type == "inproceedings" else "journal", venue),
            ("doi", doi or None),
            ("abstract", paper.get("abstract") or paper.get("chunk")),
        ]
        lines = [f"@{entry_type}{{{key},"]
        lines.extend(
            f"  {name} = {{{_bibtex_escape(value)}}},"
            for name, value in fields
            if value not in (None, "")
        )
        if len(lines) > 1:
            lines[-1] = lines[-1].rstrip(",")
        lines.append("}")
        entries.append("\n".join(lines))
    return ("\n\n".join(entries) + ("\n" if entries else "")).encode("utf-8")


def export_markdown(papers: list[dict[str, Any]]) -> bytes:
    sections = []
    for paper in papers:
        title = paper.get("title") or "（暂无标题）"
        metadata = " · ".join(
            str(value)
            for value in (
                format_authors(paper),
                paper.get("publication_published_year"),
                paper.get("publication_venue_name_unified"),
            )
            if value
        )
        lines = [f"## {title}", "", metadata]
        doi = paper.get("doi")
        if doi:
            lines.extend(["", f"DOI: https://doi.org/{doi}"])
        abstract = paper.get("abstract") or paper.get("chunk")
        if abstract:
            lines.extend(["", str(abstract)])
        sections.append("\n".join(lines))
    return ("\n\n---\n\n".join(sections) + ("\n" if sections else "")).encode("utf-8")


def export_screening_csv(candidates: list[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    columns = [
        "candidate_key",
        "paper_id",
        "title",
        "year",
        "doi",
        "status",
        "status_label",
        "exclusion_reason",
        "notes",
        "updated_at",
    ]
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    for candidate in candidates:
        writer.writerow(
            safe_csv_row(
                {
                    "candidate_key": candidate.get("candidate_key", ""),
                    "paper_id": candidate.get("paper_id", ""),
                    "title": candidate.get("title", ""),
                    "year": candidate.get("year", ""),
                    "doi": candidate.get("doi", ""),
                    "status": candidate.get("status", ""),
                    "status_label": STATUS_LABELS.get(candidate.get("status", ""), ""),
                    "exclusion_reason": candidate.get("exclusion_reason") or "",
                    "notes": candidate.get("notes", ""),
                    "updated_at": candidate.get("updated_at", ""),
                }
            )
        )
    return output.getvalue().encode("utf-8-sig")
