"""Run feedback stages; only generate and run-holdout make external requests."""

import argparse
import json
from pathlib import Path

from eval.improvement import candidates, datasets, feedback, holdout, release
from eval.improvement.artifacts import Registry


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    review = sub.add_parser("review")
    review.add_argument("--source", type=Path, required=True)
    review.add_argument("--label", type=Path, required=True)
    seal = sub.add_parser("seal")
    for name in ("development", "holdout"):
        seal.add_argument("--" + name, type=Path, required=True)
    seal.add_argument("--curator", required=True)
    seal.add_argument("--independence-review", required=True)
    propose = sub.add_parser("prepare")
    propose.add_argument("--datasets", required=True)
    propose.add_argument("--review", action="append", required=True)
    propose.add_argument("--repo", type=Path, default=Path.cwd())
    propose.add_argument("--base", required=True)
    propose.add_argument("--max-candidates", type=int, default=1)
    generate = sub.add_parser("generate")
    generate.add_argument("--input", required=True)
    generate.add_argument("--response", type=Path, help="Import reviewed proposal JSON offline")
    freeze = sub.add_parser("freeze")
    freeze.add_argument("--proposal", required=True)
    freeze.add_argument("--index", type=int, default=0)
    freeze.add_argument("--repo", type=Path, default=Path.cwd())
    freeze.add_argument("--head", required=True)
    freeze.add_argument("--reviewer", required=True)
    dev = sub.add_parser("development")
    dev.add_argument("--candidate", required=True)
    for name in ("before", "after", "before-semantic", "after-semantic"):
        dev.add_argument("--" + name, type=Path, required=True)
    dev.add_argument("--reviewer", required=True)
    dev.add_argument("--calibration", type=Path, required=True)
    dev.add_argument("--comparison-conditions", required=True)
    holdout = sub.add_parser("reserve-holdout")
    holdout.add_argument("--development", required=True)
    run = sub.add_parser("run-holdout")
    run.add_argument("--reservation", required=True)
    run.add_argument("--report-dir", type=Path, required=True)
    run.add_argument("--agent-url", default="http://127.0.0.1:8010")
    run.add_argument("--blog-url", default="http://127.0.0.1:3000")
    run.add_argument("--host", action="store_true")
    for role in ("admin", "user", "zako"):
        run.add_argument("--" + role + "-uid", type=int, required=True)
    packet = sub.add_parser("release-packet")
    packet.add_argument("--reservation", required=True)
    packet.add_argument("--report", type=Path, required=True)
    packet.add_argument("--semantic", type=Path, required=True)
    packet.add_argument("--reviewer", required=True)
    return parser.parse_args()


def dispatch(registry, args):
    match args.command:
        case "review":
            return feedback.review(registry, registry.evidence(args.source),
                                   json.loads(args.label.read_bytes()))
        case "seal":
            return datasets.seal(registry, args.development, args.holdout,
                curator=args.curator, independence_review=args.independence_review)
        case "prepare":
            return candidates.packet(registry, args.datasets, args.review, args.repo,
                                     args.base, args.max_candidates)
        case "generate":
            return (candidates.proposals(registry, args.input,
                                         json.loads(args.response.read_bytes()))
                    if args.response else candidates.generate(registry, args.input))
        case "freeze":
            return candidates.freeze(registry, args.proposal, args.index, args.repo,
                                     args.head, args.reviewer)
        case "development":
            return release.development(registry, args.candidate, args.before, args.after,
                args.before_semantic, args.after_semantic, reviewer=args.reviewer,
                comparison_conditions=args.comparison_conditions, calibration_path=args.calibration)
        case "reserve-holdout":
            return datasets.reserve(registry, args.development)
        case "run-holdout":
            from eval.online_transport import OnlineTransport

            transport = OnlineTransport(agent_url=args.agent_url, blog_url=args.blog_url,
                report_dir=args.report_dir, host=args.host)
            try:
                return holdout.run(registry, args.reservation, transport,
                    {"admin": args.admin_uid, "user": args.user_uid, "zako": args.zako_uid},
                    args.report_dir)
            finally:
                transport.client.close()
        case "release-packet":
            return release.release_packet(registry, args.reservation, args.report,
                                          args.semantic, reviewer=args.reviewer)
    raise ValueError("unknown offline stage")


def main():
    args = arguments()
    registry = Registry(args.registry)
    key = dispatch(registry, args)
    output = {"artifact": key, "registry": str(registry.root)}
    if args.command == "seal":
        output["development_suite"] = str(registry.root / "datasets" / key / "development.jsonl")
    elif args.command == "reserve-holdout":
        claim = registry.get(key, "reservation")
        output["holdout_suite"] = str(registry.root / "datasets" / claim["datasets"]
                                      / "holdout.jsonl")
        output["expected_build"] = claim["head"]
    elif args.command in {"development", "release-packet"}:
        kind = "development" if args.command == "development" else "release_packet"
        output["status"] = registry.get(key, kind)["status"]
    print(json.dumps(output))
    return 1 if output.get("status") == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
