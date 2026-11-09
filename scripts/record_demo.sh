#!/bin/sh
# Record the asciinema cast that stands in for the demo. 
# Run on the CloudLab VM, in a terminal of at least 100x30.
#
#   scripts/record_demo.sh
#
# What it records, in one pass:
#   1. the quiz over the real transport at TRANSPORT_LOSS=0
#   2. the same at TRANSPORT_LOSS=0.1, with the status line showing resends
#   3. the conformance checklist at all three loss settings
#   4. the 100-message burst measurement, stop-and-wait against a window of 8
#
# The clients run with --autoplay so one person can record it.  Drive it by
# hand for the live demo.

set -e
cd "$(dirname "$0")/.."
mkdir -p documentation/report

CAST="documentation/report/demo_$(date +%Y-%m-%d_%H%M).cast"
PORT=${PORT:-9000}

if ! command -v asciinema >/dev/null 2>&1; then
  echo "asciinema is not installed.  On the CloudLab image:"
  echo "  sudo apt-get update && sudo apt-get install -y asciinema"
  exit 1
fi

cat > /tmp/quiz_demo_inner.sh <<'INNER'
#!/bin/sh
set -e
cd "$(dirname "$0")" 2>/dev/null || true
cd "$DEMO_ROOT"

banner() {
  printf '\n\033[7m  %-76s\033[0m\n\n' "$1"
  sleep 2
}

play() {
  LOSS="$1"
  banner "QUIZ OVER transport.py AT TRANSPORT_LOSS=$LOSS"
  TRANSPORT_LOSS=$LOSS python3 src/quiz_host.py --port $PORT --auto --gap 3 --limit 8 &
  HOST=$!
  sleep 2
  TRANSPORT_LOSS=$LOSS TRANSPORT_DELAY=2  python3 src/quiz_client.py --port $PORT --name wisconsin --autoplay >/dev/null 2>&1 &
  C1=$!
  TRANSPORT_LOSS=$LOSS TRANSPORT_DELAY=25 python3 src/quiz_client.py --port $PORT --name clemson   --autoplay >/dev/null 2>&1 &
  C2=$!
  TRANSPORT_LOSS=$LOSS TRANSPORT_DELAY=45 python3 src/quiz_client.py --port $PORT --name utah      --autoplay >/dev/null 2>&1 &
  C3=$!
  sleep 35
  kill $C1 $C2 $C3 $HOST 2>/dev/null || true
  wait 2>/dev/null || true
  clear
}

play 0
play 0.1

banner "CONFORMANCE CHECKLIST, TRANSPORT_LOSS = 0, 0.1, 0.2"
python3 src/test_conformance.py

banner "100-MESSAGE BURST: STOP-AND-WAIT AGAINST A WINDOW OF 8"
python3 src/bench_burst.py --count 100 --windows 1 8 --delays 0 17.5

banner "DONE"
INNER
chmod +x /tmp/quiz_demo_inner.sh

DEMO_ROOT="$(pwd)" PORT="$PORT" asciinema rec "$CAST" \
  --title "CSCI 4306 quiz over our own transport" \
  --command "DEMO_ROOT='$(pwd)' PORT='$PORT' /tmp/quiz_demo_inner.sh"

echo
echo "cast written to $CAST"
echo "play it back with:  asciinema play $CAST"
