"""SRV-831: other ways to connect an engine host: password once, paste a key
(proxy/engineonboard.py), against fakes.

The engine host is tests/onboard_rig.py's: tests/fake_ssh.py (sshd and OpenSSH: the host key a
login must pin, a password through SSH_ASKPASS, a key's forced command) over a home of its own,
and tests/fake_tandem_remote.py as `tandem remote`. The install script runs for real, with bash,
in that home.

  Gherkin (SRV-831, plan 13 item 9):
  1  Given "Other ways", When the user gives a user name and password, Then the key is installed
     (one forced-command line with --key-id), the session closed (one login, no master kept) and
     the password kept nowhere; the host key is shown first and must come back confirmed; the
     token comes from `token rotate` over the new login, sealed; the node is kept with status
                                                                  test_password_once
  2  the password path's refusals: a wrong password, no install there (the folder given then
     works), an unsafe ~/.ssh, a host key that changed before the login or that the fleet pins
     otherwise for that address, no SSH there; nothing is kept in any of them
                                                                  test_password_refusals
  3  When the user chooses "paste a key", Then the page shows the authorized_keys line (the
     engine's key_line, the install's folder, from= when asked) And waits for the first login
     (`waiting` with the key_not_installed finding until the line is there), then pairs; a line
     that names the wrong folder says so                          test_paste_a_key
  4  no password, private key or HTTP token in any log line, answer, error, audit row, fleet
     record, fake ssh argv or record, the controller's files, or a temporary file left behind
                                                                  test_no_password_anywhere

    python tests/test_engine_connect.py
"""

from __future__ import annotations

import glob
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_ssh  # noqa: E402
import fake_tandem_remote as ftr  # noqa: E402
from onboard_rig import PASSWORD, ok, pem_lines, rig  # noqa: E402
from proxy import enginectl, hostkeys  # noqa: E402

PW = "/v1/fleet/engines/connect/password"
KEY = "/v1/fleet/engines/connect/key"


def _plan(r, **kw) -> dict:
    st, doc = r.post(f"{PW}/plan", dict({"host": "127.0.0.1", "user": "tandem"}, **kw))
    assert st == 200, doc
    return doc


def _nothing_kept(r) -> None:
    assert r.ssh_nodes() == [], r.ssh_nodes()
    hosts = os.path.join(r.fleet.trust().root, "ssh", "hosts")
    assert not os.path.isdir(hosts) or os.listdir(hosts) == []
    import sqlite3
    con = sqlite3.connect(r.cfg.path)
    assert con.execute("SELECT COUNT(*) FROM engine_host_keys").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM engine_http_tokens").fetchone()[0] == 0
    con.close()


def _runs(r) -> list[dict]:
    return [x for x in fake_ssh.log(r.sshlog) if x["tool"] == "ssh" and x.get("remote") is not None]


def test_password_once(tmp_path, monkeypatch):
    with rig(tmp_path, monkeypatch) as r:
        p = _plan(r, engine_dir=r.engine_dir, name="engine-pw")
        ok(p, "engine-connect-plan.schema.json")
        hk = p["host_key"]
        assert (hk["type"], hk["fingerprint"], hk["known"], hk["stored"]) == \
            (r.host_type, r.host_fp, "new", None)
        assert r.lines() == [] and _runs(r) == [], "the plan logs in nowhere"
        # not confirmed: nothing runs
        st, doc = r.post(f"{PW}/confirm", {"plan": p["plan"], "password": PASSWORD})
        assert st == 409 and doc["error"]["code"] == "host_key_unconfirmed", doc
        assert doc["error"]["offered"] == r.host_fp and _runs(r) == []
        st, doc = r.post(f"{PW}/confirm", {"plan": p["plan"], "password": PASSWORD,
                                           "host_key": r.host_fp})
        assert st == 200, doc
        ok(doc, "engine-paired.schema.json")
        assert doc["state"] == "paired" and doc["via"] == "password" and doc["finding"] is None
        hid = doc["engine"]["id"]
        # the key installed: one line, the forced command with --key-id
        lines = r.lines()
        assert len(lines) == 1 and lines[0] == hostkeys.key_line(
            r.cfg.host_key(hid)["public"], p["key_id"], os.path.realpath(r.engine_dir))
        mode = os.stat(os.path.dirname(r.ak)).st_mode & 0o777, os.stat(r.ak).st_mode & 0o777
        assert mode == (0o700, 0o600), oct(mode[0])
        # the session: one password login running the script, no master; every later run by key
        runs = _runs(r)
        pw = [x for x in runs if "PubkeyAuthentication=no" in x["argv"]]
        assert len(pw) == 1 and pw[0]["remote"] == "/bin/bash -s" and "ControlPath=none" in pw[0]["argv"]
        assert "SSH_ASKPASS" in pw[0]["env"]
        later = runs[runs.index(pw[0]) + 1:]
        assert later and all("PubkeyAuthentication=no" not in x["argv"] and "-i" in x["argv"]
                             for x in later)
        assert [c["tokens"] for c in r.calls()] == [["token", "rotate"], ["status"]]
        # the token sealed, the host key pinned, the node kept
        assert r.cfg.engine_token(hid) == ftr.TOKEN
        assert enginectl.pinned_key(r.fleet.trust().root, hid) == (r.host_type, r.host_b64)
        row = r.cfg.node(hid)
        assert row["alias"] == "engine-pw" and row["transport"] == "ssh"
        assert (row["trust"]["via"], row["trust"]["user"], row["trust"]["host_key"]) == \
            ("ssh", "tandem", r.host_fp)
        assert doc["engine"]["status"]["ok"] and doc["engine"]["http"]["token"]["set"]
        a = [x for x in r.cfg.audit(50) if x["action"] == "pair" and x["object_id"] == hid]
        assert a and a[0]["detail"]["method"] == "password"
        # the plan is used up; a second plan for the same host shows the key as known
        st, doc = r.post(f"{PW}/confirm", {"plan": p["plan"], "password": PASSWORD, "host_key": r.host_fp})
        assert st == 404 and doc["error"]["code"] == "not_found"
        assert _plan(r)["host_key"]["known"] == "same"
    return "host key confirmed, one password session, the line installed, token rotated, node kept"


def test_password_refusals(tmp_path, monkeypatch):
    with rig(tmp_path, monkeypatch) as r:
        # a wrong password
        p = _plan(r, engine_dir=r.engine_dir)
        st, doc = r.post(f"{PW}/confirm", {"plan": p["plan"], "password": "nope", "host_key": r.host_fp})
        assert st == 403 and doc["error"]["code"] == "auth_failed", doc
        assert r.lines() == []
        # nothing installed in the default folder; the folder given works afterwards
        p = _plan(r)
        st, doc = r.post(f"{PW}/confirm", {"plan": p["plan"], "password": PASSWORD, "host_key": r.host_fp})
        assert st == 409 and doc["error"]["code"] == "not_installed", doc
        assert doc["error"]["dir"].endswith("/TandemLLM") and "Install the engine" in doc["error"]["fix"]
        assert r.lines() == []
        # an unsafe ~/.ssh: writable by others
        os.makedirs(os.path.dirname(r.ak), mode=0o700, exist_ok=True)
        os.chmod(os.path.dirname(r.ak), 0o777)
        p = _plan(r, engine_dir=r.engine_dir)
        st, doc = r.post(f"{PW}/confirm", {"plan": p["plan"], "password": PASSWORD, "host_key": r.host_fp})
        assert st == 409 and doc["error"]["code"] == "key_refused", doc
        assert "writable by group or others" in doc["error"]["detail"]
        os.chmod(os.path.dirname(r.ak), 0o700)
        assert r.lines() == []
        # a link
        os.symlink(os.path.join(r.tmp, "elsewhere"), r.ak)
        p = _plan(r, engine_dir=r.engine_dir)
        st, doc = r.post(f"{PW}/confirm", {"plan": p["plan"], "password": PASSWORD, "host_key": r.host_fp})
        assert st == 409 and doc["error"]["code"] == "key_refused", doc
        os.unlink(r.ak)
        # the host key changed between the plan and the login: the login refuses it
        p = _plan(r, engine_dir=r.engine_dir)
        _pem, other = hostkeys.generate()
        fake_ssh.server(r.server, host_key=other.split()[:2])
        st, doc = r.post(f"{PW}/confirm", {"plan": p["plan"], "password": PASSWORD, "host_key": r.host_fp})
        assert st == 409 and doc["error"]["code"] in ("host_key_changed", "host_key_mismatch"), doc
        assert r.lines() == []
        fake_ssh.server(r.server, host_key=[r.host_type, r.host_b64])
        # a confirmed key that is not the one shown
        p = _plan(r, engine_dir=r.engine_dir)
        st, doc = r.post(f"{PW}/confirm", {"plan": p["plan"], "password": PASSWORD,
                                           "host_key": "SHA256:" + "A" * 43})
        assert st == 409 and doc["error"]["code"] == "host_key_mismatch", doc
        _nothing_kept(r)
        # no SSH there
        fake_ssh.server(r.server, unreachable=True)
        st, doc = r.post(f"{PW}/plan", {"host": "127.0.0.1", "user": "tandem"})
        assert st == 502 and doc["error"]["code"] == "ssh_unreachable", doc
        assert doc["error"]["port"] == 22 and "port 22" in doc["error"]["message"]
        fake_ssh.server(r.server, unreachable=False)
        # bad bodies
        for body in ({"host": "127.0.0.1"}, {"host": "127.0.0.1", "user": "-x"},
                     {"host": "127.0.0.1", "user": "u", "engine_dir": "rel/dir"},
                     {"host": "127.0.0.1", "user": "u", "engine_dir": "/a b"},
                     {"host": "127.0.0.1", "user": "u", "engine_dir": "/x/../etc"}):
            st, doc = r.post(f"{PW}/plan", body)
            assert st == 400 and doc["error"]["code"] == "bad_request", (body, doc)
        p = _plan(r, engine_dir=r.engine_dir)
        for pw in ("", "two\nlines", 12):
            st, doc = r.post(f"{PW}/confirm", {"plan": p["plan"], "password": pw, "host_key": r.host_fp})
            assert st == 400 and doc["error"]["code"] == "bad_request", (pw, doc)
        _nothing_kept(r)
        # a fleet node that pins another key at that address: refused before any login
        _pem, other = hostkeys.generate()
        r.pair()                                     # pins the rig's host key at 127.0.0.1:22
        fake_ssh.server(r.server, host_key=other.split()[:2])
        st, doc = r.post(f"{PW}/plan", {"host": "127.0.0.1", "user": "tandem"})
        assert st == 409 and doc["error"]["code"] == "host_key_changed", doc
        assert doc["error"]["pinned"] == r.host_fp and doc["error"]["offered"] != r.host_fp
        assert "pair it again by code" in doc["error"]["fix"]
    return "wrong password, no install, unsafe ~/.ssh, changed host key, no SSH, bad bodies"


def test_paste_a_key(tmp_path, monkeypatch):
    with rig(tmp_path, monkeypatch) as r:
        st, doc = r.post(f"{KEY}/new", {"host": "127.0.0.1", "user": "tandem"})
        assert st == 200 and doc["engine_dir_guessed"] and doc["engine_dir"] == "/home/tandem/TandemLLM"
        st, doc = r.post(f"{KEY}/new", {"host": "127.0.0.1", "user": "tandem", "engine_dir": r.engine_dir,
                                        "name": "engine-key"})
        assert st == 200, doc
        ok(doc, "engine-connect-key.schema.json")
        assert not doc["engine_dir_guessed"] and doc["file"] == "~/.ssh/authorized_keys"
        line = doc["line"]
        assert line.startswith(f'command="/bin/bash {r.engine_dir}/bin/tandem remote --key-id {doc["key_id"]}",'
                               "no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 ")
        assert line.endswith(" " + doc["key_id"]) and "PRIVATE" not in line
        assert doc["host_key"]["fingerprint"] == r.host_fp and doc["host_key"]["known"] == "new"
        assert (doc["host"], doc["port"], doc["user"]) == ("127.0.0.1", 22, "tandem")
        # the page waits for the first login: the line is not there yet
        for _ in range(2):
            st, w = r.post(f"{KEY}/check", {"plan": doc["plan"], "host_key": r.host_fp})
            assert st == 200 and w["state"] == "waiting", w
            ok(w, "engine-paired.schema.json")
            f = ok(w["finding"], "engine-finding.schema.json")
            assert f["code"] == "key_not_installed" and "tandem on 127.0.0.1" in f["fix"]
            _nothing_kept(r)
        # pasted: not confirmed, then paired
        os.makedirs(os.path.dirname(r.ak), mode=0o700, exist_ok=True)
        with open(r.ak, "a") as fh:
            fh.write(line + "\n")
        os.chmod(r.ak, 0o600)
        st, e = r.post(f"{KEY}/check", {"plan": doc["plan"]})
        assert st == 409 and e["error"]["code"] == "host_key_unconfirmed", e
        st, done = r.post(f"{KEY}/check", {"plan": doc["plan"], "host_key": r.host_fp})
        assert st == 200 and done["state"] == "paired" and done["via"] == "key", done
        ok(done, "engine-paired.schema.json")
        hid = done["engine"]["id"]
        assert r.cfg.engine_token(hid) == ftr.TOKEN and r.cfg.node(hid)["alias"] == "engine-key"
        assert enginectl.pinned_key(r.fleet.trust().root, hid) == (r.host_type, r.host_b64)
        assert done["engine"]["status"]["ok"]
        assert not [x for x in _runs(r) if "PubkeyAuthentication=no" in x["argv"]], "no password"
        # from= narrows the line; a bad one is refused
        st, d2 = r.post(f"{KEY}/new", {"host": "127.0.0.1", "user": "tandem", "engine_dir": r.engine_dir,
                                       "from": "192.0.2.0/24"})
        assert st == 200 and ',from="192.0.2.0/24" ssh-ed25519 ' in d2["line"], d2["line"]
        st, e = r.post(f"{KEY}/new", {"host": "127.0.0.1", "user": "tandem", "engine_dir": r.engine_dir,
                                      "from": "a b"})
        assert st == 400 and e["error"]["code"] == "bad_request"
        # a line that names a folder with no engine: the login works, tandem remote does not
        st, d3 = r.post(f"{KEY}/new", {"host": "127.0.0.1", "user": "tandem",
                                       "engine_dir": os.path.join(r.tmp, "no-engine")})
        with open(r.ak, "a") as fh:
            fh.write(d3["line"] + "\n")
        st, e = r.post(f"{KEY}/check", {"plan": d3["plan"], "host_key": r.host_fp})
        assert st == 409 and e["error"]["code"] == "not_installed", e
        assert e["error"]["dir"] == os.path.join(r.tmp, "no-engine")
        # no SSH there yet: the line still comes, with the finding
        fake_ssh.server(r.server, unreachable=True)
        st, d4 = r.post(f"{KEY}/new", {"host": "127.0.0.1", "user": "tandem", "engine_dir": r.engine_dir})
        assert st == 200 and d4["host_key"] is None and d4["finding"]["code"] == "ssh_unreachable", d4
        st, e = r.post(f"{KEY}/check", {"plan": d4["plan"], "host_key": r.host_fp})
        assert st == 502 and e["error"]["code"] == "ssh_unreachable", e
    return "the line shown, waiting until it is there, then paired; from=, a wrong folder"


def test_no_password_anywhere(tmp_path, monkeypatch):
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "tandem-ssh-*")))
    with rig(tmp_path, monkeypatch) as r:
        p = _plan(r, engine_dir=r.engine_dir)
        r.post(f"{PW}/confirm", {"plan": p["plan"], "password": "wrong-" + PASSWORD, "host_key": r.host_fp})
        p = _plan(r, engine_dir=r.engine_dir)
        st, doc = r.post(f"{PW}/confirm", {"plan": p["plan"], "password": PASSWORD, "host_key": r.host_fp})
        assert st == 200, doc
        hid = doc["engine"]["id"]
        st, k = r.post(f"{KEY}/new", {"host": "127.0.0.1", "user": "tandem", "engine_dir": r.engine_dir})
        pending = r.fleet.engines()._pending[k["plan"]]["pem"]
        r.get(f"/v1/fleet/engines/{hid}")
        token, pem = r.cfg.engine_token(hid), r.cfg.host_key_private(hid)
        seen = r.seen() + "\n" + r.db_rows()
        with open(r.server) as fh:
            seen += fh.read().replace(PASSWORD, "")      # the fake server's own config holds it
        for secret in (PASSWORD, "wrong-" + PASSWORD, token):
            assert secret not in seen, secret[:8]
        for line in pem_lines(pem) + pem_lines(pending):
            assert line not in seen
        raw = r.files()
        for secret in (PASSWORD, token):
            assert secret.encode() not in raw
        assert all(x.encode() not in raw for x in pem_lines(pem) + pem_lines(pending))
        for d, _, fs in os.walk(r.fleet.trust().root):
            for name in fs:
                path = os.path.join(d, name)
                if os.path.isfile(path) and not os.path.islink(path):
                    with open(path, "rb") as fh:
                        assert PASSWORD.encode() not in fh.read(), path
    left = set(glob.glob(os.path.join(tempfile.gettempdir(), "tandem-ssh-*"))) - before
    assert not left, f"temporary SSH folders left behind: {sorted(left)}"
    return "no password, key or token in texts, rows, files or temporary folders"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
