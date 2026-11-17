"""Check current documentation navigation and local assets without network access."""

from pathlib import Path
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[1]


def document_links(path: Path) -> list[str]:
    """Use the installed CommonMark parser for pages, images and references."""
    links = []
    for token in MarkdownIt().parse(path.read_text()):
        for child in token.children or []:
            attribute = {"link_open": "href", "image": "src"}.get(child.type)
            if attribute and (target := child.attrGet(attribute)):
                links.append(target)
    return links


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


def test_wiki_navigation_and_repository_references():
    directory = ROOT / "docs/wiki"
    pages = {path.stem for path in directory.glob("*.md") if not path.name.startswith("_")}
    prefix = "https://github.com/BigLeopardCat/saudade-blog-agent/"
    for name in ("Home.md", "_Sidebar.md"):
        linked = {unquote(urlsplit(link).path).rsplit("/", 1)[-1]
                  for link in document_links(directory / name) if link.startswith(prefix + "wiki/")}
        assert pages - {"Home"} <= linked, f"Unlisted Wiki tutorials in {name}"
    for page in directory.glob("*.md"):
        for link in document_links(page):
            target = unquote(link).split("#", 1)[0]
            if target.startswith(prefix + "wiki/"):
                assert target.rsplit("/", 1)[-1] in pages, (page.name, link)
            for kind in ("blob/main/", "tree/main/"):
                if target.startswith(prefix + kind):
                    assert (ROOT / target.removeprefix(prefix + kind)).exists(), (page.name, link)
