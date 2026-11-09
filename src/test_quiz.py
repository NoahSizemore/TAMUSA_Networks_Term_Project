#!/usr/bin/env python3
"""End-to-end test of the quiz, as four real processes.

This is the application half's equivalent of the conformance checklist.  It
starts a host and three clients as separate processes, the way they run in
the demo, lets them play, kills one client and restarts it under the same
name, and then checks what each process printed.

    python3 test_quiz.py                      # over the real transport
    python3 test_quiz.py --loss 0.1           # with losses
    python3 test_quiz.py --stub               # over the stub transport
    python3 test_quiz.py --keep               # leave the logs on disk

The three clients run at different TRANSPORT_DELAY values, which is the only
way the adjusted ranking can differ from the raw one.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))        # src/
APP = os.path.join(HERE, "app")                          # src/app/
STUB = os.path.join(HERE, "stub")                        # src/stub/
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

CLIENTS = [
    # name, one-way TRANSPORT_DELAY in ms
    ("wisconsin", 2.0),
    ("clemson", 25.0),
    ("utah", 45.0),
]


def strip(text):
    return ANSI.sub("", text)


class Process:
    def __init__(self, label, argv, env, directory):
        self.label = label
        self.path = os.path.join(directory, label + ".log")
        self.handle = open(self.path, "w+", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            argv,
            stdout=self.handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            cwd=APP,
        )

    def stop(self, hard=False):
        if self.proc.poll() is None:
            if hard:
                self.proc.kill()
            else:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)

    def text(self):
        self.handle.flush()
        with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
            return strip(fh.read())


def environment(loss, delay, stub, window):
    env = dict(os.environ)
    env["TRANSPORT_LOSS"] = str(loss)
    env["TRANSPORT_DELAY"] = str(delay)
    env["TRANSPORT_WINDOW"] = str(window)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["COLUMNS"] = "100"
    env["LINES"] = "30"
    # The application directory holds no transport.py, so this is what picks
    # the implementation.  A script's own directory comes first on sys.path,
    # which is why the application cannot live next to the real transport.
    env["PYTHONPATH"] = STUB if stub else HERE
    return env


def check(results, name, ok, detail):
    results.append((name, ok, detail))
    print("[%s] %-34s %s" % ("PASS" if ok else "FAIL", name, detail))
    sys.stdout.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--loss", type=float, default=0.0)
    parser.add_argument("--window", type=int, default=1)
    parser.add_argument("--port", type=int, default=9510)
    parser.add_argument("--stub", action="store_true",
                        help="run the application over stub/transport.py")
    parser.add_argument("--play", type=float, default=22.0,
                        help="seconds of play before the rejoin test")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)

    directory = tempfile.mkdtemp(prefix="quiz-test-")
    print("quiz end-to-end test")
    print("transport: %s   loss %.2f   window %d"
          % ("stub/transport.py" if args.stub else "transport.py",
             args.loss, args.window))
    print("logs in %s" % directory)
    print()

    results = []
    procs = []
    host = None
    try:
        host_delay = 5.0 if args.stub else 0.0
        host = Process(
            "host",
            [sys.executable, "quiz_host.py", "--port", str(args.port),
             "--auto", "--gap", "2", "--limit", "7"],
            environment(args.loss, host_delay, args.stub, args.window),
            directory,
        )
        procs.append(host)
        time.sleep(1.5)

        for name, delay in CLIENTS:
            proc = Process(
                "client-" + name,
                [sys.executable, os.path.join(APP, "quiz_client.py"),
                 "--host", "127.0.0.1",
                 "--port", str(args.port), "--name", name, "--autoplay",
                 "--think", "0.4", "2.0"],
                environment(args.loss, delay, args.stub, args.window),
                directory,
            )
            procs.append(proc)
            time.sleep(0.7)

        time.sleep(args.play)

        # --- the disconnect and rejoin requirement ---------------------
        victim = procs[2]  # clemson
        victim_name = CLIENTS[1][0]
        host_before = host.text()
        score_before = None
        for line in host_before.splitlines():
            match = re.search(r"^\s*%s\s+(\d+)" % victim_name, line)
            if match:
                score_before = int(match.group(1))
        victim.stop(hard=True)  # killed, so it never sends LEAVE
        print("killed client %s (score was %s), restarting it"
              % (victim_name, score_before))
        time.sleep(6.0)

        rejoined = Process(
            "client-" + victim_name + "-again",
            [sys.executable, os.path.join(APP, "quiz_client.py"),
             "--host", "127.0.0.1",
             "--port", str(args.port), "--name", victim_name, "--autoplay",
             "--think", "0.4", "2.0"],
            environment(args.loss, CLIENTS[1][1], args.stub, args.window),
            directory,
        )
        procs.append(rejoined)
        time.sleep(16.0)

        # --- collect ---------------------------------------------------
        host_text = host.text()
        client_texts = {p.label: p.text() for p in procs if p is not host}

        for proc in procs:
            proc.stop()
        time.sleep(0.4)
        host_text = host.text()
        client_texts = {p.label: p.text() for p in procs if p is not host}

        # --- assertions ------------------------------------------------
        all_text = host_text + "".join(client_texts.values())

        tracebacks = [
            label for label, text in
            list(client_texts.items()) + [("host", host_text)]
            if "Traceback (most recent call last)" in text
        ]
        check(results, "no process crashed",
              not tracebacks,
              "clean" if not tracebacks else "tracebacks in " + ", ".join(tracebacks))

        joined = [name for name, _d in CLIENTS if "%s joined" % name in host_text]
        check(results, "all three clients joined",
              len(joined) == 3, "%d of 3: %s" % (len(joined), ", ".join(joined)))

        answers = len(set(re.findall(
            r"\w+ answered [ABCD] \((?:correct|wrong)\) raw [\d.]+ ms adj [\d.]+ ms",
            host_text)))
        check(results, "the host scored answers",
              answers >= 3, "%d answers recorded" % answers)

        asked = len(set(re.findall(r"asked question \d+ of \d+", host_text)))
        check(results, "questions were asked", asked >= 2,
              "%d questions asked" % asked)

        # The status line must show rtt, resent and dropped for each peer.
        status = re.findall(  # one hit per redraw, so these repeat
            r"peer\s+\d+\s+\S+\s+rtt\s+([\d.]+) ms\s+resent\s+(\d+)\s+dropped\s+(\d+)",
            host_text,
        )
        check(results, "status line shows rtt/resent/dropped",
              len(status) >= 3, "%d status lines drawn" % len(status))

        rtts = sorted({float(s[0]) for s in status})
        if args.stub:
            # The stub does not measure anything: it reports twice its own
            # TRANSPORT_DELAY, the same number for every peer, however far
            # away that peer is.  That is the number the status line shows.
            expected = 2 * host_delay
            check(results, "stub reports a constant rtt, not a measurement",
                  rtts and all(abs(r - expected) < 0.01 for r in rtts),
                  "every peer reads %.1f ms (= 2 x %.1f)" % (expected, host_delay)
                  if rtts else "no status lines")
        else:
            # The real transport times each peer separately, so three clients
            # at 2, 25 and 45 ms of one-way delay must read back differently.
            spread = (rtts[-1] - rtts[0]) if rtts else 0.0
            check(results, "rtt_ms is a per-peer measurement",
                  spread > 20.0,
                  "%.1f to %.1f ms across peers" % (rtts[0], rtts[-1])
                  if rtts else "no status lines")

        loaded_stub = "transport: STUB" in host_text
        check(results, "the intended transport was loaded",
              loaded_stub == args.stub,
              "stub" if loaded_stub else "real transport")

        went_away = "went away" in host_text
        rejoin_line = re.search(r"(\w+) rejoined, score (\d+) kept", host_text)
        if args.stub:
            # The stub has no acknowledgments, so it can never report a peer
            # closed, and the host goes on believing the killed client holds
            # its name.  Documenting that difference is the point of running
            # the application over both.
            check(results, "stub cannot report the peer closed",
                  not went_away,
                  "as expected: the stub never closes a peer")
            contested = "is contested" in host_text
            check(results, "rejoin is refused over the stub",
                  contested and rejoin_line is None,
                  "name stayed held, client retried"
                  if contested else "unexpected: %s" % (rejoin_line and
                                                        rejoin_line.group(0)))
        else:
            check(results, "the killed client was reported gone",
                  went_away,
                  "host logged it" if went_away else "host never noticed")
            check(results, "score survived the rejoin",
                  rejoin_line is not None,
                  rejoin_line.group(0) if rejoin_line else "no rejoin logged")

        # Both rankings must reach the clients.
        ranks = re.search(r"raw rank = arrival order", all_text)
        check(results, "clients render both rankings",
              ranks is not None,
              "scoreboard header present" if ranks else "missing")

        scored = len(set(re.findall(r"scored \d+, total \d+", all_text)))
        check(results, "clients received their results",
              scored >= 2, "%d RESULT messages rendered" % scored)

        if args.loss > 0 and not args.stub:
            resends = max((int(s[1]) for s in status), default=0)
            check(results, "losses produced retransmissions",
                  resends > 0, "%d resends on the busiest peer" % resends)

        print()
        failed = [name for name, ok, _d in results if not ok]
        print("%d of %d checks passed" % (len(results) - len(failed), len(results)))
        if failed:
            print("failed: %s" % ", ".join(failed))
            print("logs kept in %s" % directory)
        return 1 if failed else 0
    finally:
        for proc in procs:
            proc.stop(hard=True)
        for proc in procs:
            try:
                proc.handle.close()
            except Exception:
                pass
        if not args.keep and not any(not ok for _n, ok, _d in results):
            shutil.rmtree(directory, ignore_errors=True)
        else:
            print("logs in %s" % directory)


if __name__ == "__main__":
    sys.exit(main())
