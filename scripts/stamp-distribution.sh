#!/usr/bin/env bash
# Write src/kiro_crew/_build_info.py so a packaged build reports which install
# path it came from in the usage beacon's `dist` field.
#
# WHY A GENERATED MODULE, not an env var: `KIROCREW_DISTRIBUTION` is inherited by
# every child process and settable by anyone with a shell, so a stray export
# relabels that host's daily count. A module inside the package tree ships with
# the artifact and a running install cannot change it. `beacon.distribution()`
# prefers this file and falls back to the env var.
#
# WHY A SHARED SCRIPT: six packaging paths need the identical file (wheel,
# dmg, appimage, deb, rpm, docker). Inlining the write in each one is how they drift,
# and a `dist` value that disagrees with the artifact is worse than no value,
# because it is indistinguishable from a real population shift on the dashboard.
#
# It also bakes the FORK REVISION. A wheel or a packaged app has no `.git`, so
# build time is the only moment `git rev-parse` can answer, and
# docs/build/release.md requires anything a stable user will see to be baked
# into the RC bytes BEFORE the RC is cut (promotion never rebuilds). See
# src/kiro_crew/fork_version.py for how the value is consumed and why the
# `FORK_STAMPED` flag makes an empty answer terminal.
#
# Usage: stamp-distribution.sh <dmg|appimage|deb|rpm|wheel|source|docker> [package_dir]
set -euo pipefail

DIST="${1:?usage: stamp-distribution.sh <dmg|appimage|deb|rpm|wheel|source|docker> [package_dir]}"
PKG_DIR="${2:-}"

# Keep in sync with beacon.KNOWN_DISTRIBUTIONS. A typo here would otherwise bake
# a value the clamp rejects, silently falling back to "source": the exact
# failure this script exists to remove.
case "$DIST" in
  dmg|appimage|deb|rpm|wheel|source|docker) ;;
  *) echo "ERROR: unknown distribution '$DIST' (want: dmg|appimage|deb|rpm|wheel|source|docker)" >&2
     exit 1 ;;
esac

if [ -z "$PKG_DIR" ]; then
  PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src/kiro_crew"
fi

if [ ! -d "$PKG_DIR" ]; then
  echo "ERROR: package dir not found: $PKG_DIR" >&2
  exit 1
fi

# Fork revision, derived from the repository this script lives in -- NOT from
# the CWD, which for a packaging run is wherever the build was invoked. Every
# failure (no git, not a repository, a shallow export) degrades to an empty
# revision, which fork_version.py reads as "not a fork build" and renders as the
# plain base version. Never fatal: a version string must not fail a build.
FORK_REVISION=""
FORK_DIRTY="False"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if command -v git >/dev/null 2>&1 && git -C "$REPO_DIR" rev-parse --git-dir >/dev/null 2>&1; then
  # --short=8 matches fork_version.REVISION_WIDTH. `|| true` so `set -e` cannot
  # abort the packaging run on an unborn HEAD.
  FORK_REVISION="$(git -C "$REPO_DIR" rev-parse --short=8 HEAD 2>/dev/null || true)"
  # Untracked files count: the question is "were these the bytes that ran", and
  # a packaging run over an untracked patch is exactly the case worth marking.
  if [ -n "$(git -C "$REPO_DIR" status --porcelain 2>/dev/null || true)" ]; then
    FORK_DIRTY="True"
  fi
fi
# Reject anything that is not a lowercase-hex object name before it reaches a
# generated Python literal.
case "$FORK_REVISION" in
  *[!0-9a-f]*|"") FORK_REVISION="" ;;
esac

cat > "$PKG_DIR/_build_info.py" <<EOF
"""Build-time provenance. GENERATED - do not edit and do not commit.

Written by scripts/stamp-distribution.sh during packaging. Absent from a git
checkout, which is why beacon.baked_distribution() treats ImportError as the
normal case and reports "source".
"""

from __future__ import annotations

DISTRIBUTION = "$DIST"

#: True whenever this file was written by stamp-distribution.sh, so an EMPTY
#: FORK_REVISION is an authoritative "no fork revision" rather than "not asked".
FORK_STAMPED = True

#: Short git sha of the commit this artifact was built from, or "" when git
#: could not answer at packaging time.
FORK_REVISION = "$FORK_REVISION"

#: Whether the packaging tree carried uncommitted changes.
FORK_DIRTY = $FORK_DIRTY
EOF

echo "Stamped distribution=$DIST fork=${FORK_REVISION:-none} dirty=$FORK_DIRTY -> $PKG_DIR/_build_info.py"
