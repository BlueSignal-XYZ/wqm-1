#!/usr/bin/env bash
# Render the WQM-1 Product & Installer Manual (WQM1-IM-100) from its HTML
# source to a print PDF with headless Chromium.
#
#   docs/manual/build.sh                 → docs/manual/dist/WQM1-IM-100-RevH.pdf
#   CHROME=/path/to/chrome docs/manual/build.sh
#
# The source is docs/manual/WQM1-IM-100-RevH.html — one <section class="page">
# per printed page, hand-paginated like the Rev G layout it replaces. Fonts
# and figures are under docs/manual/assets/ and referenced relatively, so the
# HTML opens in any browser for proofing. Chromium is the reference renderer;
# other engines break the page boxes differently.
#
# Rev H's change list is the REV H block on page 2 of the manual and
# docs/manual-errata-revG.md is the audit it answers. Every new export takes
# a NEW filename when published (the CDN serves the marketplace public/ tree
# immutable) — see the "Publishing it" section of that errata file.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/WQM1-IM-100-RevH.html"
OUT_DIR="$HERE/dist"
OUT="$OUT_DIR/WQM1-IM-100-RevH.pdf"

CHROME="${CHROME:-}"
if [ -z "$CHROME" ]; then
  for c in \
    "$(command -v chromium 2>/dev/null || true)" \
    "$(command -v chromium-browser 2>/dev/null || true)" \
    "$(command -v google-chrome 2>/dev/null || true)" \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    /opt/pw-browsers/chromium-*/chrome-linux/chrome; do
    if [ -n "$c" ] && [ -x "$c" ]; then CHROME="$c"; break; fi
  done
fi
if [ -z "$CHROME" ]; then
  echo "build.sh: no Chromium found — set CHROME=/path/to/chrome" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
"$CHROME" --headless=new --disable-gpu --no-sandbox \
  --no-pdf-header-footer --print-to-pdf="$OUT" \
  --virtual-time-budget=10000 \
  "file://$SRC" 2>/dev/null

echo "wrote $OUT"
