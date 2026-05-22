#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BUDDY_DIR="${ROOT_DIR}/lumi-buddy"
MAKEFILE="${BUDDY_DIR}/Makefile"
DIST_DIR="${BUDDY_DIR}/dist"

# Bucket and path: lumi/ota/lumi-buddy/[semver].dmg
GCS_BUCKET="${GCS_BUCKET:-s3-autonomous-upgrade-3}"

# Build target — `dmg` (unsigned, default) or `dmg-signed` (Developer ID + notarized).
# Override via env: BUDDY_DMG_TARGET=dmg-signed scripts/upload-lumi-buddy.sh
DMG_TARGET="${BUDDY_DMG_TARGET:-dmg}"

if [[ ! -f "$MAKEFILE" ]]; then
  echo "Error: $MAKEFILE not found"
  exit 1
fi

# Read current VERSION from lumi-buddy/Makefile (line: `VERSION    := X.Y.Z`)
current_version=$(grep -E '^VERSION[[:space:]]*:=' "$MAKEFILE" | head -n1 | sed -E 's/^VERSION[[:space:]]*:=[[:space:]]*([0-9.]+).*/\1/')

if [[ -z "$current_version" ]]; then
  echo "Error: could not parse VERSION from $MAKEFILE"
  exit 1
fi

# Auto-increment patch and rewrite Makefile in place
IFS='.' read -r major minor patch <<< "$current_version"
patch=$((patch + 1))
new_version="${major}.${minor}.${patch}"

# BSD sed (macOS) compatible in-place edit
sed -i '' -E "s/^VERSION([[:space:]]*):=([[:space:]]*)[0-9.]+/VERSION\1:=\2${new_version}/" "$MAKEFILE"
echo "========== Version bumped: ${current_version} -> ${new_version} =========="

DMG_NAME="LumiBuddy-${new_version}.dmg"
DMG_PATH="${DIST_DIR}/${DMG_NAME}"
GCS_PATH="${GCS_PATH:-lumi/ota/lumi-buddy/${new_version}.dmg}"

echo "========== Building DMG via 'make ${DMG_TARGET}' (VERSION=${new_version}) =========="
(cd "$BUDDY_DIR" && make "$DMG_TARGET")

if [[ ! -f "$DMG_PATH" ]]; then
  echo "Error: expected DMG not found at $DMG_PATH"
  exit 1
fi

echo "========== Upload ${DMG_NAME} to Google Cloud Storage (no-cache) =========="
gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" \
       -h "Content-Type:application/x-apple-diskimage" \
       cp "$DMG_PATH" "gs://${GCS_BUCKET}/${GCS_PATH}"

# Update metadata.json (lumi/ota/metadata.json) - lumi-buddy key
METADATA_PATH="lumi/ota/metadata.json"
METADATA_TMP=$(mktemp)
BUDDY_URL="${BUDDY_URL:-https://storage.googleapis.com/${GCS_BUCKET}/${GCS_PATH}}"

echo "========== Fetch metadata from gs://${GCS_BUCKET}/${METADATA_PATH} =========="
if gsutil cp "gs://${GCS_BUCKET}/${METADATA_PATH}" "$METADATA_TMP" 2>/dev/null; then
  content=$(cat "$METADATA_TMP")
else
  content=""
fi

if [[ -z "$(echo "$content" | tr -d '[:space:]')" ]]; then
  content="{}"
fi

updated_metadata=$(echo "$content" | python3 -c "
import json, sys
raw = sys.stdin.read()
try:
    data = json.loads(raw) if raw.strip() else {}
except json.JSONDecodeError:
    data = {}
data['lumi-buddy'] = {'version': sys.argv[1], 'url': sys.argv[2]}
print(json.dumps(data, indent=2))
" "$new_version" "$BUDDY_URL")

echo "$updated_metadata" > "$METADATA_TMP"
echo "========== Upload metadata (lumi-buddy: v${new_version}) =========="
gsutil -h "Content-Type:application/json" -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$METADATA_TMP" "gs://${GCS_BUCKET}/${METADATA_PATH}"
rm -f "$METADATA_TMP"

echo "Done: gs://${GCS_BUCKET}/${GCS_PATH} (v${new_version})"
echo "URL:  ${BUDDY_URL}"
