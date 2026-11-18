#!/usr/bin/env bash
# One command for a TandemApp release: bump, preflight, the box gate, tag, push, the GitHub release, verify.
#
#   bash tools/release.sh X.Y.Z --only bump                          write X.Y.Z into the version lines
#   bash tools/release.sh X.Y.Z --notes FILE --box HOST --dry-run    preflight, gate and verify; the tag,
#                                                                    push and release commands are printed
#   bash tools/release.sh X.Y.Z --notes FILE --box HOST              every section, in order
#
# Sections (--only takes a comma-separated list; the default is preflight,gate,tag,push,release,verify):
#
#   bump       VERSION, the chart's version and appVersion, the Homebrew formula's url and the README's
#              install lines. CHANGELOG.md stays the author's. Writes files, no commit.
#   preflight  read-only: a clean tree on main; the remote's main an ancestor of HEAD; no vX.Y.Z on the
#              remote; every version line says X.Y.Z and none the previous release; CHANGELOG.md's first
#              section is "## X.Y.Z, YYYY-MM-DD"; no commit since the previous tag carries a co-author or
#              session trailer; the notes file is there; gh is logged in
#   gate       on the box (ssh --box): a git archive of HEAD into ~/release-gate-tandemapp-X.Y.Z-<sha>, the
#              page built there (tools/dashboard_bundle.py build), TandemApp installed into a venv of its
#              own and the app suite (tests/script_suite.py --all, every tests/test_*.py), under nohup and
#              polled; then tools/app_release.py pack and a wheel build, the release workflow's files
#   tag        the annotated tag vX.Y.Z "TandemApp X.Y.Z" on HEAD
#   push       HEAD as the remote's main and the tag, no force
#   release    one GitHub release, "TandemApp X.Y.Z", the notes file, latest; the release workflow
#              (.github/workflows/release.yml) attaches the files on the tag push, and when it made a draft
#              first this fills and publishes that one
#   verify     read-only: the tag on the remote, main containing it, the release's title, draft state and
#              latest flag, and its files (waits up to --asset-wait minutes for the workflow)
#
# Exit 0 when every section run passed. Bash 3.2 (macOS) compatible.
set -euo pipefail

usage() { sed -n '2,/^set -euo pipefail$/p' "$0" | sed '$d'; }

VERSION=""; NOTES=""; BOX="${RELEASE_BOX:-}"; ONLY=""; DRY=0; ASSET_WAIT=30; GATE_WAIT=90
REMOTE=origin; GH_REPO=""
while [ $# -gt 0 ]; do
    case "$1" in
        --notes) NOTES="$2"; shift 2 ;;
        --box) BOX="$2"; shift 2 ;;
        --only) ONLY="$2"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        --asset-wait) ASSET_WAIT="$2"; shift 2 ;;
        --gate-wait) GATE_WAIT="$2"; shift 2 ;;
        --remote) REMOTE="$2"; shift 2 ;;
        --repo) GH_REPO="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "unknown option $1" >&2; exit 2 ;;
        *) [ -z "$VERSION" ] && VERSION="$1" || { echo "one version only" >&2; exit 2; }; shift ;;
    esac
done
[ -n "$VERSION" ] || { usage; exit 2; }
printf '%s' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' || { echo "X.Y.Z, not $VERSION" >&2; exit 2; }

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TAG="v$VERSION"
FAILS=0
say() { printf '[release] %s\n' "$*"; }
pass() { printf '  ok    %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; FAILS=$((FAILS + 1)); }
die() { say "STOP: $*"; exit 1; }
g() { git -C "$ROOT" "$@"; }
run() { if [ "$DRY" = 1 ]; then printf '  would run: %s\n' "$*"; else "$@"; fi; }
want() { case ",${ONLY:-preflight,gate,tag,push,release,verify}," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }

if [ -z "$GH_REPO" ]; then
    url="$(g remote get-url "$REMOTE")"
    GH_REPO="$(printf '%s\n' "$url" | sed -E 's#^(git@github\.com:|https://github\.com/|ssh://git@github\.com/)##; s#\.git$##')"
fi
PREV="$(g describe --tags --abbrev=0 --match 'v[0-9]*' HEAD 2>/dev/null || true)"
[ "$PREV" = "$TAG" ] && PREV="$(g describe --tags --abbrev=0 --match 'v[0-9]*' HEAD^ 2>/dev/null || true)"

# ------------------------------------------------------------------------------------------- bump
bump() {
    say "== bump $VERSION"
    printf '%s\n' "$VERSION" > "$ROOT/VERSION"
    sed -i.bak -E "s/^version: .*/version: $VERSION/; s/^appVersion: .*/appVersion: \"$VERSION\"/" "$ROOT/charts/tandemapp/Chart.yaml"
    sed -i.bak -E "s#releases/download/v[0-9]+\.[0-9]+\.[0-9]+/tandemapp-[0-9]+\.[0-9]+\.[0-9]+\.tar\.gz#releases/download/$TAG/tandemapp-$VERSION.tar.gz#" "$ROOT/packaging/homebrew/tandemapp.rb"
    sed -i.bak -E "s#v[0-9]+\.[0-9]+\.[0-9]+/tandemapp-[0-9]+\.[0-9]+\.[0-9]+\.tar\.gz#$TAG/tandemapp-$VERSION.tar.gz#g; s#tandemapp-[0-9]+\.[0-9]+\.[0-9]+(\.tar\.gz|/packaging|-py3-none-any\.whl|\.tgz)#tandemapp-$VERSION\1#g; s#tandemapp-dashboard-[0-9]+\.[0-9]+\.[0-9]+\.tar\.gz#tandemapp-dashboard-$VERSION.tar.gz#g" "$ROOT/README.md"
    rm -f "$ROOT/charts/tandemapp/Chart.yaml.bak" "$ROOT/packaging/homebrew/tandemapp.rb.bak" "$ROOT/README.md.bak"
    say "written; CHANGELOG.md's \"## $VERSION, YYYY-MM-DD\" section stays the author's"
}

# -------------------------------------------------------------------------------------- preflight
pin() { # file text what
    if g show "HEAD:$1" 2>/dev/null | grep -qF -- "$2"; then pass "$1: $3"; else fail "$1: $3 (\"$2\" missing)"; fi
}
preflight() {
    say "== preflight $TAG ($GH_REPO, remote $REMOTE)"
    [ -z "$(g status --porcelain)" ] && pass "clean tree" || fail "the tree has changes"
    [ "$(g rev-parse --abbrev-ref HEAD)" = main ] && pass "on main" || fail "not on main"
    g fetch -q "$REMOTE" main --tags || fail "fetch $REMOTE"
    g merge-base --is-ancestor "$REMOTE/main" HEAD && pass "$REMOTE/main is an ancestor of HEAD" || fail "$REMOTE/main is not an ancestor of HEAD (fast-forward only)"
    g ls-remote --exit-code --tags "$REMOTE" "refs/tags/$TAG" > /dev/null 2>&1 && fail "$TAG is on $REMOTE already" || pass "no $TAG on $REMOTE"
    [ "$(g show HEAD:VERSION)" = "$VERSION" ] && pass "VERSION $VERSION" || fail "VERSION says $(g show HEAD:VERSION)"
    pin charts/tandemapp/Chart.yaml "version: $VERSION" "chart version"
    pin charts/tandemapp/Chart.yaml "appVersion: \"$VERSION\"" "chart appVersion"
    pin packaging/homebrew/tandemapp.rb "releases/download/$TAG/tandemapp-$VERSION.tar.gz" "Homebrew url"
    pin README.md "releases/download/$TAG/tandemapp-$VERSION.tar.gz" "README install line"
    if [ -n "$PREV" ]; then
        p="${PREV#v}"
        if g grep -n -F "tandemapp-$p" HEAD -- README.md packaging/homebrew/tandemapp.rb > /dev/null; then fail "a line still names $p"; else pass "no line names $p"; fi
    fi
    first="$(g show HEAD:CHANGELOG.md | grep -m1 '^## ' || true)"
    printf '%s' "$first" | grep -Eq "^## $VERSION, [0-9]{4}-[0-9]{2}-[0-9]{2}$" && pass "CHANGELOG.md: $first" || fail "CHANGELOG.md's first section is \"$first\", not \"## $VERSION, YYYY-MM-DD\""
    range="HEAD"; [ -n "$PREV" ] && range="$PREV..HEAD"
    if [ -n "$PREV" ]; then
        bad="$(g log --format='%h %s%n%b' "$range" | grep -i -E '^(co-authored-by|claude-session):' || true)"
        [ -z "$bad" ] && pass "no co-author or session trailer since $PREV" || fail "trailers since $PREV: $bad"
    else
        say "  (no previous tag: the trailer check starts with the next release)"
    fi
    if [ -n "$NOTES" ]; then [ -s "$NOTES" ] && pass "notes $NOTES" || fail "no notes at $NOTES"; else want release && fail "no --notes FILE" || true; fi
    gh auth status > /dev/null 2>&1 && pass "gh is logged in" || fail "gh is not logged in"
}

# ------------------------------------------------------------------------------------------- gate
gate() {
    [ -n "$BOX" ] || { fail "no --box HOST for the gate"; return; }
    local sha dir; sha="$(g rev-parse --short=12 HEAD)"; dir="release-gate-tandemapp-$VERSION-$sha"
    say "== gate on $BOX: ~/$dir"
    g archive HEAD | ssh -o BatchMode=yes "$BOX" "mkdir -p ~/$dir/tree && tar -x -C ~/$dir/tree" || { fail "archive to $BOX"; return; }
    ssh -o BatchMode=yes "$BOX" "cat > ~/$dir/gate.sh" <<'GATE'
#!/usr/bin/env bash
set -u
D="$(cd "$(dirname "$0")" && pwd)"; T="$D/tree"; cd "$T"
for n in "$HOME"/.local/node-*/bin; do [ -d "$n" ] && PATH="$n:$PATH"; done
step() { echo "== $1 $(date -u +%H:%M:%SZ)"; }
step page;  python3 tools/dashboard_bundle.py build --commit "$(cat "$D/sha")" > "$D/page.log" 2>&1 || { echo "GATE FAIL page"; exit 1; }
step venv;  python3 -m venv "$D/venv" && "$D/venv/bin/pip" install -q ".[host]" pytest jsonschema pyyaml build > "$D/venv.log" 2>&1 || { echo "GATE FAIL venv"; exit 1; }
step suite; mkdir -p "$D/suite"
ls tests/test_*.py | xargs -P 4 -n 1 "$D/venv/bin/python" tests/script_suite.py --all --out "$D/suite/logs" > "$D/suite/RESULT" 2>&1
grep -E -i "fail|error" "$D/suite/RESULT" | grep -v -E "(^| )0 (failed|errors?)" | head -20 > "$D/suite/bad"
step files; "$D/venv/bin/python" tools/app_release.py pack --out "$D/out/tandemapp-$(cat VERSION).tar.gz" > "$D/files.log" 2>&1 \
  && "$D/venv/bin/python" -m build --wheel --outdir "$D/out" . >> "$D/files.log" 2>&1 || { echo "GATE FAIL files"; exit 1; }
ls "$D/out"
if [ -s "$D/suite/bad" ]; then echo "GATE FAIL suite"; cat "$D/suite/bad"; exit 1; fi
echo "GATE PASS"
GATE
    ssh -o BatchMode=yes "$BOX" "echo $sha > ~/$dir/sha && (setsid nohup bash ~/$dir/gate.sh > ~/$dir/gate.log 2>&1 &)" || { fail "start the gate"; return; }
    local end=$(( $(date +%s) + GATE_WAIT * 60 )) out=""
    while [ "$(date +%s)" -lt "$end" ]; do
        out="$(ssh -o BatchMode=yes "$BOX" "tail -3 ~/$dir/gate.log" 2>/dev/null || true)"
        case "$out" in *"GATE PASS"*|*"GATE FAIL"*) break ;; esac
        sleep 30
    done
    ssh -o BatchMode=yes "$BOX" "cat ~/$dir/gate.log; echo; tail -3 ~/$dir/suite/RESULT 2>/dev/null"
    case "$out" in *"GATE PASS"*) pass "gate on $BOX" ;; *) fail "gate on $BOX (log ~/$dir/gate.log)" ;; esac
}

# ---------------------------------------------------------------------------- tag, push, release
tag() { say "== tag $TAG"; run g tag -a "$TAG" -m "TandemApp $VERSION" HEAD; }
push() { say "== push"; run g push "$REMOTE" HEAD:refs/heads/main; run g push "$REMOTE" "refs/tags/$TAG"; }
release() {
    say "== release $TAG"
    [ -n "$NOTES" ] || die "no --notes FILE"
    if [ "$DRY" = 0 ] && gh release view "$TAG" -R "$GH_REPO" > /dev/null 2>&1; then
        run gh release edit "$TAG" -R "$GH_REPO" --title "TandemApp $VERSION" --notes-file "$NOTES" --draft=false --latest
    else
        run gh release create "$TAG" -R "$GH_REPO" --verify-tag --latest --title "TandemApp $VERSION" --notes-file "$NOTES"
    fi
}

# ----------------------------------------------------------------------------------------- verify
verify() {
    say "== verify $TAG on $GH_REPO"
    if ! g ls-remote --exit-code --tags "$REMOTE" "refs/tags/$TAG" > /dev/null 2>&1; then
        say "  $TAG is not on $REMOTE yet: verify would check the tag, main, the release (title, draft, latest) and its files:"
        say "  tandemapp-$VERSION.tar.gz(.sha256), tandemapp-$VERSION-py3-none-any.whl, tandemapp-dashboard-$VERSION.tar.gz(.sha256), tandemapp-$VERSION.tgz, SHA256SUMS"
        return
    fi
    g fetch -q "$REMOTE" main --tags
    [ "$(g rev-parse "$TAG^{commit}")" = "$(g ls-remote "$REMOTE" "refs/tags/$TAG^{}" | cut -f1)" ] && pass "the tag on $REMOTE is the local tag's commit" || fail "the tag on $REMOTE differs"
    g merge-base --is-ancestor "$TAG" "$REMOTE/main" && pass "main contains $TAG" || fail "main does not contain $TAG"
    local end=$(( $(date +%s) + ASSET_WAIT * 60 )) names=""
    while :; do
        names="$(gh release view "$TAG" -R "$GH_REPO" --json assets -q '.assets[].name' 2>/dev/null || true)"
        printf '%s\n' "$names" | grep -qx SHA256SUMS && break
        [ "$(date +%s)" -ge "$end" ] && break
        sleep 30
    done
    for f in "tandemapp-$VERSION.tar.gz" "tandemapp-$VERSION.tar.gz.sha256" "tandemapp-$VERSION-py3-none-any.whl" \
             "tandemapp-dashboard-$VERSION.tar.gz" "tandemapp-$VERSION.tgz" SHA256SUMS; do
        printf '%s\n' "$names" | grep -qx -- "$f" && pass "file $f" || fail "file $f missing"
    done
    local meta; meta="$(gh release view "$TAG" -R "$GH_REPO" --json name,isDraft,isPrerelease -q '"\(.name)|\(.isDraft)|\(.isPrerelease)"')"
    [ "$meta" = "TandemApp $VERSION|false|false" ] && pass "release \"TandemApp $VERSION\", published" || fail "release: $meta"
}

if [ "${ONLY:-}" = bump ]; then bump; exit 0; fi
want preflight && preflight
want gate && gate
if [ "$FAILS" -gt 0 ]; then say "$FAILS failed: no tag, push or release"; exit 1; fi
want tag && tag
want push && push
want release && release
want verify && verify
[ "$FAILS" = 0 ] && say "done" || { say "$FAILS failed"; exit 1; }
