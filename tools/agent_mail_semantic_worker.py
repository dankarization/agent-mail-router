#!/usr/bin/env python3
"""Run at most one queued semantic context operation; never polls or sends mail.

Requires operator-owned private pinned host configuration and a Gateway with
incarnation-fenced chat.inject. This command is intentionally not scheduled by
installation; deploy only after the Gateway fence and delivery review pass.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys

import agent_mail_router as router
from agent_mail_gateway_catalog import load_scope
from agent_mail_gateway_stdio import call as private_gateway_call
from agent_mail_routing_host import TrustedHostConfig, classify
from agent_mail_routing_integration import run_one_queued, queue_status


def private_host_config(path, scope_path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) & 0o077 or before.st_size > 65536):
            raise ValueError('host_config_not_private')
        raw = stream.read(65537); after = os.fstat(stream.fileno())
        if (len(raw) > 65536 or (before.st_size, before.st_mtime_ns, before.st_mode)
                != (after.st_size, after.st_mtime_ns, after.st_mode)):
            raise ValueError('host_config_changed')
    config = json.loads(raw)
    if not isinstance(config, dict) or set(config) != set(TrustedHostConfig.__dataclass_fields__):
        raise ValueError('host_config_invalid')
    if not isinstance(config['allowed_agents'], list):
        raise ValueError('host_config_invalid')
    config['allowed_agents'] = tuple(config['allowed_agents'])
    host = TrustedHostConfig(**config)
    _, scope = load_scope(scope_path)
    if (Path(host.policy_path).resolve(strict=True) != Path(scope_path).resolve(strict=True)
            or host.agent_id not in scope.allowed_agents
            or not set(host.allowed_agents).issubset(scope.allowed_agents)):
        raise ValueError('host_scope_mismatch')
    return host


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state-dir', type=Path, required=True)
    p.add_argument('--routes', type=Path)
    p.add_argument('--scope', type=Path)
    p.add_argument('--host-config', type=Path)
    p.add_argument('--gateway-helper', type=Path)
    p.add_argument('--gateway-helper-sha256')
    p.add_argument('--status', action='store_true', help='Read bounded queue counts without invoking model or Gateway')
    args = p.parse_args(argv)
    os.umask(0o077)
    db = None
    try:
        if args.status:
            uri=(args.state_dir / 'mail.sqlite3').resolve(strict=True).as_uri() + '?mode=ro'
            db=sqlite3.connect(uri, uri=True, timeout=1)
            result=queue_status(db)
            try:
                fd=os.open(args.state_dir / 'semantic-worker.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        active=False
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except BlockingIOError:
                        active=True
                finally:
                    os.close(fd)
            except FileNotFoundError:
                active=False
            except OSError:
                active=None
            result['workerActive']=active
            result['interruptedProcessing']=(result['counts']['processing'] > 0 and not active) if active is not None else None
            print(json.dumps(result, sort_keys=True))
            return 0
        if not all((args.routes,args.scope,args.host_config,args.gateway_helper,args.gateway_helper_sha256)):
            raise ValueError('worker_config_missing')
        host = private_host_config(args.host_config, args.scope)
        # A separate lock prevents worker overlap without holding the normal
        # mail runtime lock during model inference. SQLite transactions remain short.
        with (args.state_dir / 'semantic-worker.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(json.dumps({'status':'idle','reason':'worker_already_running'})); return 0
            path = args.state_dir / 'mail.sqlite3'
            if not path.is_file():
                raise ValueError('mail_database_missing')
            db = sqlite3.connect(f'file:{path}?mode=rw', uri=True, timeout=1)
            db.row_factory = sqlite3.Row
            # Avoid Store.__init__: it performs global recovery of other effects.
            store = router.Store.__new__(router.Store)
            store.state_dir = args.state_dir
            store.routes = router.routes_at(args.routes)
            store.reload_routes = lambda: router.routes_at(args.routes)
            store.db = db
            gateway = lambda method, params: private_gateway_call(
                host, args.gateway_helper, args.gateway_helper_sha256, method, params)
            def prepare_content(key):
                # Existing prepare enforces report-only policy for untrusted senders.
                # Its outputs may contain content metadata, so never publish them.
                try:
                    proc = subprocess.run([sys.executable, str(Path(__file__).with_name('agent_mail_content.py')),
                                           '--state-dir', str(args.state_dir), 'prepare', '--key', key],
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, timeout=100, check=False)
                    return proc.returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    return False
            result = run_one_queued(store, args.scope, gateway,
                                   lambda source, options: classify(host, source, options),
                                   content_prepare=prepare_content)
            # Output fixed status only; never print mail, session keys, or receipts.
            print(json.dumps({'status':result.get('status','held'), 'reason':result.get('reason')}))
            return 0 if result.get('status') in ('idle','delivered','already_delivered') else 1
    except Exception:
        print(json.dumps({'status':'held','reason':'worker_unavailable'}))
        return 1
    finally:
        if db is not None:
            db.close()


if __name__ == '__main__':
    sys.exit(main())
