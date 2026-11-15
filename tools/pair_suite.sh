#!/usr/bin/env bash
# The pair suite from TandemApp's side (ENG-413 WP14): the engine's pair tests, which run the engine
# and this tree's extension, packages and test helpers together, against an engine checkout at the
# commit contracts/engine-api-v1/PIN names. TandemApp depends on the engine's contract, never the
# other way round, so the tests live in the engine's tests/ and the engine's tools/pair_suite.sh runs
# them with TANDEMAPP_SRC set to this tree.
#
#   bash tools/pair_suite.sh ENGINE OUT     ENGINE: an engine checkout; results in OUT
#
# ENGINE as a git checkout must be at PIN's engine_commit (or a descendant whose contracts equal
# it); ENGINE as a plain tree (an archive) is held to this tree's copy of the contracts by the
# engine's script, file for file. PY (default python3) and SUITE_JOBS (default 4) pass through.
# Exit: the engine's script's, or 2 on bad use or another commit.
set -euo pipefail
[ $# -eq 2 ] || { echo "usage: pair_suite.sh ENGINE OUT" >&2; exit 2; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE="$(cd "$1" && pwd)"
[ -f "$ENGINE/tools/pair_suite.sh" ] || { echo "pair_suite: $ENGINE has no tools/pair_suite.sh" >&2; exit 2; }
PIN="$(awk '$1 == "engine_commit" { print $2 }' "$HERE/contracts/engine-api-v1/PIN")"
if HEAD="$(git -C "$ENGINE" rev-parse -q --verify HEAD 2>/dev/null)" \
        && [ "$(git -C "$ENGINE" rev-parse --show-toplevel 2>/dev/null)" = "$ENGINE" ]; then
    if [ "$HEAD" != "$PIN" ] && ! git -C "$ENGINE" merge-base --is-ancestor "$PIN" "$HEAD" 2>/dev/null; then
        echo "pair_suite: $ENGINE is at $HEAD, not at the pinned $PIN or after it" >&2
        exit 2
    fi
    echo "pair_suite: engine $HEAD (PIN $PIN)"
else
    echo "pair_suite: $ENGINE is no git checkout: the contracts are compared file for file"
fi
TANDEMAPP_SRC="$HERE" exec bash "$ENGINE/tools/pair_suite.sh" "$2"
