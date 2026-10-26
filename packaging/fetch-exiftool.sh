#!/usr/bin/env bash
# Vendor the pure-Perl ExifTool distribution into packaging/vendor/exiftool so
# the spec bundles it. The app runs it through the system perl. ExifTool is
# dual-licensed (Artistic / GPL), which permits redistribution.
#
# packaging/exiftool.lock holds the pinned version on line 1 and the sha256 of
# the tarball on line 2. On first use with no checksum, the script prints the
# hash of what it downloaded and stops so you can pin it deliberately.
set -euo pipefail
cd "$(dirname "$0")"

VERSION=$(sed -n 1p exiftool.lock)
EXPECTED=$(sed -n 2p exiftool.lock)
TARBALL="Image-ExifTool-${VERSION}.tar.gz"
URL="https://exiftool.org/${TARBALL}"

mkdir -p vendor/download
cd vendor/download
[ -f "$TARBALL" ] || curl -fsSLO "$URL"
ACTUAL=$(shasum -a 256 "$TARBALL" | cut -d' ' -f1)

if [ -z "$EXPECTED" ]; then
  echo "No checksum pinned in packaging/exiftool.lock."
  echo "Downloaded ${TARBALL} with sha256 ${ACTUAL}."
  echo "Verify it against exiftool.org, add the hash as line 2, and re-run."
  exit 2
fi
if [ "$ACTUAL" != "$EXPECTED" ]; then
  echo "Checksum mismatch for ${TARBALL}: expected ${EXPECTED}, got ${ACTUAL}" >&2
  exit 1
fi

rm -rf ../exiftool
mkdir -p ../exiftool
tar -xzf "$TARBALL" -C ../exiftool --strip-components=1
echo "Vendored ExifTool ${VERSION} into packaging/vendor/exiftool"
