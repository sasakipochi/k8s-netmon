#!/bin/sh
# docs/figures/*.html (構成図・表) を docs/*.png に描画する。Google Chrome (または Chromium) が必要
#   ./tools/render_figures.sh
set -eu
cd "$(dirname "$0")/.."
CHROME=${CHROME:-$(command -v google-chrome || command -v chromium || command -v chromium-browser)}
PROFILE=$(mktemp -d)
trap 'rm -rf "$PROFILE"' EXIT

render() { # name width height
  "$CHROME" --headless=new --no-sandbox --disable-gpu --hide-scrollbars --user-data-dir="$PROFILE" \
    --force-device-scale-factor=2 --window-size="$2,$3" \
    --screenshot="docs/$1.png" "file://$PWD/docs/figures/$1.html" >/dev/null 2>&1
  echo "docs/$1.png"
}

render architecture 1200 650
render ping-targets 1100 456
