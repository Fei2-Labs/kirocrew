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
pip install --force-reinstall --no-deps "${tmpdir}/${wheel_name}"

echo ""
echo "✅ Upgraded:"
kirocrew --version 2>/dev/null || echo "   (restart your shell to pick up the new version)"
echo ""
echo "⚠️  Restart the gateway to apply:"
echo "   kirocrew gateway"
echo "   # or: kirocrew service restart"
