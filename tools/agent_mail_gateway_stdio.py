"""Pinned ordinary Gateway CLI bootstrap with mail on stdin, never OS argv."""
import json
import os
from pathlib import Path
import time

import agent_mail_routing_host as host


def call(config, helper_path, helper_sha256, method, params):
    if method not in ('sessions.list', 'chat.inject') or not isinstance(params, dict):
        raise ValueError('gateway_method_invalid')
    # Validate the operator-pinned R3 tree, config, binary and classifier before
    # the ordinary CLI executes. No mail is included in these checks.
    deadline = time.monotonic() + 15
    host._preflight(config, deadline)
    helper = Path(helper_path).resolve(strict=True)
    if host._hash_file(helper, deadline)[0] != helper_sha256:
        raise ValueError('gateway_helper_changed')
    raw = json.dumps({'method':method, 'params':params}, ensure_ascii=False, separators=(',', ':')).encode()
    if len(raw) > 32768:
        raise ValueError('gateway_input_too_large')
    try:
        # The existing owned-process-group exchange handles cooperative timeout,
        # forced cleanup and descendants; no file or process argv contains mail.
        env={**os.environ, 'OPENCLAW_CONFIG_PATH': config.config_path}
        data=host._exchange([config.node_path, str(helper), config.build_root], Path(config.build_root),
                            env, raw, time.monotonic()+45,
                            _validate_output=lambda value: _valid_reply(value), _max_stdout=8*1024*1024)
        result=json.loads(data)
        host._preflight(config, time.monotonic()+15)
        return result
    except (OSError, ValueError, host._Unsupported, host._Expired, host._CleanupFailed, host._OutputInvalid):
        raise ValueError('gateway_unavailable') from None


def _valid_reply(raw):
    try:
        return isinstance(json.loads(raw),dict)
    except (ValueError,TypeError,UnicodeError):
        return False
