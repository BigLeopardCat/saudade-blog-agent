"""Run the existing HTTP harness once against a reserved frozen candidate."""

import json
from pathlib import Path

from eval import online_baseline
from eval.improvement.artifacts import digest, encode, private_write
from eval.improvement.datasets import PROFILE, scope_issues


def run(registry, reservation, transport, identities, report_dir):
    claim = registry.get(reservation, "reservation")
    gate = registry.get(claim["development"], "development")
    candidate = registry.get(gate["candidate"], "candidate")
    split = registry.get(claim["datasets"], "datasets")
    if (gate["status"] != "passed" or claim["head"] != candidate["head"]
            or claim["datasets"] != candidate["datasets"]):
        raise ValueError("reservation does not match a passed frozen candidate")
    if digest(PROFILE.read_bytes()) != split["profile_sha256"] or scope_issues(split["holdout"]):
        raise ValueError("holdout policy changed or incomplete")
    # No retry after a crash or failed run: the final-test data has been exposed.
    private_write(registry.root / "executions" / (reservation + ".json"), encode(claim))
    online_baseline.run(split["holdout"], transport, identities, report_dir,
        phase="full", suite_hash=split["holdout_sha256"], dataset_role="holdout",
        expected_build=claim["head"], evaluation_id=reservation)
    path = Path(report_dir) / "report.json"
    key = registry.put("holdout_run", {"reservation": reservation,
        "report_sha256": digest(path.read_bytes()), "report": str(path.resolve())})
    private_write(registry.root / "completed" / (reservation + ".json"),
                  encode({"artifact": key}))
    return key


def completed(registry, reservation, report):
    path = registry.root / "completed" / (reservation + ".json")
    if not path.is_file():
        raise ValueError("reserved holdout run has not completed")
    proof = registry.get(json.loads(path.read_bytes())["artifact"], "holdout_run")
    if (proof["reservation"] != reservation
            or proof["report_sha256"] != digest(Path(report).read_bytes())):
        raise ValueError("holdout report differs from the recorded first run")
    return proof
