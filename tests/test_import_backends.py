"""`tandem-app import-backends` (ENG-413 WP14b, QA ENG-414; proxy/backends_import.py): an engine's
old relay, its QSE_BACKENDS config database, into the controller's. On a CPU, in temporary folders:
never this machine's own config.

The test matrix:

  1  the dry run: every backend it would import is listed with what goes with it, the users are
     named and not copied (TANDEM_API_USERS_FROM), and neither file changes (the old one's bytes
     and its folder, the controller's bytes); no key in the output     test_the_dry_run_writes_nothing
  2  --write: the backends as stored (kind, URL, label, the model list, timeouts, harness, host,
     on or off), each key readable under the controller's master key and not the old one's, the
     models with prices, public ids and switches, the routing rules after the controller's own, an
     id the controller has left as it is; the old file unchanged; a second run imports nothing
                                                            test_write_copies_and_a_second_run_keeps
  3  a backend the engine's environment declared is copied as a row and its variables are named;
     --disabled stores every imported backend off           test_env_backends_and_disabled
  4  a key under a master key not found here: the backend comes without it, named; the refusals: no
     file, the controller's own file, a file that is no config database (exit 1, nothing written)
                                                            test_unreadable_keys_and_refusals

Run: python tests/test_import_backends.py
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from proxy import config as C  # noqa: E402
from proxy import secrets as V  # noqa: E402

OLD_KEY = "sk-fixture-OLDRELAY7777"
LAN_KEY = "sk-fixture-LANBOX4242"


def _db(d: str, env: dict | None = None) -> C.ConfigDB:
    os.makedirs(d, exist_ok=True)
    return C.ConfigDB(os.path.join(d, "config.sqlite3"), env=env or {}, use_keychain=False).open()


def _old(root: str) -> str:
    """An engine's relay database as the engine wrote it: its own key file beside it."""
    d = os.path.join(root, "engine", "backends")
    old = _db(d)
    old.create_backend({"id": "deepseek", "kind": "deepseek", "label": "DeepSeek", "key": OLD_KEY,
                        "models_mode": "discover", "timeouts": {"first_byte": 900}})
    old.create_backend({"id": "lan", "kind": "generic", "url": "http://10.0.0.5:8000/v1", "key": LAN_KEY,
                        "models_mode": "list", "models": ["qwen-x"], "enabled": False,
                        "harness": {"mode": "shadow"}, "max_inflight": 4})
    old.sync_models("deepseek", [{"id": "deepseek-chat", "context_length": 131072}], [], "discover")
    old.update_model("deepseek", "deepseek-chat", {"public_id": "ds-chat", "price": {"input": 0.27, "output": 1.1},
                                                   "features": {"loops": "off"}})
    old.update_model("lan", "qwen-x", {"enabled": False}, create=True)
    old.set_rules([{"pattern": "ds-*", "backend": "deepseek"}, {"pattern": "qwen-*", "backend": "lan"}])
    old.create_user({"label": "laptop"})
    old.close()
    return os.path.join(d, "config.sqlite3")


def _ctl(root: str) -> str:
    """The controller's database: its own master key, the engine as a backend, one rule."""
    d = os.path.join(root, "ctl")
    c = _db(d)
    c.create_backend({"id": "engine", "kind": "tandemllm", "url": "http://127.0.0.1:8000/v1"})
    c.create_backend({"id": "lan", "kind": "vllm", "url": "http://10.0.0.9:8000/v1"})
    c.set_rules([{"pattern": "TandemLLM", "backend": "engine"}])
    c.close()
    return os.path.join(d, "config.sqlite3")


def _cli(*args, env=None):
    e = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tempfile.mkdtemp(prefix="imp-home-")}
    e.update(env or {})
    return subprocess.run([sys.executable, "-m", "proxy", "import-backends", "--no-keychain", *args], cwd=ROOT,
                          env=e, capture_output=True, text=True, timeout=120)


def _files(d: str) -> dict:
    out = {}
    for n in sorted(os.listdir(d)):
        p = os.path.join(d, n)
        with open(p, "rb") as fh:
            out[n] = (hashlib.sha256(fh.read()).hexdigest(), os.stat(p).st_mtime_ns)
    return out


def _sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def test_the_dry_run_writes_nothing():
    root = tempfile.mkdtemp(prefix="imp1-")
    src, dst = _old(root), _ctl(root)
    before_src, before_dst = _files(os.path.dirname(src)), _files(os.path.dirname(dst))
    r = _cli("--from", src, "--config-db", dst)
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "  import    deepseek: its key, under this controller's master key" in out, out
    assert "  kept      lan: the controller has a backend of this id" in out, out
    assert "models 1, conductor switches 1, routing rules 1" in out, out
    assert f"users 1: not copied" in out and f"TANDEM_API_USERS_FROM={src}" in out, out
    assert out.rstrip().endswith("nothing written; run again with --write"), out
    assert OLD_KEY not in out + r.stderr and LAN_KEY not in out + r.stderr
    assert _files(os.path.dirname(src)) == before_src, "the old folder changed"
    assert _files(os.path.dirname(dst)) == before_dst, "the controller's folder changed"


def test_write_copies_and_a_second_run_keeps():
    root = tempfile.mkdtemp(prefix="imp2-")
    src, dst = _old(root), _ctl(root)
    shutil.rmtree(os.path.join(root, "ctl"))
    c = _db(os.path.join(root, "ctl"))
    c.create_backend({"id": "engine", "kind": "tandemllm", "url": "http://127.0.0.1:8000/v1"})
    c.set_rules([{"pattern": "TandemLLM", "backend": "engine"}])
    c.close()
    old_folder = _files(os.path.dirname(src))
    r = _cli("--from", src, "--config-db", dst, "--write")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "  imported  deepseek" in r.stdout and "  imported  lan" in r.stdout, r.stdout
    assert "restart the controller" in r.stdout and OLD_KEY not in r.stdout + r.stderr
    assert _files(os.path.dirname(src)) == old_folder, "the old folder changed"
    c = C.ConfigDB(dst, env={}, use_keychain=False).open()
    try:
        ds, lan = c.backend("deepseek"), c.backend("lan")
        assert ds["kind"] == "deepseek" and ds["label"] == "DeepSeek" and ds["enabled"] is True, ds
        assert ds["timeouts"]["first_byte"] == 900 and ds["models_mode"] == "discover", ds
        assert lan["url"] == "http://10.0.0.5:8000/v1" and lan["enabled"] is False and lan["models"] == ["qwen-x"]
        assert lan["harness"]["mode"] == "shadow" and lan["max_inflight"] == 4, lan
        assert c.key_for("deepseek") == OLD_KEY and c.key_for("lan") == LAN_KEY
        # under the controller's master key: the old key file cannot read the copies
        old_v = V.Vault.load(os.path.dirname(src), env={}, use_keychain=False)
        kid = c._conn.execute("SELECT key_id FROM backend_secrets WHERE backend_id = 'deepseek'").fetchone()[0]
        assert kid == c.vault().key_id != old_v.key_id
        m = {(x["backend"], x["model"]): x for x in c.models()}
        dm = m[("deepseek", "deepseek-chat")]
        assert dm["public_id"] == "ds-chat" and dm["price"]["input"] == 0.27 and dm["price"]["output"] == 1.1, dm
        assert dm["context_length"] == 131072 and dm["source"] == "discovered" and dm["features"]["loops"] == "off"
        assert m[("lan", "qwen-x")]["enabled"] is False
        rules = [(x["pattern"], x["backend"]) for x in c.rules()]
        assert rules == [("TandemLLM", "engine"), ("ds-*", "deepseek"), ("qwen-*", "lan")], rules
        assert [u for u in c.users() if u["source"] == "database"] == [], "users are not copied"
        assert {a["who"] for a in c.audit(200) if a["object"] in ("backend", "models")} >= {"import"}
    finally:
        c.close()
    r = _cli("--from", src, "--config-db", dst, "--write")
    assert r.returncode == 0 and "  kept      deepseek" in r.stdout and "  kept      lan" in r.stdout, r.stdout
    assert "routing rules 0" in r.stdout and "restart the controller" not in r.stdout, r.stdout
    c = C.ConfigDB(dst, env={}, use_keychain=False).open()
    try:
        assert len(c.rules()) == 3 and len(c.models()) == 2, (c.rules(), c.models())
    finally:
        c.close()


def test_env_backends_and_disabled():
    root = tempfile.mkdtemp(prefix="imp3-")
    d = os.path.join(root, "engine")
    env = {"TANDEM_BACKENDS": "cloud-x", "TANDEM_BACKEND_CLOUD_X_KIND": "openai",
           "TANDEM_BACKEND_CLOUD_X_KEY": "sk-fixture-ENVONLY0000"}
    old = _db(d, env)
    old.apply_env()
    old.create_backend({"id": "deepseek", "kind": "deepseek", "key": OLD_KEY})
    old.close()
    src = os.path.join(d, "config.sqlite3")
    dst = os.path.join(root, "ctl", "config.sqlite3")
    r = _cli("--from", src, "--config-db", dst, "--write", "--disabled")
    assert r.returncode == 0, r.stdout + r.stderr
    line = next(ln for ln in r.stdout.splitlines() if ln.startswith("  imported  cloud-x"))
    assert "TANDEM_BACKEND_CLOUD_X_*" in line and "TANDEM_BACKEND_CLOUD_X_KEY is not in the file" in line, line
    assert "stored off (--disabled)" in next(ln for ln in r.stdout.splitlines() if "imported  deepseek" in ln)
    assert "sk-fixture-ENVONLY0000" not in r.stdout + r.stderr
    c = C.ConfigDB(dst, env={}, use_keychain=False).open()
    try:
        assert {b["id"]: b["enabled"] for b in c.backends()} == {"cloud-x": False, "deepseek": False}
        assert c.key_for("cloud-x") is None and c.key_for("deepseek") == OLD_KEY
        assert c.backend_locks("cloud-x") == {}, "a row here, unlocked: the variables are the controller's to set"
    finally:
        c.close()


def test_unreadable_keys_and_refusals():
    root = tempfile.mkdtemp(prefix="imp4-")
    src = _old(root)
    lone = os.path.join(root, "lone")
    os.makedirs(lone)
    shutil.copy(src, os.path.join(lone, "config.sqlite3"))            # without its key file
    dst = os.path.join(root, "ctl", "config.sqlite3")
    r = _cli("--from", os.path.join(lone, "config.sqlite3"), "--config-db", dst, "--write")
    assert r.returncode == 0, r.stdout + r.stderr
    line = next(ln for ln in r.stdout.splitlines() if ln.startswith("  imported  deepseek"))
    assert "no key copied: its key is under a master key not found here" in line, line
    assert sorted(os.listdir(lone)) == ["config.sqlite3"], "nothing written beside the old file"
    c = C.ConfigDB(dst, env={}, use_keychain=False).open()
    try:
        assert c.backend("deepseek") is not None and c.key_for("deepseek") is None
    finally:
        c.close()
    # with the old master key in the environment it reads them
    key = open(os.path.join(os.path.dirname(src), V.KEY_FILE)).read().strip()
    dst2 = os.path.join(root, "ctl2", "config.sqlite3")
    r = _cli("--from", os.path.join(lone, "config.sqlite3"), "--config-db", dst2, "--write",
             env={V.ENV: key})
    assert r.returncode == 0 and "  imported  deepseek: its key" in r.stdout, r.stdout + r.stderr
    # the refusals
    r = _cli("--from", os.path.join(root, "nowhere.sqlite3"), "--config-db", dst)
    assert r.returncode == 1 and "FAIL no file" in r.stdout, r.stdout
    r = _cli("--from", dst, "--config-db", dst)
    assert r.returncode == 1 and "this controller's own config database" in r.stdout, r.stdout
    junk = os.path.join(root, "junk.sqlite3")
    with open(junk, "w") as fh:
        fh.write("not a database\n")
    r = _cli("--from", junk, "--config-db", dst)
    assert r.returncode == 1 and "FAIL" in r.stdout, r.stdout
    other = os.path.join(root, "other.sqlite3")
    con = sqlite3.connect(other)
    con.execute("CREATE TABLE t (x)")
    con.commit()
    con.close()
    before = _sha(dst)
    r = _cli("--from", other, "--config-db", dst, "--write")
    assert r.returncode == 1 and "is not a TandemApp config database" in r.stdout, r.stdout
    assert _sha(dst) == before


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  {name:50s} ok")
            passed += 1
    print(f"{passed} passed")
