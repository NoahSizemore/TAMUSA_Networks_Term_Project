#!/usr/bin/env python3
"""Live quiz, client side.  Protocol v1, documentation/protocol.md.

From the repository root:

    PYTHONPATH=src      python3 src/app/quiz_client.py --name noah
    PYTHONPATH=src/stub python3 src/app/quiz_client.py --name noah
    TRANSPORT_LOSS=0.1 TRANSPORT_DELAY=17.5 PYTHONPATH=src         python3 src/app/quiz_client.py --name noah

Keys:  A B C D  or  1 2 3 4   answer        r  repaint        q  quit

Set TRANSPORT_DELAY to different values at different clients and the adjusted
ranking starts to differ from the raw one, which is the whole point of it.

As on the host, the only module that touches the network is transport.py, and
recv() is called on every pass through the loop so the transport's timers keep
running.
"""

import argparse
import os
import random
import sys
import time

import terminal
import transport

def transport_path():
    """Where the transport module came from, stub or real."""
    path = getattr(transport, "__file__", "?") or "?"
    kind = "STUB" if os.path.basename(os.path.dirname(path)) == "stub" else "REAL"
    # Kind first: this line is drawn into a fixed-width row and a long
    # absolute path would push the part that matters off the end.
    return "%s %s" % (kind, os.sep.join(path.split(os.sep)[-2:]))


PROTOCOL_VERSION = 1
NAME_MAX = 16
PING_EVERY = 2.0
STATUS_EVERY = 0.2
JOIN_RETRY = 2.0  # seconds between JOIN attempts when the name is contested
JOIN_GIVE_UP = 40.0  # stop trying after this long


def valid_name(name):
    return (
        1 <= len(name) <= NAME_MAX
        and all(c.isalnum() or c in "-_" for c in name)
        and name.isascii()
    )


class Row:
    """One scoreboard row as the host sent it."""

    __slots__ = ("name", "score", "online", "answered", "raw_ms", "adj_ms",
                 "rank_raw", "rank_adj", "correct")

    @classmethod
    def parse(cls, text):
        parts = text.split(";")
        if len(parts) != 9:
            return None
        row = cls()
        try:
            row.name = parts[0]
            row.score = int(parts[1])
            row.online = parts[2] == "1"
            row.answered = parts[3] == "1"
            row.raw_ms = float(parts[4])
            row.adj_ms = float(parts[5])
            row.rank_raw = int(parts[6])
            row.rank_adj = int(parts[7])
            row.correct = parts[8]
        except ValueError:
            return None
        return row


class QuizClient:
    def __init__(self, host, port, name, screen, autoplay=False, think=None):
        self.t = transport.Transport()
        self.name = name
        self.screen = screen
        self.server = "%s:%d" % (host, port)
        # --autoplay answers after a random pause.  Like the host's --auto it
        # is a testing and recording aid: it lets three clients at three
        # different TRANSPORT_DELAY settings play at once, which is the only
        # way to see the adjusted ranking diverge from the raw one.
        self.autoplay = autoplay
        self.think_range = think or (0.3, 2.5)
        self.think_for = None

        self.peer = self.t.connect(host, port)
        self.joined = False
        self.questions = 0
        self.limit = 0

        self.qid = 0
        self.index = 0
        self.total = 0
        self.text = ""
        self.choices = []
        self.open = False
        self.my_choice = None
        self.asked_at = None

        self.result = None  # the last RESULT, as a dict
        self.reveal = None  # (correct, tally) once the question closes
        self.score_qid = 0
        self.rows = []
        self.log = []
        self.running = True
        self.last_ping = 0.0
        self.started = time.monotonic()
        self.join_retry_at = None
        self.join_attempts = 1

        self.note("connected to %s as peer %s" % (self.server, self.peer))
        self.note(
            "loss=%.2f delay=%.1f ms window=%d"
            % (self.t.loss, getattr(self.t, "delay", 0.0) * 1000,
               getattr(self.t, "window", 1))
        )
        # Which transport.py did we actually import?  The application cannot
        # tell the stub from the real one by behaviour alone at loss 0, and
        # the import is resolved by PYTHONPATH, so say it out loud.
        self.note("transport: %s" % transport_path())
        self.send("JOIN|%d|%s" % (PROTOCOL_VERSION, self.name))
        # Arm the retry straight away rather than waiting for an answer.  Over
        # the real transport a JOIN cannot be lost, but over the stub it can
        # simply vanish, and a client that only retried on ERROR would sit at
        # "joining" for ever with nothing on screen to say why.
        self.join_retry_at = time.monotonic() + JOIN_RETRY

    # -- plumbing --------------------------------------------------------

    def note(self, text):
        self.log.append("%s  %s" % (time.strftime("%H:%M:%S"), text))
        del self.log[:-8]

    def send(self, text):
        try:
            self.t.send(self.peer, text.encode("utf-8"))
            return True
        except (transport.PeerClosed, ConnectionError, ValueError) as exc:
            self.note("send failed: %s" % exc)
            return False

    def answer(self, letter):
        if not self.open:
            self.note("no question is open")
            return
        if self.my_choice is not None:
            self.note("you already answered %s" % self.my_choice)
            return
        self.my_choice = letter
        self.send("ANSWER|%d|%s" % (self.qid, letter))
        self.note("sent answer %s" % letter)

    # -- inbound ---------------------------------------------------------

    def handle(self, data):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            self.note("undecodable message from the host")
            return
        fields = text.split("|")
        kind = fields[0]

        if kind == "WELCOME" and len(fields) == 5:
            if fields[1] != str(PROTOCOL_VERSION):
                self.note("host speaks v%s, this client speaks v%d"
                          % (fields[1], PROTOCOL_VERSION))
                self.running = False
                return
            self.joined = True
            self.name = fields[2]
            self.questions = int(fields[3])
            self.limit = int(fields[4])
            self.note("joined as %s, %d questions" % (self.name, self.questions))

        elif kind == "QUESTION" and len(fields) == 10:
            self.qid = int(fields[1])
            self.index = int(fields[2])
            self.total = int(fields[3])
            self.limit = int(fields[4])
            self.text = fields[5]
            self.choices = fields[6:10]
            self.open = True
            self.my_choice = None
            self.result = None
            self.reveal = None
            self.asked_at = time.monotonic()
            self.think_for = random.uniform(*self.think_range)
            self.note("question %d arrived" % self.index)

        elif kind == "RESULT" and len(fields) == 9:
            self.result = {
                "qid": int(fields[1]),
                "correct": fields[2],
                "yours": fields[3],
                "ok": fields[4] == "1",
                "raw_ms": float(fields[5]),
                "adj_ms": float(fields[6]),
                "points": int(fields[7]),
                "total": int(fields[8]),
            }
            self.note("scored %d, total %d" % (self.result["points"],
                                               self.result["total"]))

        elif kind == "SCORE" and len(fields) >= 3:
            self.score_qid = int(fields[1])
            count = int(fields[2])
            self.rows = [r for r in (Row.parse(f) for f in fields[3:3 + count])
                         if r is not None]

        elif kind == "REVEAL" and len(fields) == 4:
            self.open = False
            self.reveal = (fields[2], fields[3].split(";"))
            self.note("question %d closed, answer %s" % (self.index, fields[2]))

        elif kind == "ERROR" and len(fields) >= 3:
            self.note("host refused it: %s (%s)" % (fields[2], fields[1]))
            if fields[1] == "1":
                # The name is held by a peer the host has not yet given up
                # on, which is what happens when this client was killed and
                # restarted: its own earlier connection is still listed.
                # Keep asking.  protocol.md 6 requires the retry.
                self.join_retry_at = time.monotonic() + JOIN_RETRY
                self.note("name in use, asking again in %.0f s" % JOIN_RETRY)
            elif fields[1] == "3":
                pass  # already answered; our own view is already right
            elif fields[1] == "4":
                self.running = False

        elif kind == "BYE":
            self.note("host said goodbye: %s" % ("|".join(fields[1:]) or "?"))
            self.running = False

        elif kind == "PONG":
            pass

        else:
            self.note("unexpected message %r" % kind)

    # -- drawing ---------------------------------------------------------

    def draw(self):
        s = self.screen
        s.clear()
        width = s.cols
        state = "answered %s" % self.my_choice if self.my_choice else (
            "OPEN" if self.open else "waiting")
        s.put(1, terminal.REVERSE + terminal.pad(
            " LIVE QUIZ - %s   host %s   question %d/%d   %s"
            % (self.name, self.server, self.index, self.total or self.questions,
               state), width))

        row = 3
        if not self.joined:
            s.put(row, terminal.FG["yellow"] + "Joining as %s..." % self.name)
            row += 2
        elif self.index == 0:
            s.put(row, terminal.DIM + "Waiting for the host to ask a question.")
            row += 2

        if self.index:
            s.put(row, terminal.BOLD + "Q%d. %s" % (self.index, self.text))
            row += 1
            correct = self.reveal[0] if self.reveal else None
            for letter, choice in zip("ABCD", self.choices):
                prefix = "    "
                colour = terminal.RESET
                if self.my_choice == letter:
                    prefix = "  > "
                    colour = terminal.BOLD
                if correct == letter:
                    colour = terminal.FG["green"]
                elif correct and self.my_choice == letter:
                    colour = terminal.FG["red"]
                s.put(row, colour + "%s%s) %s" % (prefix, letter, choice))
                row += 1
            if self.open and self.asked_at is not None:
                left = max(0.0, self.limit - (time.monotonic() - self.asked_at))
                s.put(row, terminal.FG["yellow"] + "    %.0f s left   press A-D" % left)
            row += 2

        if self.result is not None:
            r = self.result
            verdict = (terminal.FG["green"] + "correct") if r["ok"] else (
                terminal.FG["red"] + "wrong, it was %s" % r["correct"])
            s.put(row, "  You answered %s - %s" % (r["yours"], verdict))
            row += 1
            s.put(row, "  raw %.1f ms   adjusted %.1f ms   (-rtt/2 = %.1f ms)   "
                       "+%d points, total %d"
                  % (r["raw_ms"], r["adj_ms"], r["raw_ms"] - r["adj_ms"],
                     r["points"], r["total"]))
            row += 2

        s.put(row, terminal.BOLD + "scoreboard   (raw rank = arrival order, "
                                   "adj rank = after subtracting rtt/2)")
        row += 1
        s.put(row, terminal.BOLD + "%-16s %6s %5s %5s %10s %10s %s"
              % ("player", "score", "raw", "adj", "raw ms", "adj ms", ""))
        row += 1
        if not self.rows:
            s.put(row, terminal.DIM + "  empty")
            row += 1
        for r in self.rows:
            colour = terminal.RESET if r.online else terminal.DIM
            if r.name == self.name:
                colour = terminal.FG["cyan"]
            flag = {"1": "ok", "0": "no"}.get(r.correct, "")
            s.put(row, colour + "%-16s %6d %5s %5s %10s %10s %s"
                  % (r.name + ("" if r.online else " (off)"),
                     r.score,
                     r.rank_raw or "",
                     r.rank_adj or "",
                     "%.1f" % r.raw_ms if r.answered else "",
                     "%.1f" % r.adj_ms if r.answered else "",
                     flag))
            row += 1

        row += 1
        s.put(row, terminal.BOLD + "transport  (from stats(peer))")
        row += 1
        try:
            st = self.t.stats(self.peer)
            resent = st.get("resent", 0)
            colour = terminal.FG["yellow"] if resent else terminal.RESET
            s.put(row, colour + "  peer %-3s rtt %7.2f ms  resent %5d  dropped %5d"
                               "  sent %5d  recv %5d  %s"
                  % (self.peer, st.get("rtt_ms", 0.0), resent,
                     st.get("dropped", 0), st.get("sent", 0),
                     st.get("received", 0), st.get("state", "")))
        except Exception as exc:
            s.put(row, terminal.FG["red"] + "  stats unavailable: %s" % exc)
        row += 2

        for line in self.log[-4:]:
            if row < s.rows - 1:
                s.put(row, terminal.DIM + "  " + line)
                row += 1

        s.put(s.rows, terminal.REVERSE + terminal.pad(
            " A B C D or 1-4 to answer   r repaint   q quit ", width))
        s.flush()

    # -- the loop --------------------------------------------------------

    def run(self, keyboard):
        last_draw = 0.0
        while self.running:
            for key in keyboard.keys():
                if key in ("q", "Q"):
                    self.running = False
                elif key in ("r", "R"):
                    self.screen.invalidate()
                elif key.upper() in ("A", "B", "C", "D"):
                    self.answer(key.upper())
                elif key in ("1", "2", "3", "4"):
                    self.answer("ABCD"[int(key) - 1])

            got = self.t.recv(timeout=0.05)
            while got is not None:
                _peer, data = got
                self.handle(data)
                got = self.t.recv(timeout=0)

            if self.peer not in self.t.peers():
                why = None
                getter = getattr(self.t, "why_closed", None)
                if getter:
                    try:
                        why = getter(self.peer)
                    except Exception:
                        why = None
                self.note("the transport reported the host closed: %s"
                          % (why or "no acknowledgments"))
                self.draw()
                time.sleep(1.5)
                self.running = False
                break

            now = time.monotonic()

            if (not self.joined and self.join_retry_at is not None
                    and now >= self.join_retry_at):
                if now - self.started > JOIN_GIVE_UP:
                    self.note("gave up after %.0f s of the name being in use"
                              % JOIN_GIVE_UP)
                    self.draw()
                    self.running = False
                    break
                self.join_attempts += 1
                self.join_retry_at = now + JOIN_RETRY
                self.send("JOIN|%d|%s" % (PROTOCOL_VERSION, self.name))
                self.note("JOIN attempt %d (no WELCOME yet)" % self.join_attempts)

            # A keepalive while nothing else is going on, so the host's
            # rtt_ms for us stays a recent measurement.  See protocol.md 3.4.
            if self.joined and not self.open and now - self.last_ping > PING_EVERY:
                self.send("PING|k")
                self.last_ping = now

            if (self.autoplay and self.open and self.my_choice is None
                    and self.asked_at is not None
                    and now - self.asked_at >= (self.think_for or 0.0)):
                self.answer(random.choice("ABCD"))

            if self.open and self.asked_at and now - self.asked_at > self.limit + 1:
                self.open = False

            if now - last_draw >= STATUS_EVERY:
                self.draw()
                last_draw = now

        if self.peer in self.t.peers():
            self.send("LEAVE")
        self.t.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Live quiz client")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--name", required=True,
                        help="1-16 characters of A-Za-z0-9_-")
    parser.add_argument("--autoplay", action="store_true",
                        help="answer at random after a short pause")
    parser.add_argument("--think", type=float, nargs=2, metavar=("LO", "HI"),
                        help="pause range for --autoplay, seconds")
    args = parser.parse_args(argv)

    if not valid_name(args.name):
        sys.stderr.write("--name must be 1 to 16 characters of A-Za-z0-9_-\n")
        return 2

    terminal.require_size(78, 22)
    screen = terminal.Screen()
    keyboard = terminal.Keyboard()
    client = None
    try:
        with screen, keyboard:
            try:
                client = QuizClient(args.host, args.port, args.name, screen,
                                    autoplay=args.autoplay,
                                    think=tuple(args.think) if args.think else None)
            except (TimeoutError, OSError) as exc:
                screen.stop()
                keyboard.restore()
                sys.stderr.write("could not reach %s:%d - %s\n"
                                 % (args.host, args.port, exc))
                return 1
            client.run(keyboard)
    except KeyboardInterrupt:
        pass
    finally:
        keyboard.restore()
        screen.stop()
        if client is not None:
            try:
                client.t.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
