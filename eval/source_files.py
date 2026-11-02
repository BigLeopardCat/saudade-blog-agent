"""Python source scope shared by repository quality checks.

Git supplies tracked files and new, nonignored working files. Generated reports,
virtual environments and external checkouts follow the repository ignore rules.
Artifact retention is a separate concern and must inspect its own storage roots.
"""

import subprocess
from pathlib import Path


def python_sources(root: Path, targets=()) -> list[Path]:
    """Return existing source files, failing closed when Git cannot enumerate them."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "*.py"],
        cwd=root, check=True, capture_output=True,
    )
    paths = {Path(name.decode("utf-8")) for name in result.stdout.split(b"\0") if name}
    selected = tuple(Path(target) for target in targets)
    return [root / path for path in sorted(paths) if (root / path).is_file()
            and (not selected or any(path == target or target in path.parents
                                     for target in selected))]
