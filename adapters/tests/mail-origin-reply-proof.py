"""Read an actual adapter-created ledger row. Never manually registers origin."""
import json
from pathlib import Path
import sys
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools/tests"), str(ROOT / "tools")]
import test_agent_mail_router as base
import agent_mail_origin
from test_agent_mail_external import ext
from unittest.mock import Mock

def no_network(event, args):
    if event.startswith("socket.") or event in ("subprocess.Popen", "os.system", "os.posix_spawn"):
        raise AssertionError("network/subprocess forbidden")
sys.addaudithook(no_network)
path, mid = Path(sys.argv[1]), sys.argv[2]
store = base.m.Store(path, {})
store.bootstrap({"cursor": {"uidvalidity": "77", "last_uid": 0}, "retained": True})
message = ext(1, reply=mid, body="Synthetic reply only")
store.ingest(base.snap([message]))
assert store.batch() is None
assert store.db.execute("SELECT reason FROM mail WHERE key='77:1'").fetchone()[0] == agent_mail_origin.REASON
gw, tg = Mock(), Mock()
gw.call.return_value = {"ok": True, "messageId": "synthetic-context-receipt"}
assert store.dispatch_one(gw, tg)["status"] == "sent"
method, args = gw.call.call_args.args
stored = json.loads(store.db.execute("SELECT origin FROM mail_outbound_v1 WHERE mid=?", (mid,)).fetchone()[0])
assert method == "chat.inject"
assert (args["sessionKey"], args["sessionId"]) == (stored["sessionKey"], stored["sessionId"])
assert args["sessionId"] == "synthetic-session-generation"
tg.send.assert_not_called()
store.dispatch_one(gw, tg)
assert gw.call.call_count == 1
store.db.close()
print("PASS synthetic reply correlated from actual runtime adapter row before transport completion; exact sessionId; report-only; no replay")
