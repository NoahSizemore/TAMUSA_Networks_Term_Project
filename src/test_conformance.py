#!/usr/bin/env python3
"""Conformance checklist for transport.py.

Eight cases, run at TRANSPORT_LOSS = 0, 0.1 and 0.2.  Each case prints a line
saying whether it passed.

    python3 test_conformance.py                 # sweeps 0, 0.1, 0.2
    python3 test_conformance.py --loss 0.1      # one setting
    TRANSPORT_LOSS=0.1 python3 test_conformance.py --loss env
    python3 test_conformance.py --window 8      # the pipelined transport

Case 8 measures rtt_ms against a host on another CloudLab cluster, so it needs
a partner.  On the remote VM:

    python3 test_conformance.py --serve 9100

On this one:

    ping -c 5 dev.<name>.networksclass-pg0.<cluster>.cloudlab.us
    python3 test_conformance.py --peer dev.<name>...:9100 --ping-ms 34.8

Run it on an otherwise idle machine.  Case 4 at TRANSPORT_LOSS=0.2 spends most
of its time waiting out retransmission timeouts, and competing for the CPU
stretches it badly.

Without --peer, case 8 reports SKIP and says why.  Everything runs in a single
process with no threads: several Transport objects are driven in turn, which
is exactly the main loop clause 5 of the contract describes.
"""

import argparse
import select
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

CASES = {
    1: "a 1-byte message",
    2: "a 1024-byte message",
    3: "a 1025-byte message raises ValueError",
    4: "1000 messages back to back",
    5: "three peers connected at once",
    6: "recv(timeout=0.05) with no traffic",
    7: "a peer that stops answering",
    8: "stats(peer) against another cluster",
}

BASE_PORT = 9301

# Shortest interval this host can wait for accurately.  Windows rounds any
# select() timeout up to roughly 15.6 ms; Linux is accurate to tens of
# microseconds, so the spin branch below is never taken there.
_QUANTUM = 0.016 if __import__("os").name == "nt" else 0.0


# ---------------------------------------------------------------------------
# A tiny harness: endpoints that are pumped in turn, no threads
# ---------------------------------------------------------------------------


class Endpoint:
    """One Transport plus everything it has delivered so far."""

    def __init__(self, transport, name):
        self.t = transport
        self.name = name
        self.inbox = []
        self.live = True

    def drain(self):
        """Non-blocking: take everything the transport already holds."""
        got = False
        while self.live:
            msg = self.t.recv(timeout=0)
            if msg is None:
                return got
            self.inbox.append(msg)
            got = True
        return got

    def took(self, peer=None):
        if peer is None:
            return [data for _p, data in self.inbox]
        return [data for p, data in self.inbox if p == peer]


class Harness:
    def __init__(self):
        self.endpoints = []

    def add(self, transport, name):
        ep = Endpoint(transport, name)
        self.endpoints.append(ep)
        return ep

    def pump(self, until=None, limit=60.0):
        """Drive every live endpoint until `until` is true or time runs out.

        Returns True when `until` became true (or when there was no condition
        and the time budget was spent on purpose).
        """
        end = time.monotonic() + limit
        while True:
            busy = False
            for ep in self.endpoints:
                if ep.live and ep.drain():
                    busy = True
            if until is not None and until():
                return True
            if time.monotonic() >= end:
                return until is None
            if not busy:
                self._idle_wait(end)

    def _idle_wait(self, end):
        """Block until any endpoint has traffic or a timer of its own expires.

        One select() across every endpoint's socket, for exactly as long as
        the nearest retransmission timer allows.  Polling each endpoint in
        turn with a small timeout instead costs one scheduler quantum per
        endpoint per pass, which on a host that rounds select() up to 15 ms
        dominates the whole run.
        """
        socks = []
        wait = 0.050
        for ep in self.endpoints:
            if not ep.live:
                continue
            fd = ep.t.fileno()
            if fd >= 0:
                socks.append(ep.t._sock)
            wait = min(wait, ep.t.next_event_in())
        wait = max(0.0, min(wait, end - time.monotonic()))
        if not socks:
            return
        if wait < _QUANTUM:
            # A wait shorter than the scheduler's quantum cannot be slept
            # accurately: on Windows select() rounds any timeout up to about
            # 15.6 ms, so asking for 10 ms and getting 15.6 would stretch
            # every retransmission in the run.  Poll instead and come back
            # round the loop.  Linux does not need this, and the branch costs
            # nothing there because the waits it takes are already accurate.
            wait = 0.0
        try:
            select.select(socks, [], [], wait)
        except (OSError, ValueError):
            pass

    def shutdown(self):
        # Close every endpoint, pumping the others in between so the FINs and
        # their acknowledgments actually get serviced.
        for ep in self.endpoints:
            try:
                ep.t.close(linger=0.05)
            except Exception:
                pass
            for other in self.endpoints:
                if other is not ep and other.live:
                    try:
                        other.drain()
                    except Exception:
                        other.live = False
        self.endpoints = []


class Result:
    def __init__(self, number, status, detail, seconds=0.0):
        self.number = number
        self.status = status
        self.detail = detail
        self.seconds = seconds

    def line(self):
        return "[%s] case %d  %-40s %-46s %6.2fs" % (
            self.status,
            self.number,
            CASES[self.number],
            self.detail,
            self.seconds,
        )


def _port(offset):
    return BASE_PORT + offset


def _fresh(transport_module):
    return transport_module.Transport()


def dial(h, client, host_transport, port, limit=40.0):
    """Connect and drive both ends until the handshake has finished.

    connect(timeout=0) returns as soon as the SYN is out.  Everything in this
    program runs in one thread, so a blocking connect would stop us servicing
    the host and the handshake could never complete.
    """
    pid = client.connect("127.0.0.1", port, timeout=0)
    want = len(host_transport.peers()) + 1
    ok = h.pump(
        until=lambda: pid in client.peers() and len(host_transport.peers()) >= want,
        limit=limit,
    )
    if not ok:
        raise TimeoutError("handshake to 127.0.0.1:%d did not finish" % port)
    return pid


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def case_1_and_2(tp, number, size, port):
    """A message of `size` bytes arrives unchanged."""
    h = Harness()
    host = _fresh(tp)
    host.bind(port)
    client = _fresh(tp)
    hep = h.add(host, "host")
    cep = h.add(client, "client")
    try:
        peer = dial(h, client, host, port)
        payload = bytes((i * 7 + 13) % 256 for i in range(size))
        client.send(peer, payload)
        ok = h.pump(until=lambda: len(hep.inbox) >= 1, limit=30.0)
        if not ok:
            return Result(number, FAIL, "nothing arrived within 30 s")
        got = hep.inbox[0][1]
        if got != payload:
            return Result(
                number,
                FAIL,
                "payload differs (%d bytes in, %d out)" % (size, len(got)),
            )
        return Result(number, PASS, "%d bytes arrived unchanged" % size)
    finally:
        h.shutdown()


def case_3(tp, port):
    """1025 bytes raises ValueError, and nothing is sent."""
    h = Harness()
    host = _fresh(tp)
    host.bind(port)
    client = _fresh(tp)
    hep = h.add(host, "host")
    h.add(client, "client")
    try:
        peer = dial(h, client, host, port)
        before = client.stats(peer)["sent"]
        try:
            client.send(peer, b"x" * 1025)
        except ValueError:
            pass
        else:
            return Result(3, FAIL, "1025 bytes was accepted")

        after = client.stats(peer)["sent"]
        if after != before:
            return Result(3, FAIL, "the send counter moved: %d -> %d" % (before, after))

        # Give anything that might have leaked out a chance to arrive.
        h.pump(until=lambda: len(hep.inbox) > 0, limit=0.5)
        if hep.inbox:
            return Result(3, FAIL, "%d message(s) arrived anyway" % len(hep.inbox))

        # And 1024 still works, so the limit is a boundary, not a wall.
        client.send(peer, b"y" * 1024)
        if not h.pump(until=lambda: len(hep.inbox) >= 1, limit=30.0):
            return Result(3, FAIL, "1024 bytes stopped working after the refusal")
        return Result(3, PASS, "ValueError raised, nothing sent, 1024 still ok")
    finally:
        h.shutdown()


def case_4(tp, port, count=1000):
    """1000 messages back to back: all arrive, in order, once each."""
    h = Harness()
    host = _fresh(tp)
    host.bind(port)
    client = _fresh(tp)
    hep = h.add(host, "host")
    h.add(client, "client")
    try:
        peer = dial(h, client, host, port)
        for i in range(count):
            client.send(peer, ("msg-%04d" % i).encode())
        started = time.monotonic()
        # Generous, because this is the one case whose running time is set by
        # the loss setting rather than by the code: at TRANSPORT_LOSS=0.2 about
        # 500 of the 1000 messages need at least one retransmission, and each
        # of those waits out a retransmission timeout.  Expect roughly 15 s on
        # an idle Linux VM and a few times that on a loaded machine.
        budget = 600.0
        ok = h.pump(until=lambda: len(hep.inbox) >= count, limit=budget)
        elapsed = time.monotonic() - started
        got = hep.took()
        if not ok:
            return Result(
                4, FAIL,
                "only %d of %d arrived in %.0f s" % (len(got), count, budget),
                elapsed,
            )
        expected = [("msg-%04d" % i).encode() for i in range(count)]
        if got[:count] != expected:
            first_bad = next(
                (i for i in range(count) if got[i] != expected[i]), count
            )
            return Result(
                4, FAIL, "out of order or altered at index %d" % first_bad, elapsed
            )
        if len(got) != count:
            return Result(
                4, FAIL, "%d delivered, %d expected (duplicates)" % (len(got), count),
                elapsed,
            )
        st = host.stats(hep.inbox[0][0])
        cs = client.stats(peer)
        return Result(
            4,
            PASS,
            "%d in order, once each, %d resent" % (count, cs["resent"]),
            elapsed,
        )
    finally:
        h.shutdown()


def case_5(tp, port):
    """Three peers at once: each receives only its own messages."""
    h = Harness()
    host = _fresh(tp)
    host.bind(port)
    hep = h.add(host, "host")
    clients = []
    try:
        for i in range(3):
            c = _fresh(tp)
            ep = h.add(c, "client%d" % i)
            pid = dial(h, c, host, port)
            clients.append((c, ep, pid))

        if len(host.peers()) != 3:
            return Result(
                5, FAIL, "host sees %d peers, expected 3" % len(host.peers())
            )

        # Host to each client: a payload only that client should ever see.
        for peer in sorted(host.peers()):
            for k in range(5):
                host.send(peer, ("to-peer-%d-#%d" % (peer, k)).encode())

        # And each client to the host, so demultiplexing is checked both ways.
        for i, (c, _ep, pid) in enumerate(clients):
            c.send(pid, ("from-client-%d" % i).encode())

        want = 15 + 3
        ok = h.pump(
            until=lambda: sum(len(e.inbox) for _c, e, _p in clients) >= 15
            and len(hep.inbox) >= 3,
            limit=60.0,
        )
        if not ok:
            return Result(
                5,
                FAIL,
                "delivered %d of %d messages"
                % (sum(len(e.inbox) for _c, e, _p in clients) + len(hep.inbox), want),
            )

        for i, (_c, ep, _pid) in enumerate(clients):
            bodies = ep.took()
            if len(bodies) != 5:
                return Result(5, FAIL, "client %d got %d messages, expected 5" % (i, len(bodies)))
            tag = bodies[0].split(b"-#")[0]
            for b in bodies:
                if not b.startswith(tag):
                    return Result(
                        5, FAIL, "client %d received %r, meant for another peer" % (i, b)
                    )

        senders = {p for p, _d in hep.inbox}
        if len(senders) != 3:
            return Result(
                5, FAIL, "host attributed 3 messages to %d peers" % len(senders)
            )
        return Result(5, PASS, "3 peers, 18 messages, none crossed over")
    finally:
        h.shutdown()


def case_6(tp, port):
    """recv(timeout=0.05) with no traffic returns None in roughly 50 ms."""
    t = _fresh(tp)
    t.bind(port)
    try:
        samples = []
        for _ in range(5):
            started = time.monotonic()
            got = t.recv(timeout=0.05)
            samples.append(time.monotonic() - started)
            if got is not None:
                return Result(6, FAIL, "recv returned %r with no traffic" % (got,))
        worst = max(samples)
        best = min(samples)
        mean = sum(samples) / len(samples)
        # "Roughly" 50 ms.  The upper bound is generous because the Windows
        # scheduler quantises select() to about 15 ms; on CloudLab this lands
        # within a millisecond or two of 50.
        if best < 0.045:
            return Result(6, FAIL, "returned early, fastest was %.1f ms" % (best * 1000))
        if worst > 0.200:
            return Result(6, FAIL, "slowest was %.1f ms" % (worst * 1000))
        return Result(
            6,
            PASS,
            "None after %.1f ms mean (%.1f-%.1f)"
            % (mean * 1000, best * 1000, worst * 1000),
        )
    finally:
        t.close()


def case_7(tp, port):
    """A peer that stops answering is reported closed; the others keep working."""
    h = Harness()
    host = _fresh(tp)
    host.bind(port)
    hep = h.add(host, "host")
    kept = []
    try:
        silent_ep = None
        silent_pid = None
        for i in range(3):
            c = _fresh(tp)
            ep = h.add(c, "client%d" % i)
            pid = dial(h, c, host, port)
            if i == 2:
                silent_ep, silent_pid = ep, pid
            else:
                kept.append((c, ep, pid))

        peers_before = sorted(host.peers())
        if len(peers_before) != 3:
            return Result(7, FAIL, "only %d peers connected" % len(peers_before))

        # Clause 5 is the whole trick: this endpoint stops calling send() and
        # recv(), so it stops acknowledging.  Nothing else about it changes.
        silent_ep.live = False
        silent_peer_id = next(
            p for p in peers_before
            if host.stats(p)["addr"].endswith(
                ":%d" % silent_ep.t._sock.getsockname()[1]
            )
        )

        for peer in peers_before:
            host.send(peer, b"are you there")

        started = time.monotonic()
        ok = h.pump(
            until=lambda: silent_peer_id not in host.peers(), limit=90.0
        )
        elapsed = time.monotonic() - started
        if not ok:
            return Result(
                7, FAIL, "silent peer still listed after 90 s", elapsed
            )

        still = sorted(host.peers())
        if len(still) != 2:
            return Result(
                7, FAIL, "%d peers survived, expected 2" % len(still), elapsed
            )

        # The survivors must still carry traffic both ways.
        for peer in still:
            host.send(peer, b"still here")
        for _c, _ep, pid in kept:
            _c.send(pid, b"yes")
        base_host = len(hep.inbox)
        ok = h.pump(
            until=lambda: all(len(e.inbox) >= 2 for _c, e, _p in kept)
            and len(hep.inbox) >= base_host + 2,
            limit=60.0,
        )
        if not ok:
            return Result(
                7, FAIL, "the surviving peers stopped carrying traffic", elapsed
            )
        reason = host.why_closed(silent_peer_id) if hasattr(host, "why_closed") else ""
        return Result(
            7,
            PASS,
            "closed after %.1f s (%s); 2 peers kept working"
            % (elapsed, (reason or "")[:28]),
            elapsed,
        )
    finally:
        h.shutdown()


def case_8(tp, peer_spec, ping_ms, port):
    """stats(peer)['rtt_ms'] against a host on another cluster."""
    if not peer_spec:
        return Result(
            8,
            SKIP,
            "no --peer given; needs a VM on another cluster",
        )
    host_name, _, port_text = peer_spec.rpartition(":")
    if not host_name:
        return Result(8, FAIL, "--peer wants host:port, got %r" % peer_spec)

    h = Harness()
    t = _fresh(tp)
    ep = h.add(t, "local")
    try:
        started = time.monotonic()
        pid = t.connect(host_name, int(port_text))
        # Twenty round trips, so the smoothed estimate has settled.
        for i in range(20):
            t.send(pid, b"rtt-probe-%03d" % i)
            if not h.pump(until=lambda n=i + 1: len(ep.inbox) >= n, limit=15.0):
                return Result(
                    8,
                    FAIL,
                    "the responder stopped answering after %d probes" % i,
                    time.monotonic() - started,
                )
        measured = t.stats(pid)["rtt_ms"]
        elapsed = time.monotonic() - started
        if ping_ms is None:
            return Result(
                8,
                SKIP,
                "rtt_ms = %.2f ms; rerun with --ping-ms to assert" % measured,
                elapsed,
            )
        lo, hi = ping_ms / 2.0, ping_ms * 2.0
        status = PASS if lo <= measured <= hi else FAIL
        return Result(
            8,
            status,
            "rtt_ms %.2f vs ping %.2f (band %.2f-%.2f)" % (measured, ping_ms, lo, hi),
            elapsed,
        )
    finally:
        h.shutdown()


def serve(port):
    """Echo responder for case 8.  Run this on the VM in the other cluster."""
    import transport as tp

    t = tp.Transport()
    t.bind(port)
    print("case-8 responder listening on UDP %d, ctrl-C to stop" % port)
    print("loss=%.3f delay=%.1f ms window=%d" % (t.loss, t.delay * 1000, t.window))
    try:
        while True:
            msg = t.recv(timeout=0.5)
            if msg is None:
                continue
            peer, data = msg
            t.send(peer, data)  # straight back, so the client can time it
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        t.close()


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run_suite(loss, window, args, run_number):
    """Import transport.py fresh so it re-reads the environment, then run."""
    os.environ["TRANSPORT_LOSS"] = str(loss)
    os.environ["TRANSPORT_WINDOW"] = str(window)
    for stale in [m for m in sys.modules if m == "transport"]:
        del sys.modules[stale]
    import transport as tp

    print()
    print("=" * 108)
    print(
        "TRANSPORT_LOSS=%s   TRANSPORT_DELAY=%s   TRANSPORT_WINDOW=%s"
        % (loss, os.environ.get("TRANSPORT_DELAY", "0"), window)
    )
    print("=" * 108)

    base = run_number * 40
    results = []
    plan = [
        lambda: case_1_and_2(tp, 1, 1, _port(base + 1)),
        lambda: case_1_and_2(tp, 2, 1024, _port(base + 2)),
        lambda: case_3(tp, _port(base + 3)),
        lambda: case_4(tp, _port(base + 4), count=args.count),
        lambda: case_5(tp, _port(base + 5)),
        lambda: case_6(tp, _port(base + 6)),
        lambda: case_7(tp, _port(base + 7)),
        lambda: case_8(tp, args.peer, args.ping_ms, _port(base + 8)),
    ]
    for number, run in enumerate(plan, start=1):
        if args.only and number not in args.only:
            continue
        started = time.monotonic()
        try:
            result = run()
        except Exception as exc:  # a crash is a failure, not an exit
            result = Result(number, FAIL, "%s: %s" % (type(exc).__name__, exc))
        if not result.seconds:
            result.seconds = time.monotonic() - started
        print(result.line())
        sys.stdout.flush()
        results.append(result)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--loss",
        default="sweep",
        help="'sweep' (0, 0.1, 0.2), 'env' (whatever TRANSPORT_LOSS says), "
        "or a number",
    )
    parser.add_argument("--window", type=int, default=None, help="TRANSPORT_WINDOW")
    parser.add_argument("--count", type=int, default=1000, help="messages in case 4")
    parser.add_argument("--only", type=int, nargs="+", help="run only these cases")
    parser.add_argument("--peer", help="host:port of a responder for case 8")
    parser.add_argument("--ping-ms", type=float, help="ping time to that host, in ms")
    parser.add_argument(
        "--serve", type=int, metavar="PORT", help="run the case-8 responder and exit"
    )
    args = parser.parse_args(argv)

    if args.serve:
        serve(args.serve)
        return 0

    if args.loss == "sweep":
        settings = [0, 0.1, 0.2]
    elif args.loss == "env":
        settings = [float(os.environ.get("TRANSPORT_LOSS") or 0)]
    else:
        settings = [float(args.loss)]

    window = args.window
    if window is None:
        window = int(os.environ.get("TRANSPORT_WINDOW") or 1)

    print("conformance checklist for transport.py")
    print("python %s on %s" % (sys.version.split()[0], sys.platform))
    print("started %s" % time.strftime("%Y-%m-%d %H:%M:%S"))

    table = {}
    failures = 0
    for index, loss in enumerate(settings):
        results = run_suite(loss, window, args, index)
        table[loss] = {r.number: r for r in results}
        failures += sum(1 for r in results if r.status == FAIL)

    print()
    print("summary  (rows are cases, columns are TRANSPORT_LOSS)")
    print("-" * 108)
    header = "%-44s" % "case"
    for loss in settings:
        header += "%14s" % ("loss=%s" % loss)
    print(header)
    for number in sorted(CASES):
        if args.only and number not in args.only:
            continue
        row = "%-44s" % ("%d  %s" % (number, CASES[number]))
        for loss in settings:
            r = table[loss].get(number)
            row += "%14s" % (r.status if r else "-")
        print(row)
    print("-" * 108)
    print("%d failure(s)" % failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
