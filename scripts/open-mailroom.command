#!/bin/bash
# Opens the Mailroom dashboard in your default browser.
# Assumes the GUI daemon (com.tighe.mailroom.gui) is running via launchd.
# If the server isn't reachable, prints a hint and exits non-zero.
URL="http://localhost:8501"

if curl -sfo /dev/null --max-time 2 "$URL"; then
  open "$URL"
else
  echo "Mailroom server isn't reachable at $URL." >&2
  echo "Load the GUI daemon:" >&2
  echo "  launchctl load ~/Library/LaunchAgents/com.tighe.mailroom.gui.plist" >&2
  echo "Or run the server in foreground for this session:" >&2
  echo "  .venv/bin/uvicorn app:app --host 127.0.0.1 --port 8501" >&2
  exit 1
fi
