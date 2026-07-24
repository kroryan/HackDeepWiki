import json
import zipfile
from pathlib import Path

import pytest

# fanwiki_import pulls in mwparserfromhell at module load. That dep is only
# present in the full fanwiki-capable install (and in the hackdeepwiki:ollama
# image), not in a minimal .venv used for the rest of the suite. Without this
# guard, `pytest` (which discovers both `test/` and `tests/` per pytest.ini)
# aborts collection entirely on a minimal install -- one missing optional dep
# takes down the whole run. importorskip turns that into a clean skip instead.
pytest.importorskip("mwparserfromhell")

from api import fanwiki_library
from api.fanwiki_import import attach_images, import_dump, inspect_dump


SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/" version="0.11">
  <siteinfo>
    <sitename>Test Wiki</sitename>
    <dbname>testwiki</dbname>
    <base>https://example.test/wiki/Main_Page</base>
    <namespaces>
      <namespace key="0" case="first-letter" />
      <namespace key="14" case="first-letter">Category</namespace>
    </namespaces>
  </siteinfo>
  <page>
    <title>Alpha Page</title><ns>0</ns>
    <revision><text xml:space="preserve">Hello [[Beta Page]].</text></revision>
  </page>
  <page>
    <title>Beta Page</title><ns>0</ns>
    <revision><text xml:space="preserve">World [[Category:Tests]].</text></revision>
  </page>
</mediawiki>
"""


def test_import_is_durable_and_uses_valid_article_urls(tmp_path, monkeypatch):
    xml_path = tmp_path / "wiki.xml"
    xml_path.write_text(SAMPLE_XML, encoding="utf-8")
    import_dir = tmp_path / "repos" / "website_example.test"
    monkeypatch.setattr(
        "api.fanwiki_import.website_local_dir",
        lambda _start_url: str(import_dir),
    )

    info = inspect_dump(str(xml_path))
    result = import_dump(str(xml_path), info, {0, 14}, fresh=True)

    assert result["page_count"] == 2
    assert result["links_resolved"] == 1
    meta = json.loads((import_dir / "_site_meta.json").read_text(encoding="utf-8"))
    assert meta["source_type"] == "fanwiki"
    assert meta["wiki_name"] == "Test Wiki"
    assert meta["start_url"] == "https://example.test/wiki/Main_Page"
    assert meta["pages"][0]["url"] == "https://example.test/wiki/Alpha_Page"
    assert "/Main_Page/wiki/" not in meta["pages"][0]["url"]


def test_library_discovers_legacy_import_and_deletes_only_verified_source(
    tmp_path, monkeypatch
):
    repos_dir = tmp_path / "repos"
    imported_dir = repos_dir / "website_legacy.test"
    imported_dir.mkdir(parents=True)
    (imported_dir / "page.md").write_text("content", encoding="utf-8")
    (imported_dir / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (imported_dir / "_site_meta.json").write_text(
        json.dumps(
            {
                "start_url": "https://legacy.test/wiki/Main_Page",
                "crawled_at": "2026-01-02T03:04:05+00:00",
                "page_count": 1,
                # categories is the compatibility marker used by the original
                # importer before source_type was added.
                "pages": [{"relpath": "page.md", "title": "Page", "categories": []}],
            }
        ),
        encoding="utf-8",
    )
    website_dir = repos_dir / "website_normal.test"
    website_dir.mkdir()
    (website_dir / "_site_meta.json").write_text(
        json.dumps(
            {
                "start_url": "https://normal.test",
                "page_count": 1,
                "pages": [{"relpath": "index.md", "title": "Home"}],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(fanwiki_library, "get_data_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        fanwiki_library,
        "website_local_dir",
        lambda start_url: str(
            imported_dir if "legacy.test" in start_url else website_dir
        ),
    )

    entries = fanwiki_library.list_all()
    assert len(entries) == 1
    assert entries[0]["repo"] == "legacy.test"
    assert entries[0]["status"] == "imported"
    assert entries[0]["page_count"] == 1
    entry_id = entries[0]["id"]

    metadata = fanwiki_library.get(entry_id)
    assert metadata is not None
    assert metadata["main_page_path"] == "page.md"
    assert fanwiki_library.get_by_start_url(
        "https://legacy.test/wiki/Main_Page"
    )["id"] == entry_id
    assert fanwiki_library.page_index(entry_id)["entries"][0]["title"] == "Page"
    assert fanwiki_library.search(entry_id, "pag")[0]["path"] == "page.md"
    assert fanwiki_library.read_page(entry_id, "page.md")["content"] == "content"
    assert fanwiki_library.resolve_asset(entry_id, "logo.png") == str(
        imported_dir / "logo.png"
    )
    with pytest.raises(FileNotFoundError):
        fanwiki_library.read_page(entry_id, "../outside.md")
    with pytest.raises(FileNotFoundError):
        fanwiki_library.resolve_asset(entry_id, "../outside.png")

    assert fanwiki_library.delete("https://normal.test") is False
    assert website_dir.is_dir()
    assert fanwiki_library.delete("https://legacy.test/wiki/Main_Page") is True
    assert not imported_dir.exists()


def test_reader_markdown_removes_mediawiki_layout_but_keeps_content():
    source = """<!-- editor note -->
{| style="width: 100%"
| style="padding: 1em" | Welcome to **EVE**
|-
= Navigation =
| [https://example.test External page]
|}
![Logo](../_images/logo.png)
<inputbox>
type=search2
</inputbox>
"""
    rendered = fanwiki_library._reader_markdown(source)
    assert "editor note" not in rendered
    assert "style=" not in rendered
    assert "inputbox" not in rendered
    assert "Welcome to **EVE**" in rendered
    assert "# Navigation" in rendered
    assert "[External page](https://example.test)" in rendered
    assert "![Logo](../_images/logo.png)" in rendered


def test_import_and_later_attach_images_use_the_shared_images_folder(
    tmp_path, monkeypatch
):
    xml_path = tmp_path / "wiki.xml"
    xml_path.write_text(
        SAMPLE_XML.replace(
            "Hello [[Beta Page]].",
            "Hello [[Beta Page]]. [[File:Ship Logo.png|The ship logo]]",
        ),
        encoding="utf-8",
    )
    import_dir = tmp_path / "repos" / "website_example.test"
    monkeypatch.setattr(
        "api.fanwiki_import.website_local_dir",
        lambda _start_url: str(import_dir),
    )

    info = inspect_dump(str(xml_path))
    import_dump(str(xml_path), info, {0, 14}, fresh=True)
    alpha_path = import_dir / "wiki" / "Alpha_Page.md"
    assert "imagen no disponible: Ship Logo.png" in alpha_path.read_text(encoding="utf-8")

    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "Ship_Logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    result = attach_images(str(import_dir), str(images_dir))

    content = alpha_path.read_text(encoding="utf-8")
    assert result.images_attached == 1
    assert "![The ship logo](../_images/Ship_Logo.png)" in content
    assert (import_dir / "_images" / "Ship_Logo.png").is_file()

    imported_with_images = import_dump(
        str(xml_path),
        info,
        {0, 14},
        fresh=True,
        images_dir=str(images_dir),
    )
    assert imported_with_images["image_count"] == 1
    assert "![The ship logo](../_images/Ship_Logo.png)" in alpha_path.read_text(
        encoding="utf-8"
    )


def test_imported_wiki_exports_include_pages_links_and_images(tmp_path, monkeypatch):
    import_dir = tmp_path / "repos" / "website_example.test"
    (import_dir / "wiki").mkdir(parents=True)
    (import_dir / "_images").mkdir()
    (import_dir / "wiki" / "Alpha.md").write_text(
        "---\n"
        'url: "https://example.test/wiki/Alpha"\n'
        'title: "Alpha"\n'
        "likely_user_content: false\n"
        "depth: 0\n"
        "---\n\n"
        "# Alpha\n\n[Beta](Beta.md)\n\n![Logo](../_images/logo.png)\n",
        encoding="utf-8",
    )
    (import_dir / "wiki" / "Beta.md").write_text("# Beta\n", encoding="utf-8")
    (import_dir / "_images" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    meta = {
        "source_type": "fanwiki",
        "start_url": "https://example.test/wiki/Main_Page",
        "wiki_name": "Test Wiki",
        "page_count": 2,
        "pages": [
            {
                "relpath": "wiki/Alpha.md",
                "title": "Alpha",
                "url": "https://example.test/wiki/Alpha",
                "categories": ["Ships"],
            },
            {
                "relpath": "wiki/Beta.md",
                "title": "Beta",
                "url": "https://example.test/wiki/Beta",
                "categories": [],
            },
        ],
    }
    monkeypatch.setattr(fanwiki_library, "_find", lambda _entry_id: (str(import_dir), meta))

    obsidian_path = tmp_path / "wiki.zip"
    obsidian_result = fanwiki_library.export_obsidian("source", str(obsidian_path))
    assert obsidian_result == {
        "format": "obsidian",
        "page_count": 2,
        "asset_count": 1,
        "title": "Test Wiki",
    }
    with zipfile.ZipFile(obsidian_path) as archive:
        names = set(archive.namelist())
        assert "Test Wiki/Home.md" in names
        assert "Test Wiki/wiki/Alpha.md" in names
        assert "Test Wiki/wiki/Beta.md" in names
        assert "Test Wiki/_images/logo.png" in names
        assert "[Beta](Beta.md)" in archive.read("Test Wiki/wiki/Alpha.md").decode()

    reader_path = tmp_path / "wiki.hdwreader"
    reader_result = fanwiki_library.export_hdwreader("source", str(reader_path))
    assert reader_result["page_count"] == 2
    assert reader_result["asset_count"] == 1
    with zipfile.ZipFile(reader_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format_version"] == 1
        assert manifest["repo_type"] == "fanwiki"
        assert manifest["source_type"] == "mediawiki_xml"
        assert len(manifest["pages"]) == 2
        assert manifest["assets"] == ["assets/logo.png"]
        alpha = next(page for page in manifest["pages"] if page["title"] == "Alpha")
        beta = next(page for page in manifest["pages"] if page["title"] == "Beta")
        content = archive.read(alpha["file"]).decode()
        assert f"[Beta]({beta['id']}.md)" in content
        assert "![Logo](../assets/logo.png)" in content
        assert alpha["relatedPages"] == [beta["id"]]
