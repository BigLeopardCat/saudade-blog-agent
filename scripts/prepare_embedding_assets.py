"""Prepare pinned GGUF/runtime/profile assets; network access requires --download."""

import argparse
import hashlib
import json
import tarfile
import tempfile
from pathlib import Path

import httpx

MANIFEST = Path(__file__).resolve().parents[1] / "deploy/embedding/artifacts.json"


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def acquire(root: Path, asset: dict, download: bool) -> Path:
    path = root / asset["filename"]
    if path.is_file() and checksum(path) == asset["sha256"]:
        return path
    if not download:
        raise ValueError("missing/changed asset: " + asset["filename"])
    root.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    with httpx.stream("GET", asset["url"], follow_redirects=True, timeout=90) as response:
        response.raise_for_status()
        with temporary.open("wb") as stream:
            size = 0
            for block in response.iter_bytes():
                size += len(block)
                if size > asset["max_bytes"]:
                    raise ValueError("download exceeds pinned size limit")
                stream.write(block)
    if checksum(temporary) != asset["sha256"]:
        temporary.unlink()
        raise ValueError("asset SHA-256 mismatch")
    temporary.replace(path)
    return path


def runtime_ready(root: Path, archive: Path, verify_only: bool) -> Path:
    executable = root / "runtime/llama-b11829/llama-server"
    destination = root / "runtime/llama-b11829"
    if not destination.exists():
        if verify_only:
            raise ValueError("runtime has not been prepared")
        (root / "runtime").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root, prefix=".runtime-") as temporary:
            with tarfile.open(archive) as bundle:
                bundle.extractall(temporary, filter="data")
            (Path(temporary) / "llama-b11829").rename(destination)
    # Never overwrite a running executable/library, and verify extracted files too.
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            path = root / "runtime" / member.name
            if member.isfile():
                source = bundle.extractfile(member)
                if source is None:
                    raise ValueError("invalid runtime archive member")
                with source:
                    expected = hashlib.file_digest(source, "sha256").hexdigest()
                if not path.is_file() or checksum(path) != expected:
                    raise ValueError("changed immutable runtime: " + member.name)
            elif member.issym():
                if not path.is_symlink() or str(path.readlink()) != member.linkname:
                    raise ValueError("changed runtime symlink")
    if not executable.is_file():
        raise ValueError("runtime executable is missing")
    return executable


def prepare(root: Path, model_name: str, download: bool, verify_only: bool) -> dict:
    manifest = json.loads(MANIFEST.read_text())
    model = manifest["models"][model_name]
    runtime = acquire(root, manifest["runtime"], download)
    model_asset = {
        **model,
        "url": "https://huggingface.co/"
        + model["repository"]
        + "/resolve/"
        + model["revision"]
        + "/"
        + model["filename"],
    }
    acquire(root, model_asset, download)
    tokenizer = model.get("tokenizer") or manifest["models"][model["tokenizer_from"]]["tokenizer"]
    acquire(root, tokenizer, download)
    profile = {
        "schema_version": 1,
        "model_revision": model["repository"]
        + "@"
        + model["revision"]
        + "/"
        + model["filename"]
        + "/"
        + model["sha256"]
        + ";llama-b11829;"
        + manifest["runtime"]["tokenizer_overrides"],
        "tokenizer_file": tokenizer["filename"],
        "tokenizer_sha256": tokenizer["sha256"],
        "max_tokens": 512,
        "add_special_tokens": True,
        "query_prefix": model["query_prefix"],
        "document_prefix": "",
        "pooling": model["pooling"],
        "normalization": "L2",
        "unicode_normalization": model.get("unicode_normalization", "none"),
    }
    profile_path = root / (model_name + ".json")
    executable = runtime_ready(root, runtime, verify_only)
    if verify_only:
        if not profile_path.is_file():
            raise ValueError("runtime/profile have not been prepared")
        if json.loads(profile_path.read_text()) != profile:
            raise ValueError("generated profile differs from pinned model")
    else:
        profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n")
    return {
        "model": model_name,
        "alias": model["alias"],
        "expected_dimension": model["dimension"],
        "profile": str(profile_path.resolve()),
        "model_file": str((root / model["filename"]).resolve()),
        "server": str(executable.resolve()),
        "pooling": model["pooling"],
        "verified": True,
    }


def export_query_contract(profile_path: Path, destination: Path) -> dict:
    """Export verified, immutable query assets without a consumer runtime dependency."""
    from rag.embedding_inputs import load_profile, signature_payload

    profile = load_profile(str(profile_path))
    payload = signature_payload(profile.spec)
    artifact = (profile_path.resolve().parent / profile.spec.tokenizer_file).read_bytes()
    destination.mkdir(parents=True, exist_ok=True)
    if ";llama-" not in profile.spec.model_revision:
        raise ValueError("query contract export requires a llama.cpp model deployment")
    files = {
        f"tokenizer-{profile.spec.tokenizer_sha256}.json": artifact,
        f"query-profile-{profile.signature}.json": json.dumps(
            {
                "schema_version": 1,
                "signature_payload": payload.decode(),
                "tokenization_protocol": "llama.cpp-tokenize-v1",
            },
            ensure_ascii=False,
        ).encode(),
    }
    for name, data in files.items():
        path = destination / name
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError("changed immutable query contract asset")
            continue
        with tempfile.NamedTemporaryFile(dir=destination, delete=False) as file:
            temporary = Path(file.name)
            try:
                file.write(data)
                file.flush()
                temporary.chmod(0o644)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
    return {
        "query_contract": str((destination / f"query-profile-{profile.signature}.json").resolve()),
        "signature": profile.signature,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--model", required=True, choices=("bge-q8", "bge-f16", "qwen-q8"))
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--export-query-contract", type=Path)
    args = parser.parse_args()
    if args.verify_only and args.download:
        parser.error("verify-only does not download")
    result = prepare(args.directory, args.model, args.download, args.verify_only)
    if args.export_query_contract:
        result.update(export_query_contract(Path(result["profile"]), args.export_query_contract))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
