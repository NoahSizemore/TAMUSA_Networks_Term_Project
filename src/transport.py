"""Reliable message transport over UDP.

CSCI 4306 Team Project - transport half.

Provides class Transport: one UDP socket, many peers, reliable in-order
message delivery with checksums, sequence numbers, cumulative
acknowledgments, a retransmission timeout and duplicate detection.

Design summary (the full writeup lives in documentation/protocol.md and the
report):

  * 16-byte fixed header, struct.pack("!BBHIIHH", ...).
  * Three-way handshake (SYN / SYNACK / ACK) so that BOTH endpoints own a
    round-trip-time sample the moment the connection is established.
  * Go-Back-N sender with a window of TRANSPORT_WINDOW messages. The default
    window is 1, which is exactly stop-and-wait; set TRANSPORT_WINDOW=8 for
    the pipelined (bonus) behavior. The receiver never buffers out-of-order
    messages, it re-acknowledges its last in-order sequence instead.
  * Single threaded. Every timer, the delay queue and the socket are serviced
    inside send() and recv() by _service(). No threads anywhere, and the
    transport retransmits nothing while the application calls neither.

Environment variables, both read once in the constructor:

  TRANSPORT_LOSS    float 0..1, fraction of OUTGOING datagrams discarded.
                    Applied to every datagram we emit, acknowledgments and
                    handshake packets included, not only to data.
  TRANSPORT_DELAY   milliseconds of one-way delay. Datagrams are held in a
                    queue and released inside send()/recv(); time.sleep() is
                    never called, so the application is never stalled.
  TRANSPORT_WINDOW  integer >= 1, messages allowed outstanding at once.
                    Defaults to 1 (stop-and-wait).
"""

import os
import random
import select
import socket
import struct
import time

__all__ = ["Transport", "PeerClosed", "VERSION", "MAX_PAYLOAD"]

# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------

VERSION = 1

#   byte 0 : version (high 4 bits) | type (low 4 bits)
#   byte 1 : flags
#   2 - 3  : conn_id        connection identifier, demux key
#   4 - 7  : seq            sequence number of this message
#   8 - 11 : ack            highest in-order sequence received so far
#  12 - 13 : length         payload length, 0..1024
#  14 - 15 : checksum       16-bit one's complement over header+payload
HEADER_FMT = "!BBHIIHH"
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 16
assert HEADER_SIZE == 16

MAX_PAYLOAD = 1024

# Message types (4 bits).
T_SYN = 1
T_SYNACK = 2
T_DATA = 3
T_ACK = 4
T_FIN = 5
T_FINACK = 6

TYPE_NAMES = {
    T_SYN: "SYN",
    T_SYNACK: "SYNACK",
    T_DATA: "DATA",
    T_ACK: "ACK",
    T_FIN: "FIN",
    T_FINACK: "FINACK",
}

# Flags (byte 1).
FLAG_WINDOW = 0x01  # sender is running with a window > 1

# ---------------------------------------------------------------------------
# Retransmission constants.  These are the numbers documented in the report.
# ---------------------------------------------------------------------------

INITIAL_RTO = 0.200  # before any round-trip sample exists
# The floor is deliberately low.  It only ever binds on a path whose round
# trip is far below it, two processes on one VM measure tens of
# microseconds, where a 100 ms floor would make the 1000-message case of the
# conformance checklist take a minute at TRANSPORT_LOSS=0.2.  On a real
# 35 ms path SRTT + 4*DevRTT is well above the floor and the floor is never
# reached.
MIN_RTO = 0.010
MAX_RTO = 2.000
MAX_RETRIES = 8  # consecutive retransmissions of the same sequence number
ALPHA = 0.125  # SRTT   = (1-a)*SRTT   + a*sample
BETA = 0.250  # DevRTT = (1-b)*DevRTT + b*|SRTT-sample|
K = 4  # RTO    = SRTT + K*DevRTT

HANDSHAKE_SEQ = 0  # SYN and SYNACK occupy sequence 0; data starts at 1

CONNECT_TIMEOUT = 20.0  # hard ceiling on connect(), seconds
CLOSE_LINGER = 0.5  # how long close() waits for a FINACK, seconds
_POLL_CEILING = 1.0  # longest we ever block in one select() call


class PeerClosed(ConnectionError):
    """Raised by send() for a peer that is unknown, closed or gone."""


# ---------------------------------------------------------------------------
# Header helpers
# ---------------------------------------------------------------------------


def _checksum(data):
    """16-bit one's complement sum, the Internet checksum of RFC 1071."""
    if len(data) % 2:
        data = data + b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) | data[i + 1]
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def _pack(msg_type, flags, conn_id, seq, ack, payload=b""):
    """Build one datagram.  The checksum is computed with its own field zero."""
    head = struct.pack(
        HEADER_FMT,
        ((VERSION & 0x0F) << 4) | (msg_type & 0x0F),
        flags & 0xFF,
        conn_id & 0xFFFF,
        seq & 0xFFFFFFFF,
        ack & 0xFFFFFFFF,
        len(payload) & 0xFFFF,
        0,
    )
    csum = _checksum(head + payload)
    return head[:14] + struct.pack("!H", csum) + payload


def _unpack(datagram):
    """Validate and split a datagram.

    Returns a dict, or None when the datagram is malformed, carries a version
    we do not speak, or fails its checksum.
    """
    if len(datagram) < HEADER_SIZE:
        return None
    head = datagram[:HEADER_SIZE]
    payload = datagram[HEADER_SIZE:]
    ver_type, flags, conn_id, seq, ack, length, csum = struct.unpack(HEADER_FMT, head)

    if (ver_type >> 4) != VERSION:
        return None
    if length != len(payload):
        return None
    zeroed = head[:14] + b"\x00\x00"
    if _checksum(zeroed + payload) != csum:
        return None

    return {
        "type": ver_type & 0x0F,
        "flags": flags,
        "conn_id": conn_id,
        "seq": seq,
        "ack": ack,
        "payload": payload,
    }


# ---------------------------------------------------------------------------
# Per-peer state
# ---------------------------------------------------------------------------


class _Pending:
    """One datagram that has been sent and not yet acknowledged."""

    __slots__ = ("seq", "datagram", "sent_at", "retries", "rtt_ok", "is_fin")

    def __init__(self, seq, datagram, sent_at, is_fin=False):
        self.seq = seq
        self.datagram = datagram
        self.sent_at = sent_at
        self.retries = 0
        self.rtt_ok = True  # Karn: cleared once this one is retransmitted
        self.is_fin = is_fin


class _Peer:
    """Everything the transport knows about one remote endpoint."""

    def __init__(self, peer_id, addr, conn_id, is_client):
        self.id = peer_id
        self.addr = addr
        self.conn_id = conn_id
        self.is_client = is_client  # True when we dialed out with connect()

        self.established = False
        self.closed = False
        self.sent_fin = False
        self.got_fin = False
        self.close_reason = None

        # Sender
        self.next_seq = HANDSHAKE_SEQ + 1  # next sequence number to assign
        self.outbox = {}  # seq -> _Pending, the window in flight
        self.base = HANDSHAKE_SEQ  # lowest unacknowledged sequence
        self.backlog = []  # app messages waiting for window space
        self.timer = None  # absolute deadline for the oldest unacked

        # Receiver
        self.recv_next = HANDSHAKE_SEQ  # next sequence we will accept

        # Round-trip estimation
        self.srtt = None
        self.devrtt = None
        self.rto = INITIAL_RTO

        # Counters reported by stats()
        self.sent = 0
        self.received = 0
        self.resent = 0
        self.dropped = 0  # inbound datagrams discarded without delivery
        self.loss_tx = 0  # outbound datagrams discarded by the simulator
        self.acks_sent = 0
        self.datagrams_sent = 0

    # -- round-trip estimator (RFC 6298 shape) ------------------------------

    def sample_rtt(self, sample):
        if sample <= 0:
            return
        if self.srtt is None:
            self.srtt = sample
            self.devrtt = sample / 2.0
        else:
            self.devrtt = (1 - BETA) * self.devrtt + BETA * abs(self.srtt - sample)
            self.srtt = (1 - ALPHA) * self.srtt + ALPHA * sample
        self.rto = min(MAX_RTO, max(MIN_RTO, self.srtt + K * self.devrtt))

    @property
    def rtt_ms(self):
        return 0.0 if self.srtt is None else self.srtt * 1000.0


# ---------------------------------------------------------------------------
# The transport
# ---------------------------------------------------------------------------


class Transport:
    """One UDP socket, many peers, reliable ordered messages.

    The seven methods required by the project interface are bind, connect,
    send, recv, peers, close and stats.  Everything else on this class is
    internal.
    """

    def __init__(self):
        self.loss = _env_float("TRANSPORT_LOSS", 0.0, 0.0, 1.0)
        self.delay = _env_float("TRANSPORT_DELAY", 0.0, 0.0, 60000.0) / 1000.0
        self.window = max(1, _env_int("TRANSPORT_WINDOW", 1))

        self._sock = None
        self._bound = False
        self._closed = False

        self._peers = {}  # peer id -> _Peer, closed peers are kept for stats()
        self._by_key = {}  # (addr, conn_id) -> _Peer
        self._next_peer_id = 1

        self._inbox = []  # [(peer_id, bytes)] ready for recv()
        self._inbox_at = 0  # read cursor, avoids list.pop(0)
        self._pipe = []  # [(release_time, datagram, addr)] delay queue

        self._rng = random.Random()
        self._dropped_unknown = 0

    # ------------------------------------------------------------------
    # 1. bind
    # ------------------------------------------------------------------

    def bind(self, port):
        """Host side.  Start accepting peers on this UDP port."""
        if self._bound:
            raise RuntimeError("transport is already bound")
        self._open_socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("", int(port)))
        self._bound = True
        return self._sock.getsockname()[1]

    # ------------------------------------------------------------------
    # 2. connect
    # ------------------------------------------------------------------

    def connect(self, host, port, timeout=CONNECT_TIMEOUT):
        """Client side.  Reach a bound endpoint; return an integer peer id.

        The peer id is usable straight away: messages handed to send() before
        the handshake finishes queue behind the SYN and go out in order.

        By default this waits for the handshake, which is what an application
        wants.  timeout=0 returns as soon as the SYN has been emitted and
        lets the handshake finish inside later send()/recv() calls -- which is
        what the conformance test needs, because it drives both endpoints from
        one thread and cannot afford to block in either.
        """
        self._open_socket()
        if not self._bound:
            self._sock.bind(("", 0))  # ephemeral port
            self._bound = True

        addr = (socket.gethostbyname(host), int(port))

        # A fresh conn_id per attempt.  A server that still holds state from a
        # previous incarnation of this client sees a different conn_id and
        # retires the stale peer instead of mixing the two streams.
        conn_id = self._rng.randrange(1, 0x10000)
        peer = _Peer(self._next_peer_id, addr, conn_id, is_client=True)
        self._next_peer_id += 1
        self._peers[peer.id] = peer
        self._by_key[(addr, conn_id)] = peer

        syn = _pack(T_SYN, self._flags(), conn_id, HANDSHAKE_SEQ, 0)
        self._install(peer, HANDSHAKE_SEQ, syn)

        if not timeout:
            return peer.id

        deadline = time.monotonic() + timeout
        while not peer.established:
            if peer.closed:
                raise TimeoutError(
                    "no answer from %s:%d after %d attempts"
                    % (addr[0], addr[1], MAX_RETRIES)
                )
            if time.monotonic() >= deadline:
                self._shutdown_peer(peer, "connect timed out")
                raise TimeoutError("connect to %s:%d timed out" % addr)
            self._service()
            if peer.established:
                break
            self._wait(min(deadline, self._next_event()))
        return peer.id

    # ------------------------------------------------------------------
    # 3. send
    # ------------------------------------------------------------------

    def send(self, peer, data):
        """Deliver data (bytes, at most 1024) to that peer, reliably, in order."""
        if self._closed:
            raise RuntimeError("this transport has been closed")
        if isinstance(data, (bytearray, memoryview)):
            data = bytes(data)
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes, not %s" % type(data).__name__)
        # Clause 3: checked before anything goes on the wire, so a rejected
        # message leaves the connection untouched.
        if len(data) > MAX_PAYLOAD:
            raise ValueError(
                "message is %d bytes, the limit is %d" % (len(data), MAX_PAYLOAD)
            )

        p = self._peers.get(peer)
        if p is None:
            raise PeerClosed("no such peer: %r" % (peer,))
        if p.closed or p.sent_fin:
            raise PeerClosed("peer %r is closed" % (peer,))

        p.backlog.append(data)
        self._service()
        return len(data)

    # ------------------------------------------------------------------
    # 4. recv
    # ------------------------------------------------------------------

    def recv(self, timeout=None):
        """Return (peer, data) for the next message, or None on timeout."""
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        if self._closed:
            # Hand back anything that arrived before the close, then refuse.
            # Without this a blocking recv() after close() would spin: there is
            # no socket left to wait on, so select() returns at once, and with
            # timeout=None there is no deadline to end the loop.
            msg = self._take()
            if msg is not None:
                return msg
            raise RuntimeError("this transport has been closed")

        while True:
            self._service()
            msg = self._take()
            if msg is not None:
                return msg

            now = time.monotonic()
            if deadline is not None and now >= deadline:
                return None

            limit = self._next_event()
            if deadline is not None:
                limit = min(limit, deadline)
            self._wait(limit)

    # ------------------------------------------------------------------
    # 5. peers
    # ------------------------------------------------------------------

    def peers(self):
        """Return the list of peer ids currently connected."""
        return [p.id for p in self._peers.values() if p.established and not p.closed]

    # ------------------------------------------------------------------
    # 6. close
    # ------------------------------------------------------------------

    def close(self, peer=None, linger=CLOSE_LINGER):
        """Disconnect one peer, or the whole endpoint when peer is None.

        `linger` is how long we keep servicing the socket so a FIN can be
        retransmitted and its FINACK collected.  The conformance test passes a
        small value because it drives both endpoints from one thread and the
        far side is not being serviced while we sit here.
        """
        if peer is None:
            for p in list(self._peers.values()):
                if p.established and not p.closed:
                    self._begin_close(p, linger)
            end = time.monotonic() + linger
            while time.monotonic() < end and any(
                not p.closed for p in self._peers.values() if p.established
            ):
                self._service()
                self._wait(min(end, self._next_event()))
            for p in self._peers.values():
                p.closed = True
                p.close_reason = p.close_reason or "endpoint closed"
            if self._sock is not None:
                self._sock.close()
                self._sock = None
            self._closed = True
            self._pipe.clear()
            return

        p = self._peers.get(peer)
        if p is None or p.closed:
            return
        self._begin_close(p, linger)
        end = time.monotonic() + linger
        while time.monotonic() < end and not p.closed:
            self._service()
            self._wait(min(end, self._next_event()))
        self._shutdown_peer(p, p.close_reason or "closed locally")

    # ------------------------------------------------------------------
    # 7. stats
    # ------------------------------------------------------------------

    def stats(self, peer):
        """Per-peer counters.  rtt_ms is a measurement, see _on_ack()."""
        p = self._peers.get(peer)
        if p is None:
            raise PeerClosed("no such peer: %r" % (peer,))
        return {
            # required keys
            "rtt_ms": round(p.rtt_ms, 3),
            "sent": p.sent,
            "received": p.received,
            "resent": p.resent,
            "dropped": p.dropped,
            # extras, useful on the status line and in the report
            "loss_tx": p.loss_tx,
            "rto_ms": round(p.rto * 1000.0, 3),
            "in_flight": len(p.outbox),
            "backlog": len(p.backlog),
            "window": self.window,
            "state": self._peer_state(p),
            "addr": "%s:%d" % p.addr,
            "conn_id": p.conn_id,
            "datagrams_sent": p.datagrams_sent,
            "acks_sent": p.acks_sent,
        }

    # ==================================================================
    # Internals
    # ==================================================================

    def _flags(self):
        return FLAG_WINDOW if self.window > 1 else 0

    def _open_socket(self):
        if self._closed:
            raise RuntimeError("this transport has been closed")
        if self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setblocking(False)

    @staticmethod
    def _peer_state(p):
        if p.closed:
            return "closed"
        if p.sent_fin or p.got_fin:
            return "closing"
        return "established" if p.established else "connecting"

    def _live_peers(self):
        return [p for p in self._peers.values() if not p.closed]

    def _take(self):
        """Pop the next deliverable message, or None."""
        if self._inbox_at < len(self._inbox):
            msg = self._inbox[self._inbox_at]
            self._inbox_at += 1
            if self._inbox_at > 64 and self._inbox_at * 2 >= len(self._inbox):
                del self._inbox[: self._inbox_at]
                self._inbox_at = 0
            return msg
        if self._inbox:
            self._inbox.clear()
            self._inbox_at = 0
        return None

    # -- the one service routine ---------------------------------------
    #
    # Clause 5 of the contract lives here.  Called at the top of send() and
    # inside recv()'s loop, so the delay queue and the retransmission timers
    # advance only while the application is calling one of the two.

    def _service(self):
        if self._sock is None:
            return
        self._release_pipe()
        self._drain_socket()
        self._check_timers()
        for p in self._peers.values():
            if not p.closed:
                self._fill_window(p)

    def _release_pipe(self):
        """Emit delayed datagrams whose release time has come."""
        if not self._pipe:
            return
        now = time.monotonic()
        keep = []
        for release, datagram, addr in self._pipe:
            if release <= now:
                self._emit(datagram, addr)
            else:
                keep.append((release, datagram, addr))
        self._pipe = keep

    def _drain_socket(self):
        """Read every datagram the kernel already has for us."""
        for _ in range(4096):  # a bounded pass, so one peer cannot starve rest
            if self._sock is None:
                return
            try:
                datagram, addr = self._sock.recvfrom(65535)
            except (BlockingIOError, InterruptedError):
                return
            except ConnectionError:
                # On Windows an ICMP port-unreachable for a previous sendto
                # surfaces on the *receive* call as WSAECONNRESET.  It says
                # nothing about the datagrams still queued behind it, so keep
                # reading rather than abandoning the pass.
                self._dropped_unknown += 1
                continue
            except OSError:
                return
            self._on_datagram(datagram, addr)

    def _check_timers(self):
        now = time.monotonic()
        for p in list(self._peers.values()):
            if p.closed or not p.outbox or p.timer is None:
                continue
            if now < p.timer:
                continue

            base = p.outbox.get(p.base)
            if base is None:
                self._restart_timer(p)
                continue

            base.retries += 1
            if base.retries > MAX_RETRIES:
                # Clause 1: in-order once-each delivery has become impossible,
                # so the peer is reported closed.  peers() stops listing it and
                # stats() reports state "closed".
                self._shutdown_peer(
                    p,
                    "no acknowledgment for sequence %d after %d retransmissions"
                    % (base.seq, MAX_RETRIES),
                )
                continue

            # Karn's algorithm: a retransmitted message never yields an RTT
            # sample, because the ACK that comes back cannot be attributed to
            # one transmission or the other.
            p.rto = min(MAX_RTO, p.rto * 2.0)  # exponential backoff

            # Go-Back-N: everything still in flight goes again, oldest first.
            for seq in sorted(p.outbox):
                pend = p.outbox[seq]
                pend.rtt_ok = False
                p.resent += 1
                self._transmit(p, pend.datagram)
            p.timer = time.monotonic() + p.rto

    def _restart_timer(self, p):
        p.timer = (time.monotonic() + p.rto) if p.outbox else None

    def _next_event(self):
        """Absolute time of the next thing we must wake up for.

        With no timer running and no datagram waiting in the delay queue there
        is nothing to wake up *for*: select() will return the moment something
        arrives, so the ceiling is only a safety net.
        """
        best = time.monotonic() + _POLL_CEILING
        for release, _d, _a in self._pipe:
            if release < best:
                best = release
        for p in self._peers.values():
            if not p.closed and p.timer is not None and p.timer < best:
                best = p.timer
        return best

    def _wait(self, until):
        """Block in select() until `until`.

        One select() for the whole remaining interval, not a string of short
        ones: recv(timeout=0.05) has to come back at roughly 50 ms, and three
        chained 20 ms waits land at 62 on a host whose scheduler rounds up.
        """
        if self._sock is None:
            return
        gap = until - time.monotonic()
        if gap <= 0:
            gap = 0
        elif gap > _POLL_CEILING:
            gap = _POLL_CEILING
        try:
            select.select([self._sock], [], [], gap)
        except (OSError, ValueError):
            pass

    # -- sending -------------------------------------------------------

    def _fill_window(self, p):
        """Move backlogged application messages into the window."""
        while p.backlog and len(p.outbox) < self.window and not p.sent_fin:
            data = p.backlog.pop(0)
            seq = p.next_seq
            p.next_seq += 1
            datagram = _pack(
                T_DATA, self._flags(), p.conn_id, seq, p.recv_next - 1, data
            )
            p.sent += 1
            self._install(p, seq, datagram)

    def _install(self, p, seq, datagram, is_fin=False):
        """Put a datagram in the window and transmit it for the first time."""
        pend = _Pending(seq, datagram, time.monotonic(), is_fin=is_fin)
        p.outbox[seq] = pend
        if len(p.outbox) == 1:
            p.base = seq
            p.timer = time.monotonic() + p.rto
        self._transmit(p, datagram)

    def _transmit(self, p, datagram):
        """One datagram towards one peer, through loss and delay simulation."""
        p.datagrams_sent += 1
        if self.loss > 0.0 and self._rng.random() < self.loss:
            # Loss applies to every datagram we emit, ACKs included.  Dropping
            # only data would make the loss settings far easier than they look.
            p.loss_tx += 1
            return
        if self.delay > 0.0:
            self._pipe.append((time.monotonic() + self.delay, datagram, p.addr))
            return
        self._emit(datagram, p.addr)

    def _emit(self, datagram, addr):
        if self._sock is None:
            return
        try:
            self._sock.sendto(datagram, addr)
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            pass

    def _send_ack(self, p, msg_type=T_ACK):
        ack = _pack(
            msg_type, self._flags(), p.conn_id, HANDSHAKE_SEQ, p.recv_next - 1
        )
        p.acks_sent += 1
        self._transmit(p, ack)

    # -- receiving -----------------------------------------------------

    def _on_datagram(self, datagram, addr):
        msg = _unpack(datagram)
        if msg is None:
            # Corrupt, truncated, or a version we do not speak.
            self._dropped_unknown += 1
            return

        key = (addr, msg["conn_id"])
        p = self._by_key.get(key)

        if msg["type"] == T_SYN:
            self._on_syn(msg, addr, p)
            return

        if p is None or p.closed:
            # Nothing here belongs to a live connection.  A stale datagram from
            # a previous incarnation of a peer lands here and is discarded
            # rather than being mixed into the current stream.
            self._dropped_unknown += 1
            return

        kind = msg["type"]
        if kind == T_SYNACK:
            self._on_synack(p, msg)
        elif kind == T_ACK:
            self._on_ack(p, msg)
        elif kind == T_DATA:
            self._on_data(p, msg)
        elif kind == T_FIN:
            self._on_fin(p, msg)
        elif kind == T_FINACK:
            self._on_ack(p, msg)
            p.closed = True
            p.close_reason = p.close_reason or "peer acknowledged our FIN"
        else:
            p.dropped += 1

    def _on_syn(self, msg, addr, existing):
        """Host side of the handshake.  Idempotent under retransmission."""
        if existing is not None and not existing.closed:
            # Duplicate SYN: the client did not get our SYNACK.  Our SYNACK is
            # in the window and its own timer will resend it, but answering
            # here makes the handshake converge faster.
            existing.dropped += 1
            pend = existing.outbox.get(HANDSHAKE_SEQ)
            if pend is not None:
                existing.resent += 1
                pend.rtt_ok = False
                self._transmit(existing, pend.datagram)
            else:
                # Handshake already finished here; the client's SYN and our
                # SYNACK simply crossed.  A bare ACK settles it.
                self._send_ack(existing)
            return

        # A SYN from an address we already have a peer on, with a different
        # conn_id, is a new incarnation of that client.  Retire the old one so
        # the application sees a departure and an arrival rather than a peer
        # whose stream silently restarts.
        for key, old in list(self._by_key.items()):
            if key[0] == addr and not old.closed and key[1] != msg["conn_id"]:
                self._shutdown_peer(old, "replaced by a new connection")

        peer = _Peer(self._next_peer_id, addr, msg["conn_id"], is_client=False)
        self._next_peer_id += 1
        peer.recv_next = HANDSHAKE_SEQ + 1  # the SYN itself was sequence 0
        peer.established = True
        self._peers[peer.id] = peer
        self._by_key[(addr, msg["conn_id"])] = peer

        # The SYNACK occupies sequence 0 and goes into the window like any
        # other message, so the ordinary retransmission timer covers it and the
        # client's ACK gives us a round-trip sample straight away.
        synack = _pack(
            T_SYNACK, self._flags(), peer.conn_id, HANDSHAKE_SEQ, peer.recv_next - 1
        )
        self._install(peer, HANDSHAKE_SEQ, synack)

    def _on_synack(self, p, msg):
        """Client side of the handshake."""
        if p.recv_next <= HANDSHAKE_SEQ:
            p.recv_next = HANDSHAKE_SEQ + 1
        self._on_ack(p, msg)  # acknowledges our SYN, gives us the first sample
        p.established = True
        self._send_ack(p)  # third leg, so the host also gets a sample

    def _on_ack(self, p, msg):
        """Cumulative acknowledgment: everything up to msg['ack'] is safe."""
        acked = msg["ack"]
        newest = None
        for seq in list(p.outbox):
            if seq <= acked:
                pend = p.outbox.pop(seq)
                if newest is None or pend.seq > newest.seq:
                    newest = pend
                if pend.is_fin:
                    p.closed = True
                    p.close_reason = p.close_reason or "FIN acknowledged"

        if newest is None:
            return  # a duplicate ACK, nothing new

        if newest.rtt_ok:
            p.sample_rtt(time.monotonic() - newest.sent_at)
        else:
            p.rto = min(MAX_RTO, max(MIN_RTO, p.rto))

        p.base = min(p.outbox) if p.outbox else p.next_seq
        self._restart_timer(p)
        if not p.established and not p.is_client:
            p.established = True

    def _on_data(self, p, msg):
        seq = msg["seq"]
        self._on_ack(p, msg)  # DATA carries a piggybacked acknowledgment

        if seq == p.recv_next:
            p.recv_next += 1
            p.received += 1
            self._inbox.append((p.id, msg["payload"]))
        else:
            # Either a duplicate we have already delivered, or the message
            # after a gap.  Go-Back-N keeps no reassembly buffer: discard it
            # and re-acknowledge, which tells the sender where to resume.
            p.dropped += 1
        self._send_ack(p)

    def _on_fin(self, p, msg):
        seq = msg["seq"]
        self._on_ack(p, msg)
        if seq != p.recv_next:
            # Out of order: data we have not delivered yet is still coming.
            p.dropped += 1
            self._send_ack(p)
            return
        p.recv_next += 1
        p.got_fin = True
        self._send_ack(p, T_FINACK)
        self._shutdown_peer(p, "peer sent FIN")

    # -- teardown ------------------------------------------------------

    def _begin_close(self, p, linger=CLOSE_LINGER):
        """Flush what we can, then put a FIN at the end of the stream."""
        if p.sent_fin or p.closed:
            return
        end = time.monotonic() + linger
        while (p.backlog or p.outbox) and time.monotonic() < end and not p.closed:
            self._service()
            self._wait(min(end, self._next_event()))
        if p.closed:
            return
        p.backlog = []
        seq = p.next_seq
        p.next_seq += 1
        fin = _pack(T_FIN, self._flags(), p.conn_id, seq, p.recv_next - 1)
        p.sent_fin = True
        self._install(p, seq, fin, is_fin=True)

    def _shutdown_peer(self, p, reason):
        """Mark a peer closed.  The object stays for stats(); peers() drops it."""
        if not p.closed:
            p.closed = True
            p.close_reason = reason
        p.outbox.clear()
        p.backlog = []
        p.timer = None
        self._by_key.pop((p.addr, p.conn_id), None)

    # -- diagnostics ---------------------------------------------------

    def fileno(self):
        """The UDP socket's descriptor, so a caller can select() on us.

        Not one of the required seven.  An application does not need it, 
        recv(timeout=...) is the supported way to wait, but a program that
        drives several Transport objects from one thread, as the conformance
        test does, can then block on all of them at once instead of polling
        each in turn.
        """
        return -1 if self._sock is None else self._sock.fileno()

    def next_event_in(self):
        """Seconds until the next retransmission timer or delayed datagram."""
        return max(0.0, self._next_event() - time.monotonic())

    def why_closed(self, peer):
        """Why a peer was reported closed.  Not part of the required seven."""
        p = self._peers.get(peer)
        return None if p is None else p.close_reason


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _env_float(name, default, low, high):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError("%s=%r is not a number" % (name, raw))
    if not (low <= value <= high):
        raise ValueError("%s=%r is outside [%g, %g]" % (name, raw, low, high))
    return value


def _env_int(name, default):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError("%s=%r is not an integer" % (name, raw))
