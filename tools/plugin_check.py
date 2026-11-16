"""Check a plugin folder against plugin-v1 the way an install does, without installing it.

    python tools/plugin_check.py path/to/plugin                 ok, or one line a refusal
    python tools/plugin_check.py --json path/to/plugin          the whole report as JSON
    python tools/plugin_check.py --tandemapp 0.16.0 --hooks-api 1.0 path/to/plugin

Nothing in the folder runs: the files are read and hashed (contracts/plugin-v1, docs/plugins.md).
Exit 0 when the plugin would install, 1 when it would be refused, 2 for a usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from proxy import pluginmanifest as PM  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("folder")
    ap.add_argument("--json", action="store_true", help="print the whole report as JSON")
    ap.add_argument("--tandemapp", help="the TandemApp version to check against (default: this "
                                        "checkout's)")
    ap.add_argument("--hooks-api", help="the hooks API version (default: this checkout's)")
    a = ap.parse_args(argv)
    if not os.path.isdir(a.folder):
        print(f"no folder {a.folder}", file=sys.stderr)
        return 2
    rep = PM.inspect(os.path.abspath(a.folder), tandemapp=a.tandemapp, hooks_api=a.hooks_api)
    if a.json:
        print(json.dumps(rep, indent=1, sort_keys=True))
        return 0 if rep["ok"] else 1
    m = rep["manifest"] or {}
    if rep["ok"]:
        print(f"ok {m.get('name')} {m.get('version')} sha256 {rep['sha256']} "
              f"({rep['files']} files)")
        for w in rep["warnings"]:
            print(f"warning: {w}")
        return 0
    for e in rep["errors"]:
        print(f"{e['code']}: {e['message']}" + (f" [{e['field']}]" if e.get("field") else ""))
    return 1


if __name__ == "__main__":
    sys.exit(main())
