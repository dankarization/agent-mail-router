#!/usr/bin/env python3
"""Prepare an additive outbound-origin candidate. Never send mail or edit live routes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_mail_router import routes_at


def prepare(routes, route_id, entry, output):
    routes, output = Path(routes), Path(output)
    if routes.resolve() == output.resolve() or output.exists():
        raise ValueError("candidate_must_be_new_non_live_path")
    before = routes.read_bytes()
    cfg = json.loads(before)
    selected = [r for r in cfg["routes"] if r["id"] == route_id]
    if len(selected) != 1 or selected[0].get("fallback"):
        raise ValueError("verified_task_route_required")
    threads = selected[0].setdefault("outbound_threads", [])
    old = [t for r in cfg["routes"] for t in r.get("outbound_threads", []) if t["message_id"] == entry["message_id"]]
    if old:
        raise ValueError("outgoing_id_already_registered")
    threads.append(entry)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("x") as stream:
            os.chmod(output, 0o600)
            json.dump(cfg, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        routes_at(output)
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return {"candidate": str(output), "expected_routes_sha256": hashlib.sha256(before).hexdigest(),
            "candidate_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "effects": "none; operator reviews and atomically applies candidate under runtime.lock"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routes", required=True, type=Path)
    parser.add_argument("--route-id", required=True)
    parser.add_argument("--message-id", required=True)
    parser.add_argument("--recipient", required=True)
    parser.add_argument("--task-ref", default="")
    parser.add_argument("--sent-evidence", required=True)
    parser.add_argument("--candidate", required=True, type=Path)
    args = parser.parse_args()
    entry = {"message_id": args.message_id, "recipient": args.recipient,
             "task_ref": args.task_ref, "sent_evidence": args.sent_evidence}
    print(json.dumps(prepare(args.routes, args.route_id, entry, args.candidate), ensure_ascii=False))


if __name__ == "__main__":
    main()
