"""Check current documentation navigation and local assets without network access."""

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")


def document_links(path: Path) -> list[str]:
    """Ignore examples in fenced code blocks; include both page and image links."""
    text = re.sub(r"^```[^\n]*\n.*?^```\s*$", "", path.read_text(), flags=re.M | re.S)
    return LINK.findall(text)


def broken_links(root: Path, documents: list[Path]) -> list[str]:
    failures = []
    for document in documents:
        for target in document_links(document):
            url = urlsplit(target)
            if url.scheme or url.netloc or not url.path or url.path.startswith("/"):
                continue
            resolved = (document.parent / unquote(url.path)).resolve()
            if resolved.is_relative_to(root.resolve()) and not resolved.is_file():
                failures.append(f"{document.relative_to(root)}: {target}")
    return failures


def test_docs_links():
    documents = sorted([*ROOT.glob("*.md"), *ROOT.glob("docs/**/*.md"),
                        *ROOT.glob("frontend/*.md")])
    assert broken_links(ROOT, documents) == []
    index = ROOT / "docs/README.md"
    listed = {unquote(urlsplit(link).path) for link in document_links(index)}
    actual = {path.name for path in (ROOT / "docs").glob("*.md") if path != index}
    assert actual <= listed, f"Missing index entries: {actual - listed}"


def test_broken_page_and_image_are_detected(tmp_path):
    document = tmp_path / "README.md"
    document.write_text("[page](missing.md)\n![image](assets/missing.png)\n"
                        "[web](https://example.test/page)\n"
                        "```md\n[example](not-a-real-page.md)\n```\n")
    assert broken_links(tmp_path, [document]) == [
        "README.md: missing.md", "README.md: assets/missing.png",
    ]
    (tmp_path / "missing.md").write_text("# Page")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/missing.png").write_bytes(b"fixture")
    assert broken_links(tmp_path, [document]) == []
