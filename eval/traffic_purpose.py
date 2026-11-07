"""Read-only evaluation-purpose annotations from explicitly supplied canonical reports."""

import hashlib
import json
import re
from pathlib import Path

from utils.trace_io import load_trace

ALL_TRAFFIC = "all_traffic"
UNANNOTATED = "excluding_known_evaluation"


def _hex(value, size):
    return isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{size}}}", value) is not None


def _fingerprint(doc):
    return hashlib.sha256(json.dumps(doc, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _build(doc):
    meta = doc.get("input")
    runtime = meta.get("runtime") if isinstance(meta, dict) else None
    return runtime.get("build_sha") if isinstance(runtime, dict) else None


def _requests(report_paths):
    requests, reports = {}, set()
    for value in report_paths:
        path = Path(value).resolve()
        if path in reports:
            continue
        report = json.loads(path.read_text())
        version = report.get("service_version") if isinstance(report, dict) else None
        build = version.get("sha") if isinstance(version, dict) else None
        if (not isinstance(report, dict) or report.get("phase") not in ("poc", "full")
                or not _hex(report.get("suite_sha256"), 64) or not _hex(build, 40)
                or not isinstance(report.get("requests"), list)):
            raise ValueError("evaluation annotation requires a canonical HTTP report")
        seen = set()
        for row in report["requests"]:
            identity = row.get("request_id") if isinstance(row, dict) else None
            if (not _hex(identity, 32) or identity in seen
                    or row.get("transport") not in ("agent_http", "blog_http")):
                raise ValueError("invalid or duplicate canonical evaluation request")
            seen.add(identity)
            trace, digest = row.get("trace"), row.get("trace_sha256")
            fingerprint = None
            if trace:
                if (not isinstance(trace, str) or Path(trace).name != identity + ".json"
                        or Path(trace).parent.name != "traces" or not _hex(digest, 64)):
                    raise ValueError("invalid canonical evaluation trace reference")
                # Captures travel with the report; never follow a report's arbitrary file path.
                raw = (path.parent / "traces" / (identity + ".json")).read_bytes()
                if hashlib.sha256(raw).hexdigest() != digest:
                    raise ValueError("evaluation capture checksum mismatch")
                doc = json.loads(raw)
                if (not isinstance(doc, dict) or doc.get("trace_id") != identity
                        or _build(doc) != build):
                    raise ValueError("evaluation capture identity or build mismatch")
                fingerprint = _fingerprint(doc)
            elif trace != "" or digest is not None:
                raise ValueError("invalid uncaptured evaluation request")
            previous = requests.get(identity)
            if previous is not None:
                if previous[0] != build or (previous[1] is not None and fingerprint is not None
                                           and previous[1] != fingerprint):
                    raise ValueError("conflicting canonical evaluation annotations")
                fingerprint = fingerprint or previous[1]
            requests[identity] = build, fingerprint
        reports.add(path)
    return requests, len(reports)


def partition(paths, report_paths):
    """Only exact matching evidence is excluded; conflicts and unreadable files remain visible."""
    requests, reports = _requests(report_paths)
    known, unannotated, conflicts = [], [], 0
    for path in paths:
        doc = load_trace(path) if requests else None
        identity = doc.get("trace_id") if isinstance(doc, dict) else None
        reference = requests.get(identity) if _hex(identity, 32) else None
        if reference is None:
            unannotated.append(path)
        elif (_build(doc) == reference[0]
                and (reference[1] is None or _fingerprint(doc) == reference[1])):
            known.append(path)
        else:
            conflicts += 1
            unannotated.append(path)
    return known, unannotated, {
        "canonical_reports": reports, "registered_request_ids": len(requests),
        "annotated_trace_files": len(known), "unannotated_trace_files": len(unannotated),
        "conflicting_trace_files": conflicts,
        "limitation": "Unannotated traffic is not verified human traffic; "
                      "request completion and replies are not task quality.",
    }


def annotate(paths, report_paths, collect):
    """Preserve total accounting and add views; never change traces or quality judgments."""
    paths, report_paths = list(paths), list(report_paths)
    known, unannotated, evidence = partition(paths, report_paths)
    result = {**collect(paths), "traffic_scope": ALL_TRAFFIC, "includes_evaluations": True,
              "evaluation_annotation": evidence}
    if report_paths:
        result["views"] = {
            "known_evaluation": {**collect(known), "traffic_scope": "known_evaluation"},
            "excluding_known_evaluation": {**collect(unannotated), "traffic_scope": UNANNOTATED},
        }
    return result
