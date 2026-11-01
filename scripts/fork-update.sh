#!/usr/bin/env bash
set -euo pipefail

# fork-update.sh — upgrade a Fei2-Labs/kirocrew fork install from GitHub Releases.
#
# Usage:
#   bash scripts/fork-update.sh           # latest release
#   bash scripts/fork-update.sh v0.11.0+fork.gabfddaa3   # specific tag
#
# Works on any device with pip and curl. Does NOT require a git clone.

REPO="Fei2-Labs/kirocrew"
API="https://api.github.com/repos/${REPO}/releases"

tag="${1:-}"

echo "🔍 Checking current version..."
current=$( (kirocrew --version 2>/dev/null || echo "not installed") | head -1)
echo "   Current: $current"

if [ -z "$tag" ]; then
  echo "🔍 Fetching latest fork release..."
  release_json=$(curl -fsSL "${API}/latest")
  tag=$(echo "$release_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])")
else
  echo "🔍 Fetching release $tag..."
  release_json=$(curl -fsSL "${API}/tags/${tag}")
fi

echo "   Target:  $tag"

# Find the wheel asset URL
wheel_url=$(echo "$release_json" | python3 -c "
import sys, json
assets = json.load(sys.stdin).get('assets', [])
for a in assets:
    if a['name'].endswith('.whl'):
        print(a['browser_download_url'])
        break
else:
    sys.exit(1)
")

if [ -z "$wheel_url" ]; then
  echo "❌ No wheel found in release $tag"
  exit 1
fi

wheel_name=$(basename "$wheel_url")
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

echo "⬇️  Downloading $wheel_name..."
curl -fSL -o "${tmpdir}/${wheel_name}" "$wheel_url"

echo "📦 Installing..."

# Find the pip that owns the current kirocrew install.
KIROCREW_BIN=$(command -v kirocrew 2>/dev/null || true)
CREW_PIP=""

if [ -n "$KIROCREW_BIN" ]; then
  # Resolve symlinks to find the real launcher directory.
  real="$KIROCREW_BIN"
  while [ -L "$real" ]; do real=$(readlink "$real"); done
  bindir=$(cd "$(dirname "$real")" && pwd)

  # cli.sh installs: wrapper calls $DIR/python3.12 -m kiro_crew
  if [ -x "$bindir/python3.12" ]; then
    CREW_PIP="$bindir/python3.12 -m pip"
  elif [ -x "$bindir/python3" ]; then
    CREW_PIP="$bindir/python3 -m pip"
  fi
fi

# crew-venv installs (source build)
if [ -z "$CREW_PIP" ] && [ -x "$HOME/.kiro/crew-venv/bin/pip" ]; then
  CREW_PIP="$HOME/.kiro/crew-venv/bin/pip"
fi

# Fallback to whatever pip is on PATH
if [ -z "$CREW_PIP" ]; then
  CREW_PIP="pip"
fi

# --break-system-packages handles uv/PEP 668 managed environments safely
$CREW_PIP install --break-system-packages --force-reinstall --no-deps "${tmpdir}/${wheel_name}" 2>/dev/null \
  || $CREW_PIP install --force-reinstall --no-deps "${tmpdir}/${wheel_name}"

echo ""
echo "✅ Upgraded:"
kirocrew --version 2>/dev/null || echo "   (restart your shell to pick up the new version)"
echo ""
echo "⚠️  Restart the gateway to apply:"
echo "   kirocrew gateway"
echo "   # or: kirocrew service restart"
