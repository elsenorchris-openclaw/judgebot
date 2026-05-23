#!/bin/bash
# Daily: replay the CURRENT live HIGH windows on YESTERDAY's live-recorder data
# (shadow log) + live Kalshi settlement, and append the result to a running log.
# Scheduled by window-replay.timer (13:00 UTC, after all timezones' prior-day
# markets have settled). Builds a forward track record of how the live windows do.
cd /home/ubuntu/paper_judge_bot || exit 1
D=$(date -u -d "yesterday" +%F)
LOG=data/window_replay_log.txt
{
  echo "===== window replay for $D (generated $(date -u +%FT%TZ)) ====="
  /usr/bin/python3 tools/replay_windows.py "$D"
  echo
} >> "$LOG" 2>&1
