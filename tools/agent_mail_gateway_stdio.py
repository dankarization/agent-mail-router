"""Pinned Gateway SDK subprocess with mail payload on stdin, never argv."""
import json
import os
from pathlib import Path
import subprocess
import time

import agent_mail_routing_host as host


def call(config, helper_path, helper_sha256, method, params):
    if method not in ('sessions.list', 'chat.inject') or not isinstance(params, dict):
        raise ValueError('gateway_method_invalid')
    # Validate the operator-pinned R3 tree, config, binary and classifier before
    # the first SDK module executes. No mail is included in these checks.
    deadline = time.monotonic() + 15
    host._preflight(config, deadline)
    helper = Path(helper_path).resolve(strict=True)
    if host._hash_file(helper, deadline)[0] != helper_sha256:
        raise ValueError('gateway_helper_changed')
    raw = json.dumps({'method':method, 'params':params}, ensure_ascii=False, separators=(',', ':')).encode()
    if len(raw) > 32768:
        raise ValueError('gateway_input_too_large')
    try:
        proc = subprocess.run([config.node_path, str(helper), config.build_root], input=raw,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False,
                              env={**os.environ, 'OPENCLAW_CONFIG_PATH': config.config_path})
        result = json.loads(proc.stdout) if proc.returncode == 0 and len(proc.stdout) <= 8*1024*1024 else None
        if not isinstance(result, dict) or result.get('status') != 'ok' or 'value' not in result:
            raise ValueError('gateway_unavailable')
        # A configuration change after admission makes the outcome uncertain;
        # the caller's delivery journal must hold rather than blindly retry.
        host._preflight(config, time.monotonic() + 15)
        return result['value']
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise ValueError('gateway_unavailable') from None
