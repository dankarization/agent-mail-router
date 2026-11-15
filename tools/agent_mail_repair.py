#!/usr/bin/env python3
"""Fixed, allowlisted execution boundary for bounded watchdog repair.

The observer never launches processes. This helper accepts only the exact
argv shapes for the two idempotent delegate actions and re-validates them
independently, so a corrupted plan cannot become an arbitrary command.
Non-stdlib imports are avoided; no shell, no shell=True and no arbitrary
command is ever constructed. Unknown outcomes are reported, never retried.
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

# The deployed helpers carry an import prefix; both canonical and prefixed
# sibling names must validate so one artifact serves source and deployment.
KEY = re.compile(r'[1-9][0-9]{0,9}:[1-9][0-9]{0,9}')
STATE = re.compile(r'/[!-~]{1,400}')
ROUTER = ('agent_mail_router.py', 'syncopia_agent_mail_router.py')
CONTENT = ('agent_mail_content.py', 'syncopia_agent_mail_content.py')


def validate(argv):
    """Return the normalized action for an exact allowlisted argv, else None."""
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        return None
    if argv[0] != sys.executable:
        return None
    tail = Path(argv[1]).name if len(argv) > 1 else ''
    if tail not in ROUTER + CONTENT:
        return None
    if len(argv) == 5 and tail in ROUTER:
        if argv[2] == '--state-dir' and STATE.fullmatch(argv[3]) and argv[4] == 'dispatch':
            return 'dispatch'
        return None
    if len(argv) == 8 and tail in CONTENT:
        if (argv[2] == '--state-dir' and STATE.fullmatch(argv[3]) and argv[4] == 'seen'
                and argv[5] == '--key' and KEY.fullmatch(argv[6]) and argv[7] == '--apply'):
            return 'seen'
        return None
    return None


OK_STATUS = ('sent', 'stored', 'confirmed', 'eligible', 'resolved', 'ok')
UNKNOWN_STATUS = ('unknown', 'held', 'pending', 'inflight', 'rejected', 'deferred', 'quiet')


def classify(payload):
    """Map a delegate's own reported status to one fixed outcome, or None."""
    items = payload if isinstance(payload, list) else [payload]
    if not items or not all(isinstance(i, dict) and isinstance(i.get('status'), str) for i in items):
        return None
    statuses = [i['status'] for i in items]
    if all(s in OK_STATUS for s in statuses):
        return 'ok', 'confirmed' if 'confirmed' in statuses else statuses[0]
    if all(s == 'deferred' for s in statuses):
        return 'deferred', 'deferred'
    if any(s in UNKNOWN_STATUS for s in statuses):
        return 'unknown', 'delegate_uncertain'
    return 'failed', 'delegate_rejected'


def run(argv, timeout=45):
    """One bounded delegate process with fixed outcome codes only."""
    action = validate(argv)
    if action is None:
        return {'status': 'failed', 'code': 'repair_argv_rejected'}
    if action == "dispatch":
        if timeout <= 2:
            return {"status": "unknown", "code": "repair_budget_exhausted"}
        argv = [*argv, "--deadline-monotonic", str(time.monotonic() + timeout - 2)]
    try:
        process = subprocess.run(argv, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {'status': 'unknown', 'code': 'repair_action_timeout'}
    except OSError:
        return {'status': 'unknown', 'code': 'repair_action_unavailable'}
    if process.returncode:
        return {'status': 'failed', 'code': 'repair_action_failed'}
    try:
        payload = json.loads(process.stdout)
    except (ValueError, UnicodeError):
        return {'status': 'unknown', 'code': 'repair_output_invalid'}
    outcome = classify(payload)
    if outcome is None:
        return {'status': 'unknown', 'code': 'repair_output_invalid'}
    return {'status': outcome[0], 'code': 'repair_' + action + '_' + outcome[1][:24]}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'cycle':
        return cycle(argv[1:])
    if len(argv) < 2 or argv[0] != 'run':
        print(json.dumps({'status': 'error', 'code': 'repair_usage_invalid'}))
        return 1
    try:
        planned = json.loads(argv[1])
    except (ValueError, UnicodeError, TypeError):
        print(json.dumps({'status': 'error', 'code': 'repair_usage_invalid'}))
        return 1
    print(json.dumps(run(planned)))
    return 0


def cycle(argv):
    """One ordinary watcher tick plus at most one bounded repair.

    Observation, admission, alert projection and delivery stay owned by the
    watchdog module; this helper only supplies the process-launch boundary it
    is not allowed to contain itself.
    """
    allowed = {'--state-dir', '--scheduler-db', '--job-id', '--store-key',
               '--analyzer-config'}
    forwarded = ['check-analyze']
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == '--dispatch':
            forwarded.append(item)
        elif item in allowed and index + 1 < len(argv):
            forwarded += [item, argv[index + 1]]
            index += 1
        else:
            print(json.dumps({'status': 'error', 'code': 'repair_usage_invalid'}))
            return 1
        index += 1
    forwarded.append('--repair')
    import importlib.util
    # Sibling module name follows the deployed import prefix; the script's own
    # directory is already on sys.path, so its absolute imports resolve.
    watchdog_path = Path(__file__).resolve().parent / Path(__file__).name.replace('repair.py', 'watchdog.py')
    spec = importlib.util.spec_from_file_location(watchdog_path.stem, watchdog_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[watchdog_path.stem] = module
    spec.loader.exec_module(module)
    return module.main(forwarded, runner=run)


if __name__ == '__main__':
    raise SystemExit(main())