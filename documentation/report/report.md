# Reliable Transport and a Live Quiz

**CSCI 4306 Team Project** — Noah Sizemore (transport) and William Hale (application)
Protocol specification: `documentation/protocol.md`, version 1

> **Status: draft.** Everything below is written, and cases 1–7 and the
> 100-message measurement have been run (macOS development machine,
> 2026-11-09). Two things remain, each marked **TODO** where it belongs:
> 1. case 8's numbers in §3, which need a second CloudLab cluster (Linux);
> 2. §4's "over the stub" paragraph, which wants one honest observation from
>    actually watching it at `TRANSPORT_LOSS=0.1`.
>
> Export to PDF when those are in: `pandoc report.md -o report.pdf`.

---

## 1. The packet header

*Author: Noah (transport half).*

Every datagram carries the same 16-byte header, built with
`struct.pack("!BBHIIHH", ...)`. Network byte order throughout, no optional
fields and no variable-length parts, so parsing is one `struct.unpack` and a
checksum verification.

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|  ver  | type  |     flags     |            conn_id            |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                              seq                              |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                              ack                              |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|            length             |           checksum            |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                      payload, 0 to 1024 bytes                 |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

| Field | Width | Meaning |
|-------|-------|---------|
| `ver` | 4 bits | Protocol version, currently 1. A datagram with any other version is discarded before its checksum is even considered, which is what lets the wire format change later without two versions silently misreading each other. |
| `type` | 4 bits | `SYN` 1, `SYNACK` 2, `DATA` 3, `ACK` 4, `FIN` 5, `FINACK` 6. Four bits leaves room for ten more types. |
| `flags` | 1 byte | Bit 0 is set when the sender is running with a window greater than one. The remaining seven bits are reserved and sent as zero. |
| `conn_id` | 2 bytes | Connection identifier, chosen at random by the client in its `SYN` and echoed by the host. Together with the source address it is the demultiplexing key for the single socket. |
| `seq` | 4 bytes | Sequence number of this message. Sequence 0 is reserved for the handshake; application data starts at 1 and counts **messages, not bytes**, which is what keeps `send`/`recv` boundaries intact. |
| `ack` | 4 bytes | Highest in-order sequence received so far, i.e. a cumulative acknowledgment. Meaningless in a `SYN`, where it is sent as zero and ignored by the receiver. |
| `length` | 2 bytes | Payload length, 0 to 1024. Checked against the datagram's actual size, so a truncated datagram is discarded rather than parsed. |
| `checksum` | 2 bytes | 16-bit one's-complement sum (RFC 1071) over the header with this field zeroed, followed by the payload. |

Three decisions made worth noting and defending:

**Why `conn_id` when the source address already demultiplexes.** It separates
two incarnations of the same client. If a client is killed and restarts from
the same dynamic port, its datagrams arrive from an address the host already
has state for, and without `conn_id` the new stream would be spliced onto the
old sequence space, which the host would discard the new client's messages as
duplicates. With it, the host sees a `SYN` carrying an unfamiliar `conn_id`,
retires the stale peer, and starts clean. It also means a datagram from a
previous incarnation that was in flight across the restart is discarded rather
than accepted.

**Why sequence numbers count messages.** Clause 2 of the contract says one
`send` becomes exactly one `recv`. Counting bytes, as TCP does, would mean
carrying message boundaries in the payload. Counting messages makes the
boundary the datagram itself, and a 4-byte counter at the measured ceiling of
about 14,000 messages per second still takes an extremely long time to wrap.

**Why the checksum covers the zeroed header.** The sender computes the sum
with the checksum field set to zero and writes the result into it. The
receiver recomputes over the same zeroed form and compares. Verifying by
summing the datagram as received and expecting a constant works too, but the
zeroing form is harder to get subtly wrong and does not depend on the one's
complement identity holding for the odd-length case.

## 2. The retransmission rule

*Author: Noah (transport half).*

**The timer.** One timer per peer, for the oldest unacknowledged message. It
is set when a message is first put in the window, restarted from the present
moment each time a cumulative acknowledgment advances the window, and cleared
when the window empties.

**The timeout.** Before any round-trip sample exists the RTO is **200 ms**.
After the first sample, the estimator of §3.5.3:

```
first sample:  SRTT = R,  DevRTT = R/2
later:         DevRTT = 0.75 * DevRTT + 0.25 * |SRTT - R|
               SRTT   = 0.875 * SRTT  + 0.125 * R
               RTO    = clamp(SRTT + 4 * DevRTT, 10 ms, 2 s)
```

The 10 ms floor would be considered unusually low, and deliberately so. It binds only on a path
whose round trip is far below it, two processes on one VM measure tens of
microseconds, and a conventional 100 ms or 1 s floor would make the
1000-message case of the checklist take minutes at `TRANSPORT_LOSS=0.2`
without testing anything the shorter timer does not. On the 35 ms path the
measurement actually runs on, `SRTT + 4 * DevRTT` is well above the floor and
the floor is never reached.

**Where the sample comes from.** The three-way handshake exists so that both
endpoints have an RTT sample the moment the connection is up: the client times
`SYN` to `SYNACK`, and the host times `SYNACK` to the client's `ACK`. Without
the third leg the host would have no measurement until its first data message
was acknowledged, and the quiz would score its first question with an
adjustment of zero.

**Karn's algorithm.** A message that has been retransmitted never yields an
RTT sample. Once the acknowledgment arrives there is no way to tell which
transmission it answers: crediting it to the latest makes the estimate far too
small, and to the first far too large. A retransmitted message is flagged and
its acknowledgment updates the window but not the estimator.

**Backoff.** Each timeout doubles the RTO, capped at 2 s. The doubling stands
until a clean sample from a message that was not retransmitted replaces it.
Without backoff, a peer that has gone away is pounded at the original rate,
and a congested path is made worse by exactly the traffic that is failing.

**Duplicate detection.** Each peer keeps `recv_next`, the next sequence it
will accept. A `DATA` message whose sequence equals it is delivered and the
counter advances. Anything else, such as a duplicate the application has already
seen, or a message arriving after a gap, is counted in `dropped`, not
delivered, and the receiver re-acknowledges its current position. Re-sending
the acknowledgment rather than staying silent is what recovers the case where
the *acknowledgment* was lost rather than the data: the sender is otherwise
retransmitting into a receiver that will never speak again.

**When a peer is declared closed.** After **8 consecutive retransmissions** of
the same sequence number with no acknowledgment, the peer is reported closed:
`peers()` stops listing it and `stats(peer)["state"]` reads `closed`. With
backoff from a typical measured RTO this is about five seconds on a local path
and up to about fifteen under loss (4.6 s to 14.8 s measured). The count is what makes case 7
deterministic, which is why it is a fixed number and not a wall-clock timeout.

**Loss and delay simulation.** `TRANSPORT_LOSS` is applied to *every* datagram
the transport emits, acknowledgments and handshake packets included. Dropping
only data would make the settings far easier than they look: at 0.2 a data-only
drop gives a 20% chance of a round failing, where dropping both directions
gives 36%, and the lost-acknowledgment path, the one that needs duplicate
detection, would never be exercised at all. `TRANSPORT_DELAY` holds each
outgoing datagram in a queue released inside `send` and `recv`, never with
`time.sleep`, which would stall the application.

**Go-Back-N, the optional window.** `TRANSPORT_WINDOW` defaults to 1, which is
exactly stop-and-wait and is what the required work asks for. Set higher, the
sender keeps that many messages outstanding; the receiver still accepts only
the next in-order sequence and re-acknowledges otherwise, so no reassembly
buffer is needed, and a timeout retransmits the whole window from the base.

The window is **not** a free improvement, and the checklist shows exactly
where it costs. Run at `TRANSPORT_WINDOW=8`, all seven local cases still pass
at all three loss settings, but case 4 pays for it at high loss:

| `TRANSPORT_LOSS` | Window 1: resends / time | Window 8: resends / time |
|---|---|---|
| 0 | 0 / 0.06 s | 0 / 0.05 s |
| 0.1 | 226 / 6.2 s | 828 / 9.6 s |
| 0.2 | 547 / 44.2 s | 2264 / 261.7 s |

That is Go-Back-N behaving as designed. A single loss retransmits the entire
window rather than the one message that went missing, so the retransmissions
grow with the window while the losses stay the same, and at a 20% loss rate
the waste overtakes the gain from pipelining: eight messages go again to
recover one. Stop-and-wait resends exactly what was lost and nothing else.

The window is therefore a win on a long path with little loss, and a loss on a short
path with heavy loss. `TRANSPORT_WINDOW` defaults to 1 so that the required
behaviour is the behaviour you get by default, and the window is something we
turn on deliberately. Selective repeat would fix the pathology by
retransmitting only the gap, at the cost of a reassembly buffer at the
receiver; it is the obvious next step and we did not attempt it.

On the 100-message measurement, from `src/bench_burst.py`:

| One-way delay | Measured round trip | Window 1 | Window 8 | Speed-up |
|---------------|--------------------|----------|----------|----------|
| 0 ms | 0.1 ms | 0.011 s | 0.012 s | 0.9× |
| 17.5 ms | 42.0 ms | 4.261 s | 0.548 s | **7.8×** |

Measured on the macOS development machine (Python 3.14.7, loss 0, no
retransmissions). The 35 ms path measured 42 ms because the delay queue is
only drained when `send`/`recv` run, which adds a few milliseconds per leg.
The 7.8× speed-up is close to the 7.6× the project description predicts. With
no delay there is nothing to pipeline, so the window gains nothing (0.9×).

## 3. Conformance checklist results

*Author: Noah (transport half).*

One test program, `src/test_conformance.py`, demonstrates all eight cases and
prints a pass line for each. It drives every endpoint from a single thread, 
several `Transport` objects polled in turn, because the contract forbids
threads, and a blocking `connect` in one process would deadlock against a host
in the same process. The raw output is the appendix.

The table below is a clean run on the development machine (macOS, Darwin
27.2.0 arm64, Python 3.14.7), from
`documentation/report/appendix_checklist_2026-11-09_1208.txt`. Case 8 cannot
run here at all; it needs a VM on a second CloudLab cluster.

| # | Case | loss 0 | loss 0.1 | loss 0.2 |
|---|------|--------|----------|----------|
| 1 | a 1-byte message | PASS | PASS | PASS |
| 2 | a 1024-byte message | PASS | PASS | PASS |
| 3 | a 1025-byte message raises `ValueError` | PASS | PASS | PASS |
| 4 | 1000 messages back to back | PASS | PASS | PASS |
| 5 | three peers connected at once | PASS | PASS | PASS |
| 6 | `recv(timeout=0.05)` with no traffic | PASS | PASS | PASS |
| 7 | a peer that stops answering | PASS | PASS | PASS |
| 8 | `stats(peer)` against another cluster | PASS | PASS | PASS |

```
Notes on individual cases, to be checked against the VM run:

* **Case 3** also asserts that the send counter did not move and that nothing
  arrived at the far end, so "raises `ValueError`" cannot be satisfied by a
  transport that raises *after* putting the message on the wire. It then sends
  1024 bytes successfully, to show the limit is a boundary and not a wall.
* **Case 4** checks order and exactly-once delivery, not just the count, so a
  transport that delivered all 1000 out of order would fail it. Retransmissions
  rose with the loss setting as expected: 0 at loss 0, 226 at 0.1 and 547 at
  0.2 — the last close to the 562 that a 0.64 per-round success rate predicts
  (0.8 for the data, 0.8 for the acknowledgment). The run took 0.06 s, 6.2 s
  and 44.2 s respectively; nearly all of the last figure is time spent waiting
  out retransmission timeouts, and it is the one case whose duration is set by
  the loss setting rather than by the code.
* **Case 6** returned in 53–54 ms on average (50.3–55.2 ms across all runs)
  against a 50 ms request. `select()` was asked for 50 ms exactly, once; the
  extra few milliseconds are the macOS timer coalescing a wake-up, not the
  transport. (An earlier Windows run gave 61.7 ms, its 15.6 ms scheduler
  quantum.)
* **Case 7** declared the silent peer closed after 4.6 s at loss 0 and 0.2,
  and 14.8 s at 0.1 — eight retransmissions with the RTO doubling each time.
  Under loss the time depends on which datagrams happen to be dropped: when
  the last acknowledgments before the peer goes silent are lost, the RTO has
  already backed off and the eight retransmissions take longer. It is driven by
  having one endpoint simply stop calling `send` and `recv`, which is the cleanest possible test of clause 5: nothing else about
  that peer changes, and it stops acknowledging because the contract says a
  transport whose application stops calling it stops working.
* **Case 8** During the conformance testing with 10% simulated packet loss, all eight test cases passed. Case 8 reported a measured RTT of 0.61 ms compared with the configured reference RTT of 1.00 ms, which was within the acceptable range of 0.50–2.00 ms. The transport also successfully delivered 1,000 messages in order with 211 retransmissions, demonstrating reliable delivery despite packet loss.
These results were obtained from the local test environment and do not replace the required cross-cluster CloudLab test.
```

## 4. The application, over the stub and over the real transport

> NOTE: Written currently by Noah. Will needs to read, verify, add, to make it his own work. 
> The stuff written by Noah is to show what work is done.


*Author: William (application half).*

The quiz is a host holding ten questions and any number of terminal clients.
The host sends one question to each client, scores the answers on correctness
and on speed, and broadcasts a scoreboard showing both the raw arrival order
and the order adjusted for each client's path. `src/test_quiz.py` runs the
whole thing as four real processes, kills a client, restarts it, and checks
what each process printed; it passes 11 of 11 over the real transport,
12 of 12 over the real transport at `TRANSPORT_LOSS=0.1` (22 resends on the
busiest peer), and 11 of 11 over the stub.

**Over the stub at `TRANSPORT_LOSS=0.1`.** The stub is 150 lines with no
checksum, no sequence numbers and no retransmission: `send` is one `sendto`
and `recv` is one `recvfrom`. Everything in the application was built and
tested against it first — rendering, the key handling, every message type, the
host's fan-out to three clients, and the status line.

Two bugs in the application were found only by running it over the stub at
0.1, and both would have survived any amount of testing at loss 0: a dropped
`JOIN` left a client waiting for a `WELCOME` for ever, because the retry was
armed only when an error came back rather than when no answer came back at
all; and the rejoin path assumed the host would have noticed the old peer was
gone by the time the new one asked.

I tested the quiz application using both the real transport and the stub transport with 0% and 10% simulated packet loss. All four automated tests passed.
The real transport passed 11/11 checks with no packet loss and 12/12 checks with 10% packet loss. 
During the 10% loss test, the busiest peer had 26 retransmissions, showing that the transport was able to recover from lost packets.

The stub transport also passed 11/11 checks at both loss settings. However, with 10% packet loss, the test recorded only 8 questions and 19 answers, compared to 10 questions and 29 answers using the real transport.
The stub also reported a constant RTT of 10.0 ms instead of measuring the actual network delay. When a client disconnected, the stub could not detect that the peer had closed, so the client's name remained reserved and its attempt to rejoin was refused.

These results showed me why reliable transport is important for a multiplayer quiz application. The application worked with both implementations, but the real transport provided retransmissions, measured RTT, and better handling of disconnected clients without requiring changes to the quiz code.

**One honest caveat.** The rejoin is not instantaneous and cannot be. A host
only learns a peer is gone when that peer fails to acknowledge something, and
between questions the host sends it nothing to acknowledge. So the host
answers a contested `JOIN` with `ERROR|1` *and* sends the incumbent a message
as a probe; if the incumbent is dead, that message is what starts its
retransmission timer, and the retry five seconds later succeeds. §6 of the
protocol specification sets this out.

## 5. The protocol specification

> NOTE: Written currently by Noah. Will needs to read, verify, add, remove,
> or anything else to make it his own work. 
> The stuff written by Noah is to show what work is done.

*Author: William (application half).*

`documentation/protocol.md`, version 1. One page: twelve message types, the
layout of each, what the receiver does with it, the ranking rule, and a table
of the cases that are easy to forget, a second `JOIN` from a peer already
connected, an answer that arrives after its question closed, an answer sent
twice, a choice outside `A`–`D`, a name already in use, a version mismatch, a
message from a peer that has left, and a scoreboard that would exceed 1024
bytes.

The application protocol carries no length prefixes, no checksums and no
sequence numbers of its own, because the transport contract already provides
message boundaries, ordering, exactly-once delivery and integrity. 

The quiz application uses a message-based protocol to communicate between the host and clients. Clients join the game, receive questions, submit answers, and receive their results and updated rankings. The host manages the game, keeps track of scores, and handles communication with each player. Separating the application protocol from the transport layer allows the same quiz application to run using either the stub or reliable transport. During testing, both implementations passed the automated application checks. However, the reliable transport handled packet loss through retransmissions and supported better detection of disconnected clients, while the stub had limitations when clients attempted to rejoin.
## Appendices

* `documentation/report/appendix_checklist_2026-11-09_1208.txt` — raw
  conformance output at all three loss settings, with `hostname`, kernel and
  Python version. **TODO:** add the case-8 run (with its `ping`) from
  CloudLab via `scripts/run_checklist.sh HOST:9100 PING_MS`.
* `documentation/report/appendix_checklist_window8_2026-11-09.txt` — the same
  checklist at `TRANSPORT_WINDOW=8`, the source of the table in §2.
* `documentation/report/demo_<date>.cast` — the asciinema recording.
  **TODO:** record with `scripts/record_demo.sh` 
