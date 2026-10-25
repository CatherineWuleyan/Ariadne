#!/usr/bin/env bash
# Build a release bundle from this repository.
#
# The repository is already standalone (the three pipelines and the harness are
# vendored), so the bundle is the tracked tree minus development artifacts.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VERSION="$("${PYTHON:-python3}" -c 'import sys; sys.path.insert(0, "ariadne"); from ariadne import __version__; print(__version__)')"
NAME="ariadne-$VERSION"
DIST="$ROOT/dist"
STAGE="$DIST/$NAME"

rm -rf "$STAGE"
mkdir -p "$STAGE"

# Copy only version-controlled files: that is exactly what a clone gets, and it
# cannot drift from the repository the way a hand-written list does.
git ls-files -z | while IFS= read -r -d '' file; do
  mkdir -p "$STAGE/$(dirname "$file")"
  cp "$file" "$STAGE/$file"
done

find "$STAGE" -name '.DS_Store' -delete
rm -rf "$STAGE/dist"
printf '%s\n' "$VERSION" > "$STAGE/VERSION"

# Record a per-file manifest so a user can verify what they received.
(cd "$STAGE" && find . -type f ! -name MANIFEST.sha256 -print0 \
  | sort -z | xargs -0 shasum -a 256 | sed 's# \./# #' > MANIFEST.sha256)

rm -f "$DIST/$NAME-bundle.tar.gz" "$DIST/$NAME-bundle.zip" "$DIST/SHA256SUMS"
# shellcheck disable=SC2035
(cd "$DIST" && tar -czf "$NAME-bundle.tar.gz" "$NAME" && zip -qr "$NAME-bundle.zip" "$NAME")
(cd "$DIST" && shasum -a 256 "$NAME-bundle.tar.gz" "$NAME-bundle.zip" > SHA256SUMS)

echo "Built $NAME"
ls -l "$DIST/$NAME-bundle.tar.gz" "$DIST/$NAME-bundle.zip" "$DIST/SHA256SUMS"
echo "files in bundle: $(wc -l < "$STAGE/MANIFEST.sha256")"
