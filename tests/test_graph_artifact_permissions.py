"""Public graph artifacts remain readable across isolated service identities."""
import os
import stat

import numpy as np

from rag.embed_space import Space
from scripts import build_word_graph as builder


def test_only_public_artifacts_override_private_service_umask(tmp_path):
    previous = os.umask(0o077)
    try:
        public, private = tmp_path / "web", tmp_path / "private"
        result = builder.write_artifacts(
            {"built": "fixture", "model": "fixture", "dim": 2},
            np.array([[1, 0]], dtype=np.float32), ["fixture"],
            {"mean": np.zeros(2), "dirs": np.zeros((0, 2))},
            public, private, False, "https://example.invalid",
            Space("embedding", "fixture", "https://example.invalid/v1", "offline"),
        )
        assert stat.S_IMODE(public.stat().st_mode) == 0o755
        assert stat.S_IMODE((public / result["file"]).stat().st_mode) == 0o644
        assert stat.S_IMODE((public / "manifest.json").stat().st_mode) == 0o644
        assert stat.S_IMODE((private / "index.json").stat().st_mode) == 0o600
        assert stat.S_IMODE((private / "vectors.f32").stat().st_mode) == 0o600
    finally:
        os.umask(previous)
