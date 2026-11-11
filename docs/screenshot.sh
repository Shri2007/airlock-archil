#!/bin/sh
# Re-take docs/viewer.png from the current viewer.html + results.json.
# Usage (from repo root): sh docs/screenshot.sh
set -e
python3 -m http.server 8799 --bind 127.0.0.1 >/dev/null 2>&1 &
PID=$!
sleep 1
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new \
  --disable-gpu --hide-scrollbars --window-size=1440,1700 \
  --virtual-time-budget=8000 --screenshot=docs/viewer.png \
  http://127.0.0.1:8799/viewer.html 2>/dev/null
kill $PID
echo "wrote docs/viewer.png"
