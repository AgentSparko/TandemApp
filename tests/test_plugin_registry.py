"""SRV-857 (QA SRV-858): the plugin installer and registry (proxy/pluginstore.py over
proxy/pluginsource.py and proxy/pluginmanifest.py) in a temporary config database.

  Gherkin and the acceptance criteria:
  1  Valid plugin: Given a folder with a valid plugin.json and an MCP server, When it is installed,
     Then the sha256 of its tree is recorded, compatibility is checked and the plugin appears
     disabled, its tree under <state>/plugins/<id>/<version>, audited; an inspect installs
     nothing                                                   test_folder_install
  2  A wheel installs (its manifest at the root or in one top-level folder, RECORD's hashes
     checked, the archive's sha256 recorded); a member that leaves the tree, a symlink, a wrong
     RECORD hash, no manifest or two are refused                test_wheel_install
  3  A git URL at a commit installs that commit's tree even after the branch moved; a branch or tag
     name, a short id, a commit the repository does not have, another transport are refused; a
     subdir; a submodule or an LFS file refuses unless the manifest declares it
                                                               test_git_install
  4  No code runs at install: an entry, a setup.py, a .pth, a git hook that would write a file
     write nothing, and the only process an install starts is git
                                                               test_no_code_runs
  5  The tree's sha256 is checked on load: a changed or missing tree reads mismatch or missing,
     load and enable refuse it; put back, it loads again      test_integrity_on_load
  6  Enable, disable and remove work; removal is blocked while a hook names the plugin (409
     referenced with the hooks), and takes its folder and secrets with it
                                                               test_enable_disable_remove
  7  Upgrade keeps the settings the new schema allows, the secrets still declared, the grants
     still requested, and enabled; drops and lists the rest; removes the old version's folder;
     the same version with another tree, an older version and a second plugin of an installed
     name are refused                                          test_upgrade
  8  An install cut short leaves nothing behind: a failed write leaves no row and no folder, and
     the next start removes staging and orphan folders         test_interrupted_install
  9  Incompatible version: refused at install with the needed version; a plugin that no longer fits
     this TandemApp is refused at enable and load               test_compatibility_at_install_and_enable

Run: python tests/test_plugin_registry.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from plugin_util import (Reg, bumped, commit, example, example_files, folder, git,  # noqa: E402
                         repo, wheel, write_tree)

from proxy import pluginmanifest as PM  # noqa: E402
from proxy.pluginstore import PluginError  # noqa: E402


def refused(fn, status: int, code: str):
    try:
        fn()
    except PluginError as exc:
        assert (exc.status, exc.code) == (status, code), (exc.status, exc.code, exc.message)
        return exc
    raise AssertionError(f"not refused: wanted {status} {code}")


# ---------------------------------------------------------------------------- 1
def test_folder_install():
    r = Reg("folder")
    src = folder()
    try:
        rep = r.store.inspect({"type": "folder", "path": src})
        assert rep["ok"] and rep["manifest"]["name"] == "hello-tool", rep["errors"]
        assert rep["settings_form"]["fields"][0]["key"] == "greeting"
        assert r.store.list() == [] and not os.listdir(r.store._staging())
        p, note = r.install(src)
        assert note["action"] == "installed" and note["dropped"] == []
        assert p["sha256"] == PM.tree_digest(src) and len(p["sha256"]) == 64
        assert p["enabled"] is False and p["integrity"] == "ok" and p["kind"] == "plugin"
        assert p["version"] == "0.1.0" and p["source"] == {"type": "folder", "path": src}
        assert os.path.realpath(p["path"]) == os.path.realpath(
            os.path.join(r.dir, "plugins", "hello-tool", "0.1.0")), (p["path"], r.dir)
        assert sorted(os.listdir(p["path"])) == ["plugin.json", "server.py"]
        assert p["installed_by"] == "admin" and p["granted"] == {} and p["settings"] == {}
        assert r.store.effective_settings(p) == {"greeting": "Hello", "excited": False}
        assert not os.listdir(r.store._staging())
        rows = [a for a in r.audit() if a["object"] == "plugin"]
        assert [a["action"] for a in rows] == ["install"] and rows[0]["object_id"] == "hello-tool"
        # compatibility was checked: a range this TandemApp misses refuses
        m = example("hello-tool")
        m["tandemapp"] = ">=0.16"
        bad = folder(manifest=m)
        exc = refused(lambda: r.install(bad), 422, "plugin_refused")
        assert exc.extra["errors"][0]["code"] == "incompatible_tandemapp"
        shutil.rmtree(bad)
    finally:
        r.close()
        shutil.rmtree(src)
    return "sha256 recorded, compatibility checked, disabled, audited"


# ---------------------------------------------------------------------------- 2
def test_wheel_install():
    r = Reg("wheel")
    d = tempfile.mkdtemp(prefix="plugin-wheels-")
    try:
        files = example_files("hello-tool", "hello_tool")
        files["hello_tool/__init__.py"] = ""
        w = wheel(os.path.join(d, "hello_tool-0.1.0-py3-none-any.whl"), files)
        p, note = r.store.install({"type": "wheel", "path": w}, who="admin")
        assert note["action"] == "installed" and p["manifest_dir"] == "hello_tool", p
        assert p["source"]["type"] == "wheel" and p["source"]["archive_sha256"] == \
            PM.file_sha256(w)
        assert os.path.isfile(os.path.join(p["path"], "hello_tool", "server.py"))
        assert p["sha256"] == PM.tree_digest(p["path"])
        r.store.remove("hello-tool", "admin")
        # the manifest at the wheel's root
        w2 = wheel(os.path.join(d, "root.whl"), example_files("hello-tool"))
        p, _ = r.store.install({"type": "wheel", "path": w2}, who="admin")
        assert p["manifest_dir"] == ""
        r.store.remove("hello-tool", "admin")
        cases = {
            "leaves": (wheel(os.path.join(d, "a.whl"), dict(files, **{"../evil.py": "x"})),
                       "bad_wheel"),
            "symlink": (wheel(os.path.join(d, "b.whl"), files, symlink="hello_tool/link"),
                        "symlink"),
            "record": (wheel(os.path.join(d, "c.whl"), files, bad_hash="hello_tool/server.py"),
                       "bad_wheel"),
            "none": (wheel(os.path.join(d, "e.whl"), {"x/a.py": ""}), "manifest_missing"),
            "two": (wheel(os.path.join(d, "f.whl"), dict(files, **{"other/plugin.json": "{}"})),
                    "manifest_missing"),
            "not a zip": (write_tree(d, {"g.whl": "not a zip"}) and os.path.join(d, "g.whl"),
                          "bad_wheel"),
        }
        for why, (path, code) in cases.items():
            refused(lambda: r.store.install({"type": "wheel", "path": path}), 422, code)
        refused(lambda: r.store.install({"type": "wheel", "path": os.path.join(d, "x.zip")}),
                422, "bad_source")
        refused(lambda: r.store.install({"type": "wheel", "path": "relative.whl"}), 422,
                "bad_source")
        assert r.store.list() == [] and not os.listdir(r.store._staging())
        assert not [n for n in os.listdir(r.store.root()) if not n.startswith(".")]
    finally:
        r.close()
        shutil.rmtree(d)
    return f"wheel installs; {len(cases)} bad wheels refused"


# ---------------------------------------------------------------------------- 3
def test_git_install():
    r = Reg("git")
    files = example_files("hello-tool")
    d, c1 = repo(files)
    try:
        m2 = bumped("hello-tool", "0.2.0")
        c2 = commit(d, {"plugin.json": json.dumps(m2), "server.py": "# moved on\n"})
        p, note = r.store.install({"type": "git", "url": d, "commit": c1}, who="admin")
        assert p["version"] == "0.1.0" and p["source"] == {"type": "git", "url": d, "commit": c1}
        with open(os.path.join(p["path"], "server.py"), "rb") as f:
            assert f.read() == files["server.py"], "the tree is the pinned commit's"
        r.store.remove("hello-tool", "admin")
        p, _ = r.store.install({"type": "git", "url": "file://" + d, "commit": c2}, who="admin")
        assert p["version"] == "0.2.0"
        r.store.remove("hello-tool", "admin")
        for bad in ("main", "HEAD", c1[:12], "v0.1.0", c1.upper(), c1 + "0"):
            exc = refused(lambda: r.store.install({"type": "git", "url": d, "commit": bad}), 422,
                          "commit_required")
            assert "branch" in exc.message and exc.field == "source.commit"
        refused(lambda: r.store.install({"type": "git", "url": d}), 422, "commit_required")
        refused(lambda: r.store.install({"type": "git", "url": d, "commit": "a" * 40}), 422,
                "commit_not_found")
        for url in ("ext::sh -c touch% /tmp/x", "-uhelp", "git://example.invalid/r",
                    "http://example.invalid/r.git", "fd::17"):
            refused(lambda: r.store.install({"type": "git", "url": url, "commit": c1}), 422,
                    "bad_source")
        # a plugin in a subfolder of a repository
        mono, cm = repo({"README.md": "monorepo\n", **{f"plugins/hello/{k}": v
                                                       for k, v in files.items()}})
        p, _ = r.store.install({"type": "git", "url": mono, "commit": cm,
                                "subdir": "plugins/hello"}, who="admin")
        assert sorted(os.listdir(p["path"])) == ["plugin.json", "server.py"]
        r.store.remove("hello-tool", "admin")
        refused(lambda: r.store.install({"type": "git", "url": mono, "commit": cm,
                                         "subdir": "../etc"}), 422, "bad_source")
        # a submodule (a gitlink) refuses unless declared
        git(mono, "update-index", "--add", "--cacheinfo", f"160000,{c1},plugins/hello/vendor")
        git(mono, "commit", "-q", "-m", "submodule")
        cs = git(mono, "rev-parse", "HEAD")
        exc = refused(lambda: r.store.install({"type": "git", "url": mono, "commit": cs,
                                               "subdir": "plugins/hello"}), 422, "submodule")
        assert "vendor" in exc.message
        m = example("hello-tool")
        m["git"] = {"submodules": "ignore"}
        write_tree(mono, {"plugins/hello/plugin.json": json.dumps(m)})
        git(mono, "add", "plugins/hello/plugin.json")          # the gitlink stays in the index
        git(mono, "commit", "-q", "-m", "declare")
        cs2 = git(mono, "rev-parse", "HEAD")
        p, note = r.store.install({"type": "git", "url": mono, "commit": cs2,
                                   "subdir": "plugins/hello"}, who="admin")
        assert any("submodule vendor" in w for w in note["warnings"]), note
        r.store.remove("hello-tool", "admin")
        # an LFS pointer refuses unless declared
        lfs, cl = repo(dict(files, **{"model.bin": "version https://git-lfs.github.com/spec/v1\n"
                                                   "oid sha256:" + "0" * 64 + "\nsize 9\n"}))
        refused(lambda: r.store.install({"type": "git", "url": lfs, "commit": cl}), 422, "lfs")
        lfs2, cl2 = repo(dict(files, **{".gitattributes": "*.bin filter=lfs diff=lfs\n",
                                        "w.bin": "plain bytes"}))
        refused(lambda: r.store.install({"type": "git", "url": lfs2, "commit": cl2}), 422, "lfs")
        m = example("hello-tool")
        m["git"] = {"lfs": "ignore"}
        cl3 = commit(lfs, {"plugin.json": json.dumps(m)})
        p, note = r.store.install({"type": "git", "url": lfs, "commit": cl3}, who="admin")
        assert any("lfs model.bin" in w for w in note["warnings"])
        for x in (mono, lfs, lfs2):
            shutil.rmtree(x)
    finally:
        r.close()
        shutil.rmtree(d)
    return "pinned commit installs; branch, short id, unknown commit, other transports refused"


# ---------------------------------------------------------------------------- 4
def test_no_code_runs():
    r = Reg("nocode")
    marks = tempfile.mkdtemp(prefix="plugin-marks-")
    mark = os.path.join(marks, "ran")
    writer = f"open({mark!r}, 'w').write('ran')\n"
    m = example("hello-tool")
    m["entry"] = {"command": ["python3", "-c", writer]}
    extra = {"setup.py": writer, "zz_plugin.pth": f"import os; {writer}",
             "sitecustomize.py": writer, "conftest.py": writer}
    src = folder(manifest=m, extra=extra)
    d = tempfile.mkdtemp(prefix="plugin-wheels-")
    started: list = []
    real = subprocess.Popen.__init__

    def watch(self, args, *a, **kw):
        started.append(list(args) if isinstance(args, (list, tuple)) else [args])
        return real(self, args, *a, **kw)
    try:
        subprocess.Popen.__init__ = watch
        r.store.inspect({"type": "folder", "path": src})
        r.install(src)
        r.store.remove("hello-tool", "admin")
        files = {k: v for k, v in example_files("hello-tool").items() if k != "plugin.json"}
        files.update({"plugin.json": json.dumps(m), **extra})
        w = wheel(os.path.join(d, "w.whl"), files)
        r.store.install({"type": "wheel", "path": w}, who="admin")
        r.store.remove("hello-tool", "admin")
        gd, gc = repo(files)
        with open(os.path.join(gd, ".git", "hooks", "post-checkout"), "w") as f:
            f.write(f"#!/bin/sh\ntouch {mark}\n")
        os.chmod(os.path.join(gd, ".git", "hooks", "post-checkout"), 0o755)
        with open(os.path.join(gd, ".git", "hooks", "post-merge"), "w") as f:
            f.write(f"#!/bin/sh\ntouch {mark}\n")
        os.chmod(os.path.join(gd, ".git", "hooks", "post-merge"), 0o755)
        started.clear()
        r.store.install({"type": "git", "url": gd, "commit": gc}, who="admin")
        r.store.enable_check("hello-tool")
        assert started and all(a[0] == "git" for a in started), started
        assert not any("-c" in a[a.index("git") + 1:] and writer in " ".join(a) for a in started)
        shutil.rmtree(gd)
    finally:
        subprocess.Popen.__init__ = real
        r.close()
        shutil.rmtree(src)
        shutil.rmtree(d)
    assert not os.path.exists(mark), "a file of the plugin ran at install"
    shutil.rmtree(marks)
    return f"folder, wheel and git: nothing ran; {len(started)} git calls only"


# ---------------------------------------------------------------------------- 5
def test_integrity_on_load():
    r = Reg("integrity")
    src = folder()
    try:
        p, _ = r.install(src)
        r.store.set_enabled("hello-tool", True, "admin")
        got = r.store.load("hello-tool")
        assert got["settings"] == {"greeting": "Hello", "excited": False} and got["granted"] == []
        server = os.path.join(p["path"], "server.py")
        with open(server) as f:
            orig = f.read()
        with open(server, "a") as f:
            f.write("# changed after the install\n")
        assert r.store.verify("hello-tool")["integrity"] == "mismatch"
        refused(lambda: r.store.load("hello-tool"), 409, "integrity")
        r.store.set_enabled("hello-tool", False, "admin")
        refused(lambda: r.store.set_enabled("hello-tool", True, "admin"), 409, "integrity")
        assert r.store.get("hello-tool")["integrity"] == "mismatch"
        assert any("mismatch" in m for m in r.logs)
        with open(server, "w") as f:
            f.write(orig)
        os.chmod(server, 0o755)
        assert r.store.verify("hello-tool")["integrity"] == "mismatch", "a mode bit counts"
        os.chmod(server, 0o644)
        r.store.set_enabled("hello-tool", True, "admin")
        assert r.store.load("hello-tool")["version"] == "0.1.0"
        os.rename(p["path"], p["path"] + ".gone")
        assert r.store.verify("hello-tool")["integrity"] == "missing"
        refused(lambda: r.store.load("hello-tool"), 409, "integrity")
        os.rename(p["path"] + ".gone", p["path"])
        r.store.set_enabled("hello-tool", False, "admin")
        refused(lambda: r.store.load("hello-tool"), 409, "disabled")
    finally:
        r.close()
        shutil.rmtree(src)
    return "changed, mode-changed and missing trees refused on load and enable"


# ---------------------------------------------------------------------------- 6
def test_enable_disable_remove():
    r = Reg("lifecycle")
    src = folder()
    try:
        r.install(src)
        assert r.store.set_enabled("hello-tool", True, "admin")["enabled"] is True
        assert r.store.set_enabled("hello-tool", True, "admin")["enabled"] is True
        assert r.store.set_enabled("hello-tool", False, "admin")["enabled"] is False
        r.store.write("hello-tool", settings_set={"greeting": "Hi"}, who="admin")
        r.refer("uses-greet", "hello-tool/greet")
        r.refer("other", "hello-toolbox/x")                     # another plugin's name prefix
        exc = refused(lambda: r.store.remove("hello-tool", "admin"), 409, "referenced")
        assert [h["id"] for h in exc.extra["hooks"]] == ["uses-greet"], exc.extra
        assert "uses-greet" in exc.message
        assert os.path.isdir(os.path.join(r.store.root(), "hello-tool"))
        r.runner.store.delete("uses-greet", "admin")
        r.refer("mcp-hook", "hello-tool", kind="mcp")
        refused(lambda: r.store.remove("hello-tool", "admin"), 409, "referenced")
        r.runner.store.delete("mcp-hook", "admin")
        out = r.store.remove("hello-tool", "admin")
        assert out == {"removed": "hello-tool", "version": "0.1.0"}
        assert r.store.get("hello-tool") is None
        assert not os.path.exists(os.path.join(r.store.root(), "hello-tool"))
        refused(lambda: r.store.remove("hello-tool", "admin"), 404, "not_found")
        acts = [a["action"] for a in r.audit() if a["object"] == "plugin"]
        assert acts == ["install", "enable", "disable", "settings", "remove"], acts
    finally:
        r.close()
        shutil.rmtree(src)
    return "enable, disable, remove; blocked while a hook names it"


# ---------------------------------------------------------------------------- 7
def test_upgrade():
    r = Reg("upgrade")
    srcs = []
    try:
        srcs.append(folder("classify-provider"))
        r.install(srcs[-1])
        r.store.write("classify-provider", settings_set={"fallback": "chat",
                                                         "endpoint": "https://x.example/v1",
                                                         "classes": {"code": ["def"]}},
                      secrets_set={"API_KEY": "sk-" + "U1" * 15},
                      grant=["prompts:redacted", "network:192.168.1.30:8720"],
                      confirmed=["network:192.168.1.30:8720"], who="admin")
        r.store.set_enabled("classify-provider", True, "admin")

        def change(m):
            m["settings"]["properties"]["fallback"]["enum"] = ["code", "unclear"]   # chat goes
            m["settings"]["properties"].pop("endpoint")
            m["settings"]["properties"]["depth"] = {"type": "integer", "default": 2}
            m["secrets"].append({"name": "OTHER"})
            m["capabilities"] = ["prompts:redacted", "secrets:API_KEY", "secrets:OTHER",
                                 "prompts:full"]
        srcs.append(folder("classify-provider", bumped("classify-provider", "0.2.0", change)))
        exc = refused(lambda: r.install(srcs[-1]), 409, "exists")
        assert exc.extra == {"installed": "0.1.0", "version": "0.2.0"}
        old_path = r.store.get("classify-provider")["path"]
        p, note = r.install(srcs[-1], upgrade=True)
        assert note["action"] == "upgraded" and note["from"] == "0.1.0"
        assert p["version"] == "0.2.0" and p["enabled"] is True
        assert p["settings"] == {"classes": {"code": ["def"]}}, p["settings"]
        assert sorted(note["dropped"]) == ["capabilities.network:192.168.1.30:8720",
                                           "settings.endpoint", "settings.fallback"], note
        assert "secrets.API_KEY" in note["kept"] and "capabilities.prompts:redacted" in note["kept"]
        assert sorted(p["granted"]) == ["prompts:redacted"], "a new request starts ungranted"
        hints = r.store.secret_hints("classify-provider")
        assert hints["API_KEY"]["set"] and not hints["OTHER"]["set"]
        assert r.store.secret_for("classify-provider", "API_KEY") == "sk-" + "U1" * 15
        assert not os.path.exists(old_path) and os.path.isdir(p["path"])
        assert r.store.effective_settings(p)["depth"] == 2
        # the same version again: unchanged; another tree under it: refused
        p2, note = r.install(srcs[-1], upgrade=True)
        assert note["action"] == "unchanged" and p2["row_version"] == p["row_version"]
        with open(os.path.join(srcs[-1], "provider.py"), "a") as f:
            f.write("# a change without a new version\n")
        refused(lambda: r.install(srcs[-1], upgrade=True), 409, "version_unchanged")
        # an older version
        refused(lambda: r.install(srcs[0], upgrade=True), 409, "downgrade")
        p3, note = r.install(srcs[0], upgrade=True, allow_downgrade=True)
        assert note["action"] == "upgraded" and p3["version"] == "0.1.0"
        assert "secrets.OTHER" not in note["kept"]
        assert [a["action"] for a in r.audit() if a["object"] == "plugin"].count("upgrade") == 2
    finally:
        r.close()
        for s in srcs:
            shutil.rmtree(s)
    return "kept what the new schema allows, listed the rest; refusals hold"


# ---------------------------------------------------------------------------- 8
def test_interrupted_install():
    r = Reg("cut")
    src = folder()
    try:
        r.store.ready()
        real = r.cfg._audit

        def boom(*a, **kw):
            raise RuntimeError("the power went")
        r.cfg._audit = boom
        try:
            r.install(src)
            raise AssertionError("installed")
        except RuntimeError:
            pass
        finally:
            r.cfg._audit = real
        assert r.store.get("hello-tool") is None
        assert not os.path.exists(os.path.join(r.store.root(), "hello-tool", "0.1.0"))
        assert not os.listdir(r.store._staging())
        # a crash between the move and the row, and one while staging: the next start sweeps
        os.makedirs(os.path.join(r.store.root(), "ghost", "1.0.0"))
        write_tree(os.path.join(r.store.root(), ".staging", "abc123"), {"plugin.json": "{}"})
        r.install(src)
        from proxy import pluginstore
        again = pluginstore.PluginStore(r.cfg, hooks=r.runner.store, log=r.logs.append,
                                        tandemapp="0.15.0", hooks_api="1.0")
        again.ready()
        assert not os.path.exists(os.path.join(r.store.root(), "ghost"))
        assert not os.path.exists(os.path.join(r.store.root(), ".staging"))
        assert os.path.isdir(os.path.join(r.store.root(), "hello-tool", "0.1.0"))
        assert any("cut short" in m for m in r.logs)
    finally:
        r.close()
        shutil.rmtree(src)
    return "no half-installed plugin; leftovers swept at start"


# ---------------------------------------------------------------------------- 9
def test_compatibility_at_install_and_enable():
    r = Reg("compat")
    m = example("hello-tool")
    m["tandemapp"] = ">=0.17"
    src = folder(manifest=m)
    ok = folder()
    try:
        exc = refused(lambda: r.install(src), 422, "plugin_refused")
        e = exc.extra["errors"][0]
        assert (e["code"], e["needed"], e["have"]) == ("incompatible_tandemapp", ">=0.17",
                                                      "0.15.0"), e
        assert ">=0.17" in exc.message
        m2 = example("hello-tool")
        m2["hooks_api"] = "1.3"
        src2 = folder(manifest=m2)
        exc = refused(lambda: r.install(src2), 422, "plugin_refused")
        assert exc.extra["errors"][0]["code"] == "incompatible_hooks_api"
        shutil.rmtree(src2)
        r.install(ok)
        from proxy import pluginstore
        older = pluginstore.PluginStore(r.cfg, hooks=r.runner.store, tandemapp="0.14.2",
                                        hooks_api="1.0")
        exc = refused(lambda: older.set_enabled("hello-tool", True, "admin"), 409,
                      "incompatible_tandemapp")
        assert exc.extra["needed"] == ">=0.15" and exc.extra["have"] == "0.14.2"
        r.store.set_enabled("hello-tool", True, "admin")
        refused(lambda: older.load("hello-tool"), 409, "incompatible_tandemapp")
    finally:
        r.close()
        shutil.rmtree(src)
        shutil.rmtree(ok)
    return "refused with the needed version at install, enable and load"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
