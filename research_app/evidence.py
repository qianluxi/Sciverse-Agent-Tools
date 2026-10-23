from __future__ import annotations

import csv
import io
import re
import zipfile
from typing import Any


IMAGE_REF_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
MAX_IMAGES_PER_PAPER = 4
MAX_PAPERS_PER_RUN = 5


def is_readable(paper: dict[str, Any]) -> bool:
    return bool(paper.get("doc_id")) and paper.get("is_content_accessible") is True


def extract_figure_refs(
    markdown: str, *, limit: int = MAX_IMAGES_PER_PAPER
) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in IMAGE_REF_RE.finditer(markdown or ""):
        alt = match.group(1).strip()
        file_name = match.group(2).strip()
        if not file_name or file_name.startswith(("http://", "https://")):
            continue
        if file_name in seen:
            continue
        seen.add(file_name)
        refs.append({"alt": alt or file_name, "file_name": file_name})
        if len(refs) >= max(1, limit):
            break
    return refs


def _safe_image_name(index: int, file_name: str) -> str:
    base = file_name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    extension = ".png"
    if "." in base:
        suffix = base.rsplit(".", 1)[-1].lower()
        if suffix in {"png", "jpg", "jpeg", "gif", "webp", "svg"}:
            extension = f".{suffix}"
    return f"figure_{index:03d}{extension}"


def export_evidence_manifest_csv(items: list[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    columns = [
        "paper_id",
        "title",
        "year",
        "doi",
        "alt",
        "file_name",
        "mime",
        "note",
        "archive_name",
    ]
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    for item in items:
        writer.writerow({column: item.get(column, "") for column in columns})
    return output.getvalue().encode("utf-8-sig")


def export_evidence_zip(items: list[dict[str, Any]]) -> bytes:
    buffer = io.BytesIO()
    manifest_rows = []
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, item in enumerate(items, start=1):
            archive_name = _safe_image_name(index, item.get("file_name", ""))
            archive.writestr(archive_name, item.get("image") or b"")
            manifest_rows.append({**item, "archive_name": archive_name})
        archive.writestr(
            "manifest.csv",
            export_evidence_manifest_csv(manifest_rows),
        )
    return buffer.getvalue()
