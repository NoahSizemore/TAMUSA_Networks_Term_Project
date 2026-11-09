#!/usr/bin/env python3
"""Live quiz, host side.  Speaks the protocol in documentation/protocol.md v1.

From the repository root:

    PYTHONPATH=src      python3 src/app/quiz_host.py --port 9000   # real
    PYTHONPATH=src/stub python3 src/app/quiz_host.py --port 9000   # stub
    TRANSPORT_LOSS=0.1 PYTHONPATH=src python3 src/app/quiz_host.py --port 9000

Nothing in this file changes between the first two: which transport.py is
imported is decided entirely by PYTHONPATH.  That is also why the application
lives in a directory of its own with no transport.py beside it -- Python puts
a script's own directory ahead of PYTHONPATH, so an application sitting next
to the real transport could never be pointed at the stub.

Keys:  n  ask the next question        e  close the question early
       r  repaint the screen           q  quit

The only module here that touches the network is transport.py.  terminal.py is
local stdlib terminal plumbing with no networking in it at all; `socket` and
`struct` appear nowhere in the application half.

The main loop is single-threaded and calls recv() every pass, which is what
keeps the transport's retransmission timers running: clause 5 of the contract
says the transport retransmits nothing while the application calls neither
send() nor recv().
"""

import argparse
import os
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
MAX_MESSAGE = 1024
SCOREBOARD_ROWS = 12
NAME_MAX = 16
STATUS_EVERY = 0.25  # seconds between status-line refreshes

# (text, a, b, c, d, correct)
QUESTIONS = [
    (
        "Which layer does a UDP datagram belong to?",
        "Link", "Network", "Transport", "Application", "C",
    ),
    (
        "What does a transport checksum let a receiver do?",
        "Reorder messages", "Detect corruption", "Compress data",
        "Encrypt payloads", "B",
    ),
    (
        "In stop-and-wait, how many messages are outstanding at once?",
        "One", "Eight", "As many as fit the window", "Unlimited", "A",
    ),
    (
        "A sender's ACK is lost. What does the receiver see next?",
        "Nothing more", "A duplicate of a message it already has",
        "A connection reset", "A smaller window", "B",
    ),
    (
        "Why does Karn's algorithm ignore retransmitted messages?",
        "They are usually corrupt",
        "Their acknowledgment cannot be attributed to one transmission",
        "They use a different checksum", "They are sent out of order", "B",
    ),
    (
        "What does the 'cumulative' in a cumulative acknowledgment mean?",
        "It counts total bytes", "It acknowledges everything up to a sequence",
        "It is sent once per window", "It includes a timestamp", "B",
    ),
    (
        "TRANSPORT_DELAY=17.5 at both ends gives a round trip of",
        "17.5 ms", "35 ms", "8.75 ms", "70 ms", "B",
    ),
    (
        "Go-Back-N retransmits which messages after a timeout?",
        "Only the lost one", "The whole window from the base",
        "The last one sent", "None, it waits for a NAK", "B",
    ),
    (
        "Why is rtt_ms/2 an estimate of one-way delay, not a measurement?",
        "The clock is too coarse", "The two directions need not be equal",
        "UDP has no checksum", "ACKs are optional", "B",
    ),
    (
        "A peer stops answering. What must the transport do?",
        "Block forever in send", "Report the peer closed and keep the others",
        "Close every peer", "Switch to TCP", "B",
    ),
]


def clean(text):
    """Strip the field and row separators out of anything we put on the wire."""
    return str(text).replace("|", "/").replace(";", ",").replace("\n", " ")


def valid_name(name):
    return (
        1 <= len(name) <= NAME_MAX
        and all(c.isalnum() or c in "-_" for c in name)
        and name.isascii()
    )


class Player:
    """A competitor, keyed on name so a score survives a reconnect."""

    def __init__(self, name):
        self.name = name
        self.score = 0
        self.peer = None  # transport peer id while online, else None
        self.raw_ms = -1.0
        self.adj_ms = -1.0
        self.choice = None
        self.correct = None
        self.answered_qid = None
        self.joined_at = time.monotonic()

    @property
    def online(self):
        return self.peer is not None


class QuizHost:
    def __init__(self, port, limit_s, screen, auto=False, gap=4.0):
        self.t = transport.Transport()
        self.port = self.t.bind(port)
        self.limit = limit_s
        self.screen = screen
        # --auto walks the question list on a timer instead of waiting for the
        # n key.  It exists so the stack can be exercised, and the asciinema
        # cast recorded, without a hand on the keyboard; the demo itself is
        # driven by hand.
        self.auto = auto
        self.gap = gap
        self.finished = False

        self.players = {}  # name -> Player
        self.by_peer = {}  # peer id -> name
        self.known_peers = set()

        self.qid = 0
        self.index = 0  # 1-based index of the current question
        self.question = None  # the tuple from QUESTIONS
        self.open = False
        self.sent_at = {}  # peer id -> when this peer was sent the question
        self.closed_at = None
        self.log = []
        self.running = True

        self.note("bound to UDP %d, waiting for clients" % self.port)
        self.note(
            "loss=%.2f delay=%.1f ms window=%d"
            % (self.t.loss, getattr(self.t, "delay", 0.0) * 1000,
               getattr(self.t, "window", 1))
        )
        # Which transport.py did we actually import?  The application cannot
        # tell the stub from the real one by behaviour alone at loss 0, and
        # the import is resolved by PYTHONPATH, so say it out loud.
        self.note("transport: %s" % transport_path())

    # -- bookkeeping -----------------------------------------------------

    def note(self, text):
        self.log.append("%s  %s" % (time.strftime("%H:%M:%S"), text))
        del self.log[:-8]

    def send(self, peer, text):
        """One application message.  Never raises at the call site."""
        data = text.encode("utf-8")
        if len(data) > MAX_MESSAGE:
            self.note("refusing to send %d bytes" % len(data))
            return False
        try:
            self.t.send(peer, data)
            return True
        except (transport.PeerClosed, ConnectionError, ValueError) as exc:
            self.note("send to peer %s failed: %s" % (peer, exc))
            return False

    def broadcast(self, text, skip=None):
        for peer in self.t.peers():
            if peer != skip:
                self.send(peer, text)

    def player_of(self, peer):
        name = self.by_peer.get(peer)
        return self.players.get(name) if name else None

    # -- scoreboard ------------------------------------------------------

    def rankings(self):
        """Placings for the current question, by arrival and by adjusted time."""
        answered = [
            p for p in self.players.values() if p.answered_qid == self.qid and self.qid
        ]
        raw_rank = {
            p.name: i + 1
            for i, p in enumerate(sorted(answered, key=lambda p: p.raw_ms))
        }
        adj_rank = {
            p.name: i + 1
            for i, p in enumerate(sorted(answered, key=lambda p: p.adj_ms))
        }
        return raw_rank, adj_rank

    def score_message(self):
        raw_rank, adj_rank = self.rankings()
        ordered = sorted(
            self.players.values(), key=lambda p: (-p.score, p.joined_at)
        )
        rows = []
        for p in ordered[:SCOREBOARD_ROWS]:
            answered = 1 if p.answered_qid == self.qid and self.qid else 0
            rows.append(
                "%s;%d;%d;%d;%.1f;%.1f;%d;%d;%s"
                % (
                    clean(p.name),
                    p.score,
                    1 if p.online else 0,
                    answered,
                    p.raw_ms if answered else -1.0,
                    p.adj_ms if answered else -1.0,
                    raw_rank.get(p.name, 0),
                    adj_rank.get(p.name, 0),
                    "-" if not answered else ("1" if p.correct else "0"),
                )
            )
        return "SCORE|%d|%d|%s" % (self.qid, len(rows), "|".join(rows))

    def push_scoreboard(self):
        message = self.score_message()
        if len(message.encode("utf-8")) > MAX_MESSAGE:
            # Cannot happen with 12 rows of this shape, but the protocol says
            # what to do if it ever did, so do it rather than trust the maths.
            message = "SCORE|%d|0" % self.qid
        self.broadcast(message)

    # -- questions -------------------------------------------------------

    def ask_next(self):
        if self.open:
            self.close_question()
        if self.index >= len(QUESTIONS):
            if not self.finished:
                self.note("that was the last question")
            self.finished = True
            return
        self.index += 1
        self.qid += 1
        self.question = QUESTIONS[self.index - 1]
        self.open = True
        self.closed_at = None
        self.sent_at = {}
        for p in self.players.values():
            p.answered_qid = None
            p.choice = None
            p.correct = None
            p.raw_ms = -1.0
            p.adj_ms = -1.0

        text, a, b, c, d, _correct = self.question
        body = "QUESTION|%d|%d|%d|%d|%s|%s|%s|%s|%s" % (
            self.qid, self.index, len(QUESTIONS), self.limit,
            clean(text), clean(a), clean(b), clean(c), clean(d),
        )
        # Per-peer send timestamps: the host sends to its peers in a loop, and
        # with one message outstanding at a time the later peers really are
        # later.  Timing them all from one instant would charge that to the
        # players.
        for peer in self.t.peers():
            if self.by_peer.get(peer):
                self.sent_at[peer] = time.monotonic()
                self.send(peer, body)
        self.note("asked question %d of %d" % (self.index, len(QUESTIONS)))
        self.push_scoreboard()

    def close_question(self):
        if not self.open:
            return
        self.open = False
        self.closed_at = time.monotonic()
        correct = self.question[5]
        tally = {"A": 0, "B": 0, "C": 0, "D": 0}
        for p in self.players.values():
            if p.answered_qid == self.qid and p.choice in tally:
                tally[p.choice] += 1
        self.broadcast(
            "REVEAL|%d|%s|%d;%d;%d;%d"
            % (self.qid, correct, tally["A"], tally["B"], tally["C"], tally["D"])
        )
        self.push_scoreboard()
        self.note("question %d closed, answer %s" % (self.index, correct))

    # -- inbound ---------------------------------------------------------

    def handle(self, peer, data):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            self.send(peer, "ERROR|4|not utf-8")
            return
        fields = text.split("|")
        kind = fields[0] if fields else ""

        if kind == "JOIN":
            self.on_join(peer, fields)
            return

        if peer not in self.by_peer:
            self.send(peer, "ERROR|7|join first")
            return

        if kind == "ANSWER":
            self.on_answer(peer, fields)
        elif kind == "LEAVE":
            self.on_leave(peer)
        elif kind == "PING":
            if len(fields) != 2 or not (1 <= len(fields[1]) <= 8):
                self.send(peer, "ERROR|4|bad token")
            else:
                self.send(peer, "PONG|%s" % clean(fields[1]))
        else:
            self.send(peer, "ERROR|5|unknown message type")

    def on_join(self, peer, fields):
        if len(fields) != 3:
            self.send(peer, "ERROR|5|JOIN wants a version and a name")
            return
        version, name = fields[1], fields[2]
        if version != str(PROTOCOL_VERSION):
            self.send(peer, "BYE|6|protocol v%s, this host speaks v%d"
                      % (version, PROTOCOL_VERSION))
            self.drop(peer, "version mismatch")
            return
        if not valid_name(name):
            self.send(peer, "ERROR|4|name must be 1-16 of A-Za-z0-9_-")
            return

        existing = self.players.get(name)

        # A second JOIN from a peer that already holds this name is a re-send,
        # not a new player.  Nothing is reset; the state just goes out again.
        if existing is not None and existing.peer == peer:
            self.note("%s re-sent JOIN" % name)
            self.send_state(peer, existing)
            return

        if existing is not None and existing.online:
            # The name is taken.  It may be taken by a client that is still
            # there, or by one that was killed a moment ago and that the
            # transport has not given up on yet -- a peer is only reported
            # closed after it fails to acknowledge something, and between
            # questions the host sends it nothing to acknowledge.
            #
            # So answer ERROR 1, and in the same breath put a message in
            # flight to the incumbent.  If it is alive it acknowledges and
            # keeps the name.  If it is gone, that message is what starts its
            # retransmission timer, the transport reports it closed a few
            # seconds later, and the retry this client is required to make
            # then succeeds with the score intact.
            self.send(peer, "ERROR|1|%s is already playing" % clean(name))
            self.send(existing.peer, self.score_message())
            self.note("%s is contested, probing the peer that holds it" % name)
            return

        if existing is None:
            existing = Player(name)
            self.players[name] = existing
            self.note("%s joined" % name)
        else:
            self.note("%s rejoined, score %d kept" % (name, existing.score))

        # Whatever this peer was called before, it is this player now.
        old = self.by_peer.get(peer)
        if old and old in self.players and self.players[old].peer == peer:
            self.players[old].peer = None
        existing.peer = peer
        self.by_peer[peer] = name
        self.send_state(peer, existing)
        self.push_scoreboard()

    def send_state(self, peer, player):
        """Everything a client needs to draw itself: the rejoin path."""
        self.send(
            peer,
            "WELCOME|%d|%s|%d|%d"
            % (PROTOCOL_VERSION, clean(player.name), len(QUESTIONS), self.limit),
        )
        self.send(peer, self.score_message())
        if self.open and player.answered_qid != self.qid:
            text, a, b, c, d, _ = self.question
            self.sent_at[peer] = time.monotonic()
            self.send(
                peer,
                "QUESTION|%d|%d|%d|%d|%s|%s|%s|%s|%s"
                % (self.qid, self.index, len(QUESTIONS), self.limit,
                   clean(text), clean(a), clean(b), clean(c), clean(d)),
            )

    def on_answer(self, peer, fields):
        arrival = time.monotonic()
        player = self.player_of(peer)
        if player is None:
            self.send(peer, "ERROR|7|join first")
            return
        if len(fields) != 3:
            self.send(peer, "ERROR|5|ANSWER wants a qid and a choice")
            return
        try:
            qid = int(fields[1])
        except ValueError:
            self.send(peer, "ERROR|4|qid is not a number")
            return
        choice = fields[2].upper()

        if not self.open or qid != self.qid:
            self.send(peer, "ERROR|2|question %d is not open" % qid)
            return
        if player.answered_qid == self.qid:
            self.send(peer, "ERROR|3|you have already answered")
            return
        if choice not in ("A", "B", "C", "D"):
            self.send(peer, "ERROR|4|choice must be A, B, C or D")
            return

        sent = self.sent_at.get(peer)
        if sent is None:
            # Joined after the question went out and was never sent it.
            self.send(peer, "ERROR|2|you were not sent question %d" % qid)
            return

        try:
            rtt_ms = float(self.t.stats(peer).get("rtt_ms", 0.0))
        except Exception:
            rtt_ms = 0.0

        raw_ms = (arrival - sent) * 1000.0
        adj_ms = max(0.0, raw_ms - rtt_ms / 2.0)

        correct = choice == self.question[5]
        points = 0
        if correct:
            fraction = 1.0 - (adj_ms / 1000.0) / float(self.limit)
            points = 100 + int(round(50 * max(0.0, fraction)))
            player.score += points

        player.answered_qid = self.qid
        player.choice = choice
        player.correct = correct
        player.raw_ms = raw_ms
        player.adj_ms = adj_ms

        self.send(
            peer,
            "RESULT|%d|%s|%s|%d|%.1f|%.1f|%d|%d"
            % (self.qid, self.question[5], choice, 1 if correct else 0,
               raw_ms, adj_ms, points, player.score),
        )
        self.note(
            "%s answered %s (%s) raw %.0f ms adj %.0f ms"
            % (player.name, choice, "correct" if correct else "wrong", raw_ms, adj_ms)
        )
        self.push_scoreboard()

        online = [p for p in self.players.values() if p.online]
        if online and all(p.answered_qid == self.qid for p in online):
            self.close_question()

    def on_leave(self, peer):
        player = self.player_of(peer)
        if player is not None:
            player.peer = None
            self.note("%s left" % player.name)
        self.by_peer.pop(peer, None)
        self.push_scoreboard()
        try:
            self.t.close(peer)
        except Exception:
            pass

    def drop(self, peer, why):
        player = self.player_of(peer)
        if player is not None:
            player.peer = None
        self.by_peer.pop(peer, None)
        self.known_peers.discard(peer)
        self.note("dropped peer %s: %s" % (peer, why))
        try:
            self.t.close(peer)
        except Exception:
            pass

    def check_departures(self):
        """The transport reports a closed peer by no longer listing it."""
        live = set(self.t.peers())
        for peer in sorted(self.known_peers - live):
            player = self.player_of(peer)
            if player is not None:
                player.peer = None
                self.note(
                    "%s went away (%s)"
                    % (player.name, self.why(peer) or "transport reported it closed")
                )
            else:
                self.note("peer %s went away before joining" % peer)
            self.by_peer.pop(peer, None)
            self.push_scoreboard()
        for peer in sorted(live - self.known_peers):
            self.note("peer %s connected" % peer)
        self.known_peers = live

    def why(self, peer):
        getter = getattr(self.t, "why_closed", None)
        try:
            return getter(peer) if getter else None
        except Exception:
            return None

    # -- drawing ---------------------------------------------------------

    def draw(self):
        s = self.screen
        s.clear()
        width = s.cols
        s.put(1, terminal.REVERSE + terminal.pad(
            " LIVE QUIZ - HOST   port %d   question %d/%d   %s"
            % (self.port, self.index, len(QUESTIONS),
               "OPEN" if self.open else "closed"), width))

        row = 3
        if self.question is not None:
            text, a, b, c, d, correct = self.question
            s.put(row, terminal.BOLD + "Q%d. %s" % (self.index, text))
            row += 1
            for letter, choice in zip("ABCD", (a, b, c, d)):
                mark = ""
                if not self.open and letter == correct:
                    mark = terminal.FG["green"] + "  <- answer" + terminal.RESET
                s.put(row, "    %s) %s%s" % (letter, choice, mark))
                row += 1
            if self.open:
                left = max(0.0, self.limit - (time.monotonic() - min(
                    self.sent_at.values(), default=time.monotonic())))
                s.put(row, terminal.FG["yellow"] + "    %.0f s left" % left)
            else:
                s.put(row, terminal.DIM + "    press n for the next question")
            row += 2
        else:
            s.put(row, terminal.DIM + "No question asked yet.  Press n to begin.")
            row += 2

        s.put(row, terminal.BOLD + "%-16s %6s  %-4s %-4s  %9s %9s  %s"
              % ("player", "score", "raw", "adj", "raw ms", "adj ms", "answer"))
        row += 1
        raw_rank, adj_rank = self.rankings()
        ordered = sorted(self.players.values(), key=lambda p: (-p.score, p.joined_at))
        if not ordered:
            s.put(row, terminal.DIM + "  nobody has joined yet")
            row += 1
        for p in ordered[:SCOREBOARD_ROWS]:
            answered = p.answered_qid == self.qid and self.qid
            colour = terminal.RESET if p.online else terminal.DIM
            verdict = "-"
            if answered:
                verdict = (terminal.FG["green"] + p.choice + " ok")  \
                    if p.correct else (terminal.FG["red"] + p.choice + " no")
            s.put(row, colour + "%-16s %6d  %-4s %-4s  %9s %9s  %s"
                  % (p.name + ("" if p.online else " (off)"),
                     p.score,
                     raw_rank.get(p.name, "") or "",
                     adj_rank.get(p.name, "") or "",
                     "%.1f" % p.raw_ms if answered else "",
                     "%.1f" % p.adj_ms if answered else "",
                     verdict))
            row += 1

        row += 1
        s.put(row, terminal.BOLD + "transport  (from stats(peer), redrawn as it changes)")
        row += 1
        peers = self.t.peers()
        if not peers:
            s.put(row, terminal.DIM + "  no peers connected")
            row += 1
        for peer in peers[: max(1, s.rows - row - len(self.log) - 3)]:
            try:
                st = self.t.stats(peer)
            except Exception:
                continue
            name = self.by_peer.get(peer, "(not joined)")
            resent = st.get("resent", 0)
            colour = terminal.FG["yellow"] if resent else terminal.RESET
            s.put(row, colour + "  peer %-3s %-16s rtt %7.2f ms  resent %5d  "
                  "dropped %5d  sent %5d  recv %5d"
                  % (peer, name, st.get("rtt_ms", 0.0), resent,
                     st.get("dropped", 0), st.get("sent", 0),
                     st.get("received", 0)))
            row += 1

        for line in self.log[-5:]:
            if row < s.rows - 1:
                s.put(row, terminal.DIM + "  " + line)
                row += 1

        s.put(s.rows, terminal.REVERSE + terminal.pad(
            " n next question   e close question   r repaint   q quit ", width))
        s.flush()

    # -- the loop --------------------------------------------------------

    def run(self, keyboard):
        last_draw = 0.0
        while self.running:
            for key in keyboard.keys():
                if key in ("q", "Q"):
                    self.running = False
                elif key in ("n", "N"):
                    self.ask_next()
                elif key in ("e", "E"):
                    self.close_question()
                elif key in ("r", "R"):
                    self.screen.invalidate()

            # recv() on every pass: this is what services the retransmission
            # timers and the delay queue.
            got = self.t.recv(timeout=0.05)
            while got is not None:
                peer, data = got
                self.handle(peer, data)
                got = self.t.recv(timeout=0)

            self.check_departures()

            if self.open and self.sent_at:
                oldest = min(self.sent_at.values())
                if time.monotonic() - oldest > self.limit:
                    self.close_question()

            if self.auto and not self.open and not self.finished:
                waited = time.monotonic() - (self.closed_at or 0.0)
                if self.closed_at is None or waited >= self.gap:
                    if self.players or self.index:
                        self.ask_next()

            now = time.monotonic()
            if now - last_draw >= STATUS_EVERY:
                self.draw()
                last_draw = now

        for peer in self.t.peers():
            self.send(peer, "BYE|0|host is shutting down")
        self.t.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Live quiz host")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--limit", type=int, default=20,
                        help="answer window per question, seconds")
    parser.add_argument("--auto", action="store_true",
                        help="advance through the questions on a timer")
    parser.add_argument("--gap", type=float, default=4.0,
                        help="seconds between questions with --auto")
    args = parser.parse_args(argv)

    terminal.require_size(78, 22)
    screen = terminal.Screen()
    keyboard = terminal.Keyboard()
    host = None
    try:
        with screen, keyboard:
            host = QuizHost(args.port, args.limit, screen,
                            auto=args.auto, gap=args.gap)
            if not keyboard.interactive:
                sys.stderr.write("stdin is not a terminal; keys are disabled\n")
            host.run(keyboard)
    except KeyboardInterrupt:
        pass
    finally:
        # Restoring the terminal happens here whatever went wrong, so a crash
        # cannot leave a shell that echoes nothing.
        keyboard.restore()
        screen.stop()
        if host is not None:
            try:
                host.t.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
