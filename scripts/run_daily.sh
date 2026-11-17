#!/usr/bin/env bash
# Daily production crawl for ferrari-preowned-client.
# Called by ~/.config/systemd/user/ferrari-preowned-crawl.service (03:00,
# Persistent=true catch-up). Safe to call manually at any time.
#
# Behavior:
#   - runs the smart incremental crawl against the production store
#   - VPN must be OFF: Ferrari's CloudFront blocks datacenter/VPN IPs.
#     The engine's pre-flight probe detects a block and exits 2 with an
#     actionable report — nothing else happens, the store is untouched.
#   - prune stays MANUAL until opted in: dry-run first with
#     'ferrari-preowned prune <target>'
#   - keeps the last 14 daily logs; removes anything older
set -euo pipefail

MODULE_DIR="/home/leon/Dokumente/Coding/Ferrari-Project/Modules/ferrari-preowned-client"
TARGET="/mnt/games/Temp-Playground/PreOwned-Full"
LOG_DIR="$TARGET/logs"
PY=/home/leon/.pyenv/versions/3.12.12/bin/python3
KEEP=14

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/daily_$(date +%Y-%m-%d).log"

cd "$MODULE_DIR"
"$PY" -m ferrari_preowned_client.cli smart "$TARGET" \
    --budget 20000 --pace 6 --workers 4 --seed 424242 \
    >> "$LOG" 2>&1
status=$?
echo "exit=$status $(date -Is)" >> "$LOG"

# rotate: keep the newest $KEEP daily logs
ls -1t "$LOG_DIR"/daily_*.log 2>/dev/null | tail -n +$((KEEP + 1)) \
    | xargs -r rm --

exit $status
