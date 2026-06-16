#!/usr/bin/env bash
#
# release.sh — cut a new MAPOG QGIS plugin release.
#
# Usage:   ./release.sh 0.4.0
#
# It writes the version into mapog/metadata.txt and docs/plugins.xml, rebuilds
# docs/mapog.zip from mapog/, and stages everything. Then review and push:
#
#   git commit -m "Release vX.Y.Z" && git push origin main
#
# Your friend (subscribed to the repository URL) then sees an "Upgrade" badge
# in the QGIS Plugin Manager.

set -euo pipefail

VERSION="${1:-}"
if [[ -z "$VERSION" ]]; then
  echo "usage: $0 <version>   e.g. $0 0.4.0" >&2
  exit 1
fi
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "error: version must look like X.Y.Z (got '$VERSION')" >&2
  exit 1
fi

cd "$(dirname "$0")"

# 1. metadata.txt — the line "version=..."
if grep -qE '^version=' mapog/metadata.txt; then
  sed -i '' -E "s/^version=.*/version=${VERSION}/" mapog/metadata.txt
else
  echo "error: no 'version=' line in mapog/metadata.txt" >&2
  exit 1
fi

# 2. plugins.xml — both the version="..." attribute and the <version> tag.
sed -i '' -E "s/(<pyqgis_plugin name=\"MAPOG\" version=\")[^\"]*(\")/\1${VERSION}\2/" docs/plugins.xml
sed -i '' -E "s|<version>[^<]*</version>|<version>${VERSION}</version>|" docs/plugins.xml

# 2b. Stamp today's date as <update_date> so QGIS shows a fresh release date
# (the upgrade reads as new, not stuck on the original create_date).
TODAY="$(date +%F)"
sed -i '' -E "s|<update_date>[^<]*</update_date>|<update_date>${TODAY}</update_date>|" docs/plugins.xml

# 3. Rebuild the downloadable zip (top-level mapog/ dir, no caches).
rm -f docs/mapog.zip
zip -rq docs/mapog.zip mapog -x '*.pyc' -x '*/__pycache__/*' -x '*.DS_Store'

# 4. Sanity-check the manifest is still well-formed XML.
python3 -c "import xml.dom.minidom; xml.dom.minidom.parse('docs/plugins.xml')"

# 5. Stage everything for review.
git add mapog/metadata.txt docs/plugins.xml docs/mapog.zip mapog

echo
echo "Release v${VERSION} prepared:"
grep -E '^version=' mapog/metadata.txt
grep -E '<version>' docs/plugins.xml
grep -E '<update_date>' docs/plugins.xml
echo
echo "Next:  git commit -m \"Release v${VERSION}\" && git push origin main"
