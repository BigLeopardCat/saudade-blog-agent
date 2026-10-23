"""Prepare the pinned, complete BEIR SciFact test set (explicit network opt-in)."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.rag_dataset import load  # noqa: E402

SOURCE = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/scifact.zip"
SHA256 = "536e14446a0ba56ed1398ab1055f39fe852686ecad24a6306c80c490fa8e0165"
MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_MEMBER_BYTES = 20 * 1024 * 1024
FILES = ("corpus.jsonl", "queries.jsonl", "qrels/test.tsv")
MANIFEST = {
    "schema_version": 1,
    "name": "BEIR SciFact",
    "source": SOURCE,
    "revision": "sha256:" + SHA256,
    "license": "SciFact claims/annotations: CC-BY-4.0; abstracts: ODC-By-1.0",
    "license_source": "https://github.com/allenai/scifact/blob/master/LICENSE.md",
    "format_source": "https://github.com/beir-cellar/beir/wiki/Datasets-available",
    "scope": "complete BEIR test: 5183 documents, 300 queries; English scientific claims",
    "limitations": (
        "Document relevance only; no chunk evidence, no unanswerable or required-evidence-group "
        "labels. Biomedical abstracts do not represent Chinese/mixed-language blog traffic."
    ),
}


def download(path: Path) -> None:
    with httpx.stream("GET", SOURCE, follow_redirects=True, timeout=60) as response:
        response.raise_for_status()
        total = 0
        with path.open("wb") as stream:
            for block in response.iter_bytes():
                total += len(block)
                if total > MAX_ARCHIVE_BYTES:
                    raise ValueError("archive exceeds download limit")
                stream.write(block)


def prepare(archive: Path, output: Path) -> dict:
    if output.exists():
        raise FileExistsError("output already exists; choose a new directory")
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("archive exceeds size limit")
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise ValueError("SciFact archive SHA-256 mismatch")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Validate in a sibling temporary directory; failed preparation publishes nothing.
    with tempfile.TemporaryDirectory(prefix=".scifact-", dir=output.parent) as temporary:
        root = Path(temporary) / "dataset"
        root.mkdir()
        with zipfile.ZipFile(archive) as bundle:
            for name in FILES:
                member = bundle.getinfo("scifact/" + name)
                if member.file_size > MAX_MEMBER_BYTES:
                    raise ValueError("archive member exceeds size limit")
                destination = root / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                # Copy only fixed member names, never extract paths supplied by the archive.
                with bundle.open(member) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)
        (root / "manifest.json").write_text(
            json.dumps(MANIFEST, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        data = load(root, "test")
        if len(data["corpus"]) != 5183 or len(data["queries"]) != 300:
            raise ValueError("unexpected full-test cardinality")
        root.rename(output)
    return {"manifest": data["manifest"], "input_hashes": data["hashes"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--archive", type=Path, help="use an already downloaded archive offline")
    inputs.add_argument(
        "--download", action="store_true", help="download the pinned public archive"
    )
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("output already exists; choose a new directory")
    if args.archive:
        result = prepare(args.archive, args.output)
    else:
        with tempfile.TemporaryDirectory(prefix="scifact-download-") as temporary:
            archive = Path(temporary) / "scifact.zip"
            download(archive)
            result = prepare(archive, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
