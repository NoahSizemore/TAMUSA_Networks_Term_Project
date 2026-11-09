#!/bin/sh
# Run the conformance checklist at all three loss settings and keep the raw
# output for the report's appendix.  Run this on the CloudLab VM.
#
#   scripts/run_checklist.sh                       seven local cases
#   scripts/run_checklist.sh dev.quiz.networksclass-pg0.clemson.cloudlab.us:9100 34.8
#                                                  all eight, with case 8 live
#
# Case 8 needs a responder on a VM in another cluster:
#   python3 src/test_conformance.py --serve 9100

set -e
cd "$(dirname "$0")/.."

PEER="$1"
PING_MS="$2"
STAMP=$(date +%Y-%m-%d_%H%M)
OUT="documentation/report/appendix_checklist_${STAMP}.txt"
mkdir -p documentation/report

ARGS=""
if [ -n "$PEER" ]; then
  ARGS="--peer $PEER"
  [ -n "$PING_MS" ] && ARGS="$ARGS --ping-ms $PING_MS"
fi

{
  echo "host:   $(hostname)"
  echo "kernel: $(uname -srm)"
  echo "python: $(python3 --version 2>&1)"
  echo "date:   $(date -u) (UTC)"
  if [ -n "$PEER" ]; then
    echo
    echo "ping to $PEER:"
    ping -c 5 "$(echo "$PEER" | cut -d: -f1)" 2>&1 || true
  fi
  echo
  # The sweep sets TRANSPORT_LOSS itself, once per pass, and reports which
  # setting each block of results belongs to.
  python3 src/test_conformance.py $ARGS
} 2>&1 | tee "$OUT"

echo
echo "written to $OUT"
