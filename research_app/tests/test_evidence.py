import io
import zipfile

from research_app.evidence import (
    export_evidence_manifest_csv,
    export_evidence_zip,
    extract_figure_refs,
    has_fulltext,
    is_readable,
)


def test_extract_figure_refs_dedupes_limits_and_skips_remote_urls():
    markdown = """
    ![Figure 1](dt=x/p1/f1.png)
    ![Figure 1 duplicate](dt=x/p1/f1.png)
    ![Remote](https://example.com/f2.png)
    ![Figure 3](dt=x/p1/f3.png)
    """

    refs = extract_figure_refs(markdown, limit=2)

    assert refs == [
        {"alt": "Figure 1", "file_name": "dt=x/p1/f1.png"},
        {"alt": "Figure 3", "file_name": "dt=x/p1/f3.png"},
    ]


def test_evidence_zip_contains_images_and_manifest_with_notes():
    items = [
        {
            "paper_id": "paper:1",
            "title": "Paper",
            "year": 2024,
            "doi": "10.1/x",
            "alt": "Figure 1",
            "file_name": "dt=x/p1/f1.png",
            "mime": "image/png",
            "note": "Evidence note",
            "image": b"\x89PNG\r\n\x1a\n",
        }
    ]

    data = export_evidence_zip(items)

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        manifest = archive.read("manifest.csv").decode("utf-8-sig")

    assert "figure_001.png" in names
    assert "manifest.csv" in names
    assert "Evidence note" in manifest
    assert "figure_001.png" in manifest


def test_manifest_csv_has_stable_columns():
    data = export_evidence_manifest_csv(
        [
            {
                "paper_id": "paper:1",
                "title": "Paper",
                "file_name": "f.png",
                "note": "note",
            }
        ]
    ).decode("utf-8-sig")

    assert data.splitlines()[0] == ("paper_id,title,year,doi,alt,file_name,mime,note,archive_name")


def test_is_readable_requires_doc_id_and_permission():
    assert is_readable({"doc_id": "d", "is_content_accessible": True})
    assert not is_readable({"doc_id": "d", "is_content_accessible": False})
    assert not is_readable({"doc_id": "d"})
    assert not is_readable({"is_content_accessible": True})


def test_has_fulltext_only_requires_doc_id():
    assert has_fulltext({"doc_id": "d", "is_content_accessible": False})
    assert not has_fulltext({"is_content_accessible": True})
