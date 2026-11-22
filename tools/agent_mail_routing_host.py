"""Pinned source-bridge host boundary; never wired to the mail runtime here.

All paths, hashes, agent authority and budgets are operator-owned, not mail data.
"""
from dataclasses import dataclass
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time

from agent_mail_routing import SelectionInput, Shortlist, model_input

_CODES = {'classifier_input_invalid', 'classifier_runtime_unsupported', 'classifier_source_changed',
          'classifier_timeout', 'classifier_unavailable', 'classifier_output_invalid', 'classifier_cleanup_failed'}
_ROOTS = ('src', 'scripts', 'packages', 'extensions', 'dist', 'node_modules')
_EXCLUDED = {'.git', '.qa-results', '.artifacts', '.cache', 'coverage', '__pycache__',
             'operator-artifact-manifest.json'}
_REQUIRED = {'package.json', 'dist/build-info.json', 'scripts/tsx.mjs',
             'src/config/config.ts', 'src/agents/utility-completion.ts',
             'src/agents/agent-scope.ts', 'src/agents/isolated-completion-route.ts',
             'src/agents/isolated-completion.ts', 'src/shared/async-work-scope.ts',
             'src/agents/prepared-model-runtime.lifecycle.ts', 'src/cli/runtime-cleanup.ts'}
_HEX = re.compile(r'[a-f0-9]{64}\Z')
_COMMIT = re.compile(r'[a-f0-9]{40}\Z')
_MAX_MANIFEST = 64 * 1024 * 1024
_MAX_ENVELOPE_BYTES = 192 * 1024
_MAX_STDOUT = 8192


@dataclass(frozen=True)
class TrustedHostConfig:
    node_path: str
    node_sha256: str
    adapter_path: str
    adapter_sha256: str
    allowed_build_parent: str
    build_root: str
    expected_commit: str
    manifest_path: str
    manifest_sha256: str
    config_path: str
    config_sha256: str
    policy_path: str
    policy_sha256: str
    agent_id: str
    allowed_agents: tuple[str, ...]
    wall_timeout_ms: int


@dataclass(frozen=True)
class BridgeResult:
    status: str
    decision_raw: str | None = None
    error_code: str | None = None


class _Unsupported(Exception): pass
class _Changed(Exception): pass
class _Expired(Exception): pass
class _CleanupFailed(Exception): pass
class _OutputInvalid(Exception): pass


def _deadline(end):
    if time.monotonic() >= end:
        raise _Expired()


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate')
        out[key] = value
    return out


def _json(raw):
    return json.loads(raw, object_pairs_hook=_unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('constant')))


def _serialize_envelope(host_value, payload):
    data = json.dumps({'version': 1, 'host': host_value, 'payload': payload},
                      ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return data if len(data) <= _MAX_ENVELOPE_BYTES else None


def _path(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise _Unsupported()
    return Path(value).resolve(strict=True)


def _hash_file(path, end, maximum=None):
    _deadline(end)
    digest = hashlib.sha256(); size = 0
    with path.open('rb') as stream:
        while True:
            _deadline(end)
            block = stream.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            if maximum is not None and size > maximum:
                raise _Unsupported()
            digest.update(block)
    return digest.hexdigest(), size


def _no_includes(value):
    if isinstance(value, dict):
        return '$include' not in value and all(_no_includes(v) for v in value.values())
    return not isinstance(value, list) or all(_no_includes(v) for v in value)


def _confined(root, relative):
    if (not isinstance(relative, str) or not relative or '\x00' in relative
            or Path(relative).is_absolute() or any(p in ('..', '.') for p in relative.split('/'))):
        raise _Unsupported()
    path = root / relative
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root) or resolved != path:
        raise _Unsupported()
    for part in (*Path(relative).parts, *resolved.relative_to(root).parts):
        if part in _EXCLUDED or part.startswith('native-declarations-'):
            raise _Unsupported()
    return path


def _preflight(cfg: TrustedHostConfig, end):
    """Check pinned full tree before the tsx loader is allowed to execute."""
    if (not isinstance(cfg, TrustedHostConfig) or not _COMMIT.fullmatch(cfg.expected_commit)
            or not all(isinstance(x, str) and _HEX.fullmatch(x) for x in
                       (cfg.node_sha256, cfg.adapter_sha256, cfg.manifest_sha256,
                        cfg.config_sha256, cfg.policy_sha256))
            or not isinstance(cfg.allowed_agents, tuple) or cfg.agent_id not in cfg.allowed_agents
            or not isinstance(cfg.agent_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', cfg.agent_id)
            or type(cfg.wall_timeout_ms) is not int or not 3000 <= cfg.wall_timeout_ms <= 120000):
        raise _Unsupported()
    root = _path(cfg.build_root); parent = _path(cfg.allowed_build_parent)
    if root == parent or not root.is_relative_to(parent) or not root.is_dir():
        raise _Unsupported()
    node, adapter, manifest = _path(cfg.node_path), _path(cfg.adapter_path), _path(cfg.manifest_path)
    config, policy = _path(cfg.config_path), _path(cfg.policy_path)
    if manifest != root / 'operator-artifact-manifest.json':
        raise _Unsupported()
    for path, expected in ((node, cfg.node_sha256), (adapter, cfg.adapter_sha256)):
        if _hash_file(path, end)[0] != expected:
            raise _Changed()
    pinned = []
    for path, expected, maximum in ((manifest, cfg.manifest_sha256, _MAX_MANIFEST),
                                    (config, cfg.config_sha256, 4 * 1024 * 1024),
                                    (policy, cfg.policy_sha256, 4 * 1024 * 1024)):
        _deadline(end)
        with path.open('rb') as stream:
            raw = stream.read(maximum + 1)
        _deadline(end)
        if len(raw) > maximum:
            raise _Unsupported()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise _Changed()
        pinned.append(_json(raw))
    data, config_value, policy_value = pinned
    if not _no_includes(config_value) or not _no_includes(policy_value):
        raise _Unsupported()
    if (not isinstance(data, dict) or data.get('commit') != cfg.expected_commit
            or not isinstance(data.get('files'), list)
            or len(data['files']) > 500000):
        raise _Unsupported()
    # The operator verifies the complete artifact once at freeze/install and
    # keeps this versioned root immutable. This per-call gate verifies only
    # pinned critical entrypoints; it does not prove transitive filesystem integrity.
    required = _REQUIRED
    files = {}
    for entry in data['files']:
        _deadline(end)
        if not isinstance(entry, dict):
            raise _Unsupported()
        name = entry.get('path')
        if name not in required:
            continue
        if (name in files or not {'path', 'bytes', 'sha256'} <= set(entry)
                or type(entry['bytes']) is not int or entry['bytes'] < 0
                or not isinstance(entry['sha256'], str) or not _HEX.fullmatch(entry['sha256'])):
            raise _Unsupported()
        path = _confined(root, name)
        if path.is_symlink() or not path.is_file():
            raise _Unsupported()
        mode = entry.get('mode')
        if mode is not None and (type(mode) is not int or stat.S_IMODE(path.stat().st_mode) != stat.S_IMODE(mode)):
            raise _Changed()
        actual, size = _hash_file(path, end)
        if size != entry['bytes'] or actual != entry['sha256']:
            raise _Changed()
        files[name] = entry
    if not required <= set(files):
        raise _Unsupported()
    package = _json((root / 'package.json').read_bytes())
    build = _json((root / 'dist/build-info.json').read_bytes())
    if not isinstance(package, dict) or package.get('name') != 'openclaw' or not isinstance(build, dict) or build.get('commit') != cfg.expected_commit:
        raise _Unsupported()
    _deadline(end)
    return root, node, adapter


def _proc_identity(pid):
    # Linux stat fields after the last ')' begin with state, ppid, pgrp,
    # session; starttime is field 22. Parentheses may occur in comm.
    raw = Path('/proc', str(pid), 'stat').read_text()
    rest = raw[raw.rfind(')') + 2:].split()
    if len(rest) < 20:
        raise _CleanupFailed()
    return rest[0], int(rest[2]), int(rest[3]), int(rest[19])


def _leader_identity(process):
    if (sys.platform != 'linux' or not all(hasattr(os, name) for name in
            ('waitid', 'P_PID', 'WEXITED', 'WNOHANG', 'WNOWAIT'))):
        raise _Unsupported()
    try:
        state, group, session, started = _proc_identity(process.pid)
        if group != process.pid or session != process.pid or group == os.getpgrp():
            raise _CleanupFailed()
        return process.pid, group, session, started
    except (OSError, ValueError, IndexError):
        raise _CleanupFailed() from None


def _verify_leader(process, identity):
    try:
        pid, group, session, started = identity
        if process.pid != pid or process.returncode is not None:
            raise _CleanupFailed()
        _, current_group, current_session, current_started = _proc_identity(pid)
        if (current_group, current_session, current_started) != (group, session, started):
            raise _CleanupFailed()
        # Must still be our child and not reaped. WNOWAIT reserves the leader PID
        # and process-group identity through cleanup, even after it exits.
        return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except (OSError, ValueError, IndexError):
        raise _CleanupFailed() from None


def _group_has_live_members(group, ignore_pid=None):
    try:
        with os.scandir('/proc') as entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                try:
                    state, pgid, _, _ = _proc_identity(int(entry.name))
                except FileNotFoundError:
                    continue
                if pgid == group and int(entry.name) != ignore_pid and state not in ('Z', 'X'):
                    return True
        return False
    except (OSError, ValueError, IndexError):
        raise _CleanupFailed() from None


def _stop_group(process, identity, end):
    """Force only our verified process group; never report forced cleanup as success."""
    try:
        _verify_leader(process, identity)
        if _group_has_live_members(identity[1]):
            _verify_leader(process, identity)
            os.killpg(identity[1], signal.SIGKILL)
        while True:
            status = _verify_leader(process, identity)
            if status is not None and not _group_has_live_members(identity[1], ignore_pid=process.pid):
                process.wait(timeout=max(0.001, end - time.monotonic()))
                break
            if time.monotonic() >= end:
                break
            time.sleep(min(0.01, max(0, end - time.monotonic())))
    except (OSError, subprocess.SubprocessError):
        raise _CleanupFailed() from None
    raise _CleanupFailed()


def _schedule(end, now):
    """One classify-entry hard deadline, with 15s work and 2s reap reserves."""
    work = end - 15.0
    if now >= work - 0.001:
        raise _Expired()
    return work, end - 14.0, end - 2.0


def _control_action(now, term_at, force_at, term_sent, failed):
    if now >= force_at:
        return 'force'
    if not term_sent and (failed or now >= term_at):
        return 'term'
    return 'wait'


def _valid_output_shape(raw):
    try:
        value = _json(raw)
        if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
            return False
        if set(value) == {'version', 'status', 'decisionRaw'} and value['status'] == 'ok':
            return isinstance(value['decisionRaw'], str) and len(value['decisionRaw'].encode('utf-8')) <= 4096
        return (set(value) == {'version', 'status', 'errorCode'} and value['status'] == 'deferred'
                and isinstance(value['errorCode'], str) and value['errorCode'] in _CODES)
    except (ValueError, TypeError, RecursionError, UnicodeEncodeError):
        return False


def _require_native_sigchld_default():
    """Read Linux x86_64 glibc sigaction; do not change process signal state.

    The standalone caller must not concurrently change SIGCHLD or reap our
    child. This narrow ABI check does not establish detached-native cleanup.
    """
    try:
        if (sys.platform != 'linux' or os.uname().machine != 'x86_64'
                or ctypes.sizeof(ctypes.c_void_p) != 8 or ctypes.sizeof(ctypes.c_ulong) != 8
                or not os.confstr('CS_GNU_LIBC_VERSION').startswith('glibc ')):
            raise _Unsupported()

        class Sigaction(ctypes.Structure):
            _fields_ = [('handler', ctypes.c_void_p), ('mask', ctypes.c_ulong * 16),
                        ('flags', ctypes.c_int), ('restorer', ctypes.c_void_p)]

        if (ctypes.sizeof(Sigaction) != 152 or Sigaction.flags.offset != 136
                or Sigaction.restorer.offset != 144):
            raise _Unsupported()
        action = Sigaction()
        libc = ctypes.CDLL(None, use_errno=True)
        read_action = libc.sigaction
        read_action.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(Sigaction)]
        read_action.restype = ctypes.c_int
        if read_action(signal.SIGCHLD, None, ctypes.byref(action)) != 0:
            raise _Unsupported()
        if action.handler is not None or action.flags & 0x2:  # SIG_DFL, !SA_NOCLDWAIT
            raise _Unsupported()
    except (AttributeError, OSError, TypeError, ValueError):
        raise _Unsupported() from None


def _exchange(argv, cwd, env, payload, end, *, _bounds=None,
              _validate_output=_valid_output_shape, _max_stdout=_MAX_STDOUT):
    """Cooperate with the owned leader, then force only its group at the reserve."""
    if (sys.platform != 'linux' or not all(hasattr(os, name) for name in
            ('waitid', 'P_PID', 'WEXITED', 'WNOHANG', 'WNOWAIT'))):
        raise _Unsupported()
    try:
        if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
            raise _Unsupported()
        _require_native_sigchld_default()
        _proc_identity(os.getpid())
    except (OSError, ValueError, IndexError):
        raise _Unsupported() from None
    if not callable(_validate_output) or type(_max_stdout) is not int or not 1 <= _max_stdout <= 8*1024*1024:
        raise _Unsupported()
    work_at, term_at, force_at = _bounds or _schedule(end, time.monotonic())
    process = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    identity = None; reaped = False; output = bytearray(); offset = 0
    failure = None; term_sent = False
    try:
        identity = _leader_identity(process)
        if process.stdin is None or process.stdout is None:
            raise _CleanupFailed()
        os.set_blocking(process.stdin.fileno(), False); os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
            selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
            while True:
                now = time.monotonic()
                status = _verify_leader(process, identity)
                if status is not None and not _group_has_live_members(identity[1], ignore_pid=process.pid):
                    # Drain already-buffered output after all owned writers stop.
                    if process.stdout and not process.stdout.closed:
                        while True:
                            try: block = os.read(process.stdout.fileno(), 4096)
                            except BlockingIOError: raise _CleanupFailed() from None
                            if not block: break
                            output.extend(block)
                            if len(output) > _max_stdout: failure = _OutputInvalid()
                    process.wait(timeout=max(0.001, end - time.monotonic())); reaped = True
                    if now >= force_at or time.monotonic() >= end:
                        raise _CleanupFailed()
                    if failure is not None: raise failure
                    if process.returncode != 0: raise _Unsupported()
                    return bytes(output)
                action = _control_action(now, term_at, force_at, term_sent, failure is not None)
                if action == 'force':
                    _stop_group(process, identity, end)
                if action == 'term':
                    term_sent = True
                    if status is None:
                        _verify_leader(process, identity)
                        os.kill(process.pid, signal.SIGTERM)
                    if failure is not None:
                        force_at = min(force_at, now + 13.0)
                if now >= end:
                    raise _CleanupFailed()
                timeout = min(0.05, max(0, min(force_at, end) - now))
                for event, _ in selector.select(timeout):
                    if event.data == 'stdin':
                        try:
                            count = os.write(process.stdin.fileno(), payload[offset:offset + 16384]); offset += count
                        except (BrokenPipeError, OSError):
                            count = 0
                            if failure is None: failure = _Unsupported()
                        if offset == len(payload) or count == 0:
                            selector.unregister(process.stdin); process.stdin.close()
                    else:
                        block = os.read(process.stdout.fileno(), 4096)
                        if not block:
                            selector.unregister(process.stdout); process.stdout.close()
                            if not _validate_output(bytes(output)) and failure is None:
                                failure = _OutputInvalid()
                        elif len(output) + len(block) > _max_stdout:
                            if failure is None: failure = _OutputInvalid()
                        else:
                            output.extend(block)
    except BaseException:
        if identity is not None and not reaped:
            # Unexpected host errors still give the verified leader its own grace;
            # forcing before force_at would interrupt native owner cleanup.
            try:
                status = _verify_leader(process, identity)
                if status is None and not term_sent:
                    _verify_leader(process, identity)
                    os.kill(process.pid, signal.SIGTERM)
                force_at = min(force_at, time.monotonic() + 13.0)
                while time.monotonic() < force_at:
                    status = _verify_leader(process, identity)
                    if status is not None and not _group_has_live_members(identity[1], ignore_pid=process.pid):
                        process.wait(timeout=max(0.001, end - time.monotonic())); reaped = True
                        break
                    time.sleep(min(0.05, max(0, force_at - time.monotonic())))
                if not reaped: _stop_group(process, identity, end)
            except _CleanupFailed:
                raise
        raise
    finally:
        if process.stdin and not process.stdin.closed: process.stdin.close()
        if process.stdout and not process.stdout.closed: process.stdout.close()


def classify(cfg: TrustedHostConfig, source: SelectionInput, options: Shortlist, *, exchange=_exchange) -> BridgeResult:
    """Run only the operator-pinned bridge, returning raw decision for strict validation.

    The host must durably reserve a decision before this call and separately run
    `decide`, revalidate authority and admit any delivery after this call.
    """
    if not isinstance(cfg, TrustedHostConfig) or type(cfg.wall_timeout_ms) is not int or not 3000 <= cfg.wall_timeout_ms <= 120000:
        return BridgeResult('deferred', error_code='classifier_input_invalid')
    end = time.monotonic() + cfg.wall_timeout_ms / 1000
    try:
        payload = model_input(source, options)
        root, node, adapter = _preflight(cfg, end)
        now = time.monotonic()
        work_at, _, _ = _schedule(end, now)
        remaining = int((end - now) * 1000)
        if work_at - now < 1:
            raise _Expired()
        host = {'buildRoot': str(root), 'expectedCommit': cfg.expected_commit,
                'manifestPath': str(_path(cfg.manifest_path)), 'manifestSha256': cfg.manifest_sha256,
                'configPath': str(_path(cfg.config_path)), 'configSha256': cfg.config_sha256,
                'policyPath': str(_path(cfg.policy_path)), 'policySha256': cfg.policy_sha256,
                'agentId': cfg.agent_id, 'wallTimeoutMs': remaining}
        data = _serialize_envelope(host, payload)
        if data is None:
            return BridgeResult('deferred', error_code='classifier_input_invalid')
        env = {k: os.environ[k] for k in ('HOME', 'PATH', 'LANG', 'LC_ALL', 'TMPDIR') if k in os.environ}
        env['OPENCLAW_CONFIG_PATH'] = host['configPath']
        env['OPENCLAW_MAIL_WORK_DEADLINE_MONOTONIC_MS'] = str(int(work_at * 1000))
        raw = exchange([str(node), '--import', str(root / 'scripts/tsx.mjs'), str(adapter)], root, env, data, end)
        if not isinstance(raw, bytes) or len(raw) > _MAX_STDOUT:
            return BridgeResult('deferred', error_code='classifier_output_invalid')
        try:
            output = _json(raw)
        except (ValueError, TypeError, RecursionError):
            return BridgeResult('deferred', error_code='classifier_output_invalid')
        if not isinstance(output, dict) or type(output.get('version')) is not int or output.get('version') != 1:
            return BridgeResult('deferred', error_code='classifier_output_invalid')
        if set(output) == {'version', 'status', 'decisionRaw'} and output['status'] == 'ok':
            if time.monotonic() >= work_at:
                return BridgeResult('deferred', error_code='classifier_timeout')
            value = output['decisionRaw']
            try:
                valid_value = isinstance(value, str) and len(value.encode('utf-8')) <= 4096
            except UnicodeEncodeError:
                valid_value = False
            if not valid_value:
                return BridgeResult('deferred', error_code='classifier_output_invalid')
            # Pin the caller-owned files after cleanup; ACL/catalog recheck is separate.
            for path, expected in ((node, cfg.node_sha256), (adapter, cfg.adapter_sha256),
                                   (_path(cfg.manifest_path), cfg.manifest_sha256),
                                   (_path(cfg.config_path), cfg.config_sha256), (_path(cfg.policy_path), cfg.policy_sha256)):
                if _hash_file(path, end)[0] != expected: raise _Changed()
            return BridgeResult('ok', decision_raw=value)
        if set(output) == {'version', 'status', 'errorCode'} and output['status'] == 'deferred' and isinstance(output['errorCode'], str) and output['errorCode'] in _CODES:
            return BridgeResult('deferred', error_code=output['errorCode'])
        return BridgeResult('deferred', error_code='classifier_output_invalid')
    except _Expired:
        return BridgeResult('deferred', error_code='classifier_timeout')
    except _Changed:
        return BridgeResult('deferred', error_code='classifier_source_changed')
    except _Unsupported:
        return BridgeResult('deferred', error_code='classifier_runtime_unsupported')
    except _CleanupFailed:
        return BridgeResult('deferred', error_code='classifier_cleanup_failed')
    except _OutputInvalid:
        return BridgeResult('deferred', error_code='classifier_output_invalid')
    except (OSError, ValueError, TypeError, KeyError, RecursionError, subprocess.SubprocessError):
        return BridgeResult('deferred', error_code='classifier_runtime_unsupported')
