#!/usr/bin/env bash
# Full global run: all countries, all listings, all images, all exports.
# Usage: ./run_full_global.sh [TARGET_DIR]
set -u
TARGET="${1:-/mnt/games/Temp-Playground/PreOwned-Full}"
cd /home/leon/Dokumente/Coding/Ferrari-Project/modena-maestro-v2/3-PreOwned-API/ferrari-preowned-client

echo "=================================================================="
echo " Ferrari Pre-Owned — FULL GLOBAL RUN"
echo " target: $TARGET"
echo " start:  $(date)"
echo "=================================================================="

python3 -m ferrari_preowned_client.cli full "$TARGET" \
    --budget 20000 \
    --pace 6 \
    --workers 2 \
    --seed 424242
rc=$?

echo "=================================================================="
echo " exit code: $rc"
echo " finished: $(date)"
echo "=================================================================="
exit $rc
