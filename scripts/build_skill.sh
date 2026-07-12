#!/usr/bin/env bash
#
# build_skill.sh — package the Claude skill as an installable `.skill` asset.
#
# A `.skill` file is just a ZIP with a `.skill` extension containing a single
# top-level `internet-historian/` folder. Drop it into Claude to install the
# skill, or unzip it into ~/.claude/skills/.
#
# What goes in: SKILL.md, plus only the supporting files SKILL.md actually
# references. This skill is a THIN control surface — SKILL.md just calls the
# `internet-historian` CLI (installed separately via pipx / Homebrew), so it
# bundles NO supporting files of its own. The `INCLUDE` manifest below is the
# single source of truth; if SKILL.md ever grows a `references/` or `assets/`
# directory, add those paths there.
#
# What stays out: dist/, logs/, data/, __pycache__/, config.local.toml, .git,
# and the engine itself (historian.py) — none of that belongs in a thin skill.
#
# The build is reproducible: same inputs -> byte-identical `.skill`. Set
# SOURCE_DATE_EPOCH to override the timestamp stamped into the archive;
# otherwise we use the commit time of SKILL.md, falling back to a fixed epoch.
#
# Usage:
#   scripts/build_skill.sh            # writes ./internet-historian.skill
#   scripts/build_skill.sh out.skill  # writes to a chosen path

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SKILL_NAME="internet-historian"
OUT="${1:-$ROOT/${SKILL_NAME}.skill}"

# --- The manifest: SKILL.md plus any files it actually references. -----------
# Paths are relative to the repo root and land under `internet-historian/` in
# the archive. Keep this list minimal and honest — one line per bundled file.
INCLUDE=(
  "SKILL.md"
)

# Paths that must NEVER appear inside the package, as a guard against a future
# manifest edit slipping one in.
DENY_REGEX='(^|/)(dist|logs|data|__pycache__|\.git|config\.local\.toml)(/|$)'

# --- Determine a deterministic timestamp for the archive. -------------------
if [[ -n "${SOURCE_DATE_EPOCH:-}" ]]; then
  EPOCH="$SOURCE_DATE_EPOCH"
elif EPOCH="$(git -C "$ROOT" log -1 --format=%ct -- SKILL.md 2>/dev/null)" && [[ -n "$EPOCH" ]]; then
  :
else
  EPOCH=1700000000  # fixed fallback: 2023-11-14T22:13:20Z
fi

# touch timestamp (UTC) in the form touch -t understands: [[CC]YY]MMDDhhmm.SS
if date -r "$EPOCH" +%Y%m%d%H%M.%S >/dev/null 2>&1; then
  TOUCH_TS="$(TZ=UTC date -r "$EPOCH" +%Y%m%d%H%M.%S)"   # BSD/macOS date
else
  TOUCH_TS="$(TZ=UTC date -d "@$EPOCH" +%Y%m%d%H%M.%S)"  # GNU date
fi

# --- Stage into a clean temp dir, then zip it reproducibly. -----------------
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
PKGDIR="$STAGE/$SKILL_NAME"
mkdir -p "$PKGDIR"

echo "Building ${SKILL_NAME}.skill"
echo "  root:      $ROOT"
echo "  epoch:     $EPOCH  (SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-<unset>})"
echo "  contents:"

for rel in "${INCLUDE[@]}"; do
  if [[ "$rel" =~ $DENY_REGEX ]]; then
    echo "ERROR: manifest entry '$rel' matches the exclusion list — refusing." >&2
    exit 1
  fi
  src="$ROOT/$rel"
  if [[ ! -f "$src" ]]; then
    echo "ERROR: manifest entry '$rel' not found at $src" >&2
    exit 1
  fi
  dest="$PKGDIR/$rel"
  mkdir -p "$(dirname "$dest")"
  cp "$src" "$dest"
  size="$(wc -c < "$src" | tr -d ' ')"
  echo "    + $SKILL_NAME/$rel  (${size} bytes)"
done

# Stamp a deterministic mtime on every staged file and directory.
find "$STAGE" -exec touch -t "$TOUCH_TS" {} +

# Zip with a stable entry order and no extra file attributes (-X), so the
# bytes depend only on contents + timestamp, not on uid/gid/host quirks.
rm -f "$OUT"
( cd "$STAGE" \
  && find "$SKILL_NAME" -type f | LC_ALL=C sort | zip -X -q "$OUT" -@ )

echo "  wrote:     $OUT  ($(wc -c < "$OUT" | tr -d ' ') bytes)"
echo
echo "Verify with:  unzip -l \"$OUT\""
