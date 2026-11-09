"""Stub transport: the seven methods, none of the machinery.

This is the file the application half is built against before the real
transport exists, and the fallback the project description tells us to keep in
the repository afterwards.  It has the same module name and the same class
name as the real transport, so nothing in the application changes when the two
are swapped:

    run over the stub:  PYTHONPATH=src/stub python3 src/quiz_host.py
    run over the real:  python3 src/quiz_host.py

No checksum, no sequence numbers, no acknowledgments, no retransmission.  One
send() is one sendto(), one recv() is one recvfrom().  What it does have is the
three behaviors that make the application exercise its network-facing code:

  * it discards TRANSPORT_LOSS of the datagrams it sends,
  * it returns a stats() dict with the required keys,
  * it reports rtt_ms as twice TRANSPORT_DELAY, which is the right answer for
    a transport that waits for nothing, and is what the quiz's adjusted
    ranking needs in order to do anything at all.

Messages larger than 1024 bytes raise ValueError here too, so the application
cannot grow a dependency on a limit the real transport does not have.
"""

import os
import random
import select
import socket
import time

MAX_PAYLOAD = 1024


class PeerClosed(ConnectionError):
    """Mirrors the real transport's exception so applications can catch it."""


class Transport:
    def __init__(self):
        self.loss = float(os.environ.get("TRANSPORT_LOSS") or 0.0)
        self.delay = float(os.environ.get("TRANSPORT_DELAY") or 0.0) / 1000.0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        self._bound = False
        self._addr_to_id = {}
        self._id_to_addr = {}
        self._next_id = 1
        self._pipe = []  # (release_time, datagram, addr), the delay queue
        self._counts = {}

    # -- the seven methods ---------------------------------------------

    def bind(self, port):
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("", int(port)))
        self._bound = True
        return self._sock.getsockname()[1]

    def connect(self, host, port):
        if not self._bound:
            self._sock.bind(("", 0))
            self._bound = True
        addr = (socket.gethostbyname(host), int(port))
        peer = self._peer_for(addr)
        self.send(peer, b"")  # an empty datagram so the host learns our address
        return peer

    def send(self, peer, data):
        if isinstance(data, (bytearray, memoryview)):
            data = bytes(data)
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        if len(data) > MAX_PAYLOAD:
            raise ValueError("message is %d bytes, the limit is 1024" % len(data))
        addr = self._id_to_addr.get(peer)
        if addr is None:
            raise PeerClosed("no such peer: %r" % (peer,))
        self._counts[peer]["sent"] += 1
        self._service()
        if self.loss > 0.0 and random.random() < self.loss:
            return len(data)  # discarded, exactly as a lossy path would
        if self.delay > 0.0:
            self._pipe.append((time.monotonic() + self.delay, data, addr))
        else:
            self._sock.sendto(data, addr)
        return len(data)

    def recv(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            self._service()
            while True:
                try:
                    datagram, addr = self._sock.recvfrom(65535)
                except (BlockingIOError, InterruptedError, OSError):
                    break
                peer = self._peer_for(addr)
                if datagram == b"":
                    continue  # the connect() probe, not an application message
                self._counts[peer]["received"] += 1
                return (peer, datagram)
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                return None
            gap = 0.02 if deadline is None else min(0.02, deadline - now)
            if self._pipe:
                gap = min(gap, max(0.0, min(r for r, _, _ in self._pipe) - now))
            try:
                select.select([self._sock], [], [], max(0.0, gap))
            except (OSError, ValueError):
                return None

    def peers(self):
        return list(self._id_to_addr)

    def close(self, peer=None):
        if peer is None:
            self._sock.close()
            self._id_to_addr.clear()
            self._addr_to_id.clear()
            return
        addr = self._id_to_addr.pop(peer, None)
        self._addr_to_id.pop(addr, None)

    def stats(self, peer):
        c = self._counts.get(peer)
        if c is None:
            raise PeerClosed("no such peer: %r" % (peer,))
        return {
            "rtt_ms": round(self.delay * 2000.0, 3),  # twice the one-way delay
            "sent": c["sent"],
            "received": c["received"],
            "resent": 0,  # a transport that never retransmits
            "dropped": 0,
            "state": "established",
            "window": 1,
        }

    # -- internals ------------------------------------------------------

    def _peer_for(self, addr):
        peer = self._addr_to_id.get(addr)
        if peer is None:
            peer = self._next_id
            self._next_id += 1
            self._addr_to_id[addr] = peer
            self._id_to_addr[peer] = addr
            self._counts[peer] = {"sent": 0, "received": 0}
        return peer

    def _service(self):
        """Release delayed datagrams.  Never time.sleep()."""
        if not self._pipe:
            return
        now = time.monotonic()
        keep = []
        for release, datagram, addr in self._pipe:
            if release <= now:
                try:
                    self._sock.sendto(datagram, addr)
                except OSError:
                    pass
            else:
                keep.append((release, datagram, addr))
        self._pipe = keep
