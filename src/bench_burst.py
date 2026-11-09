#!/usr/bin/env python3
"""The 100-message burst measurement, stop-and-wait against a window.

100 messages back to back at TRANSPORT_DELAY=17.5, a 35 ms path.  Stop and
wait spends one round trip per message, so 100 of them; a window of 8 spends
about an eighth of that.

    python3 bench_burst.py                       # 0 ms and 17.5 ms, windows 1 and 8
    TRANSPORT_DELAY=27.5 python3 bench_burst.py --delays env
    python3 bench_burst.py --count 100 --windows 1 4 8 16

Both endpoints live in this one process and are driven in turn, with no
threads, so the figure includes the application's own polling and is not an
optimistic lower bound.

Expected shape, from the project description:

    round trip      stop-and-wait     window of 8
    0.8 ms              0.08 s           0.01 s
    35 ms  (17.5)       3.50 s           0.46 s
    55 ms  (27.5)       5.50 s           0.71 s
"""

import argparse
import os
import select
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PORT = 9450


def run_once(delay_ms, window, count, loss=0.0):
    """Send `count` messages one way and return how long they took to arrive."""
    os.environ["TRANSPORT_DELAY"] = str(delay_ms)
    os.environ["TRANSPORT_WINDOW"] = str(window)
    os.environ["TRANSPORT_LOSS"] = str(loss)
    sys.modules.pop("transport", None)
    import transport as tp

    global PORT
    PORT += 1
    host = tp.Transport()
    host.bind(PORT)
    client = tp.Transport()
    peer = client.connect("127.0.0.1", PORT, timeout=0)

    received = []

    def pump(until, limit):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            progress = False
            while True:
                got = client.recv(timeout=0)
                if got is None:
                    break
                progress = True
            while True:
                got = host.recv(timeout=0)
                if got is None:
                    break
                received.append(got[1])
                progress = True
            if until():
                return True
            if not progress:
                wait = min(client.next_event_in(), host.next_event_in(), 0.05)
                if wait >= (0.016 if os.name == "nt" else 0.0):
                    try:
                        select.select([client._sock, host._sock], [], [], wait)
                    except (OSError, ValueError):
                        pass
        return until()

    try:
        if not pump(lambda: peer in client.peers() and host.peers(), 20.0):
            raise TimeoutError("handshake did not finish")

        payload = b"x" * 512
        started = time.monotonic()
        for index in range(count):
            client.send(peer, payload + b"%04d" % index)
        ok = pump(lambda: len(received) >= count, 300.0)
        elapsed = time.monotonic() - started
        if not ok:
            raise TimeoutError("only %d of %d arrived" % (len(received), count))
        st = client.stats(peer)
        return elapsed, st
    finally:
        host.close(linger=0.05)
        client.close(linger=0.05)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--windows", type=int, nargs="+", default=[1, 8])
    parser.add_argument("--delays", nargs="+", default=["0", "17.5"],
                        help="one-way delays in ms, or 'env'")
    parser.add_argument("--loss", type=float, default=0.0)
    args = parser.parse_args(argv)

    if args.delays == ["env"]:
        delays = [float(os.environ.get("TRANSPORT_DELAY") or 0.0)]
    else:
        delays = [float(d) for d in args.delays]

    print("%d messages of 516 bytes, back to back, one process, no threads"
          % args.count)
    print("python %s on %s, loss %.2f" % (sys.version.split()[0], sys.platform,
                                          args.loss))
    print()
    print("%-12s %-10s %10s %10s %9s %9s"
          % ("one-way", "round trip", "window", "elapsed", "msg/s", "resent"))
    print("-" * 68)

    table = {}
    for delay in delays:
        for window in args.windows:
            try:
                elapsed, st = run_once(delay, window, args.count, args.loss)
            except Exception as exc:
                print("%-12s %-10s %10d %10s  %s"
                      % ("%.1f ms" % delay, "-", window, "FAILED", exc))
                continue
            table[(delay, window)] = elapsed
            print("%-12s %-10s %10d %9.3fs %9.1f %9d"
                  % ("%.1f ms" % delay,
                     "%.1f ms" % st["rtt_ms"],
                     window,
                     elapsed,
                     args.count / elapsed if elapsed else 0.0,
                     st["resent"]))

    if len(args.windows) > 1:
        print()
        base = args.windows[0]
        for delay in delays:
            if (delay, base) not in table:
                continue
            for window in args.windows[1:]:
                if (delay, window) not in table:
                    continue
                speedup = table[(delay, base)] / max(table[(delay, window)], 1e-9)
                print("at %.1f ms one-way, a window of %d beats a window of %d "
                      "by %.1fx" % (delay, window, base, speedup))
    return 0


if __name__ == "__main__":
    sys.exit(main())
