# Live Quiz Protocol — version 1

CSCI 4306 Team Project, application half.
Speaks over the reliable message transport defined in `src/transport.py`.
Version 1, frozen 2026-11-09. Both halves build against this number; if
anything below changes, the number changes with it and `JOIN`/`WELCOME` start
rejecting the other version.

---

## 1. What the transport already provides

The application is built on the seven-method transport contract, so this
protocol does **not** restate any of it. In particular there are no length
prefixes, no checksums, no sequence numbers and no retransmissions here,
because the layer below guarantees:

* messages arrive in the order they were sent, once each, with nothing
  missing, or the peer is reported closed;
* one `send` becomes exactly one `recv`, never joined and never split;
* a message is at most 1024 bytes.

Everything the application knows about the network it learns from
`stats(peer)` and from `peers()`.

## 2. Framing

One application message is exactly one transport message.

```
<TYPE>|<field>|<field>|...
```

* UTF-8 text, no trailing newline.
* `|` (U+007C) separates fields and may not appear inside one.
* `;` (U+003B) separates the sub-fields of a scoreboard row, and may not
  appear inside one.
* The first field is the message type, in upper case.
* A message is never longer than 1024 bytes. The only message that could grow
  past that is `SCORE`, and §6 says what the host does about it.

Text is chosen over a packed binary layout deliberately: the transport has
already solved integrity and framing, so the application gains nothing from
bit-packing, and a readable wire format is worth a great deal when the
transport underneath it is still being debugged.

## 3. Client → host

| # | Message | Fields |
|---|---------|--------|
| 1 | `JOIN` | `ver`, `name` |
| 2 | `ANSWER` | `qid`, `choice` |
| 3 | `LEAVE` | — |
| 4 | `PING` | `token` |

**`JOIN|<ver>|<name>`** — the first message a client sends.
`ver` is the protocol version, `1`.
`name` is 1 to 16 characters of `[A-Za-z0-9_-]`, and is the identity the
scoreboard is keyed on, not the transport's peer id. That is what lets a
score survive a disconnect: peer ids change when a client reconnects, names
do not.

**`ANSWER|<qid>|<choice>`** — `qid` is the question id from `QUESTION`,
`choice` is one of `A` `B` `C` `D`. Sending the question id back rather than
"my answer to whatever is current" is what makes a late answer detectable
instead of being misattributed to the next question.

**`LEAVE`** — a clean goodbye. The player keeps their score and is shown
offline. A client that is killed instead sends nothing, and the host finds out
when the transport reports the peer closed; the scoreboard outcome is the same.

**`PING|<token>`** — `token` is any 1 to 8 character printable string. The host
answers `PONG` with the same token. Nothing in the quiz depends on it: its
purpose is to keep traffic flowing in both directions while no question is
open, so that `stats(peer)["rtt_ms"]` on the **host** side stays a recent
measurement rather than a stale one from the last question. A client sends one
every two seconds while idle.

## 4. Host → client

| # | Message | Fields |
|---|---------|--------|
| 5 | `WELCOME` | `ver`, `name`, `questions`, `limit_s` |
| 6 | `QUESTION` | `qid`, `index`, `total`, `limit_s`, `text`, `a`, `b`, `c`, `d` |
| 7 | `RESULT` | `qid`, `correct`, `yours`, `ok`, `raw_ms`, `adj_ms`, `points`, `total` |
| 8 | `SCORE` | `qid`, `n`, `row` × n |
| 9 | `REVEAL` | `qid`, `correct`, `tally` |
| 10 | `BYE` | `reason` |
| 11 | `ERROR` | `code`, `text` |
| 12 | `PONG` | `token` |

**`WELCOME|1|<name>|<questions>|<limit_s>`** — accepted. `name` is echoed
back because the host may have had to reject what was asked for. `questions`
is how many are in the bank, `limit_s` the answer window in seconds.

**`QUESTION|<qid>|<index>|<total>|<limit_s>|<text>|<a>|<b>|<c>|<d>`** — one
question, sent to each connected client in turn. `index` is 1-based.
The host records the moment of each individual `send` call, per peer, and that
timestamp is the zero point for that client's answer. It is recorded per peer
because the host sends to its peers in a loop, and at a window of one the
later peers are measurably later.

**`RESULT|<qid>|<correct>|<yours>|<ok>|<raw_ms>|<adj_ms>|<points>|<total>`** —
sent to the one client that answered. `ok` is `1` or `0`, `raw_ms` and
`adj_ms` are §5, `points` is what this answer earned and `total` the running
score.

**`SCORE|<qid>|<n>|<row>|...`** — the scoreboard, broadcast whenever it
changes. `qid` is the question the timings in the rows belong to, or `0` when
no question has been asked yet. Each row is

```
name;score;online;answered;raw_ms;adj_ms;rank_raw;rank_adj;correct
```

* `online` — `1` while a transport peer is attached to this name, else `0`
* `answered` — `1` if this player answered question `qid`
* `raw_ms`, `adj_ms` — this player's timings for question `qid`, `-1` if none
* `rank_raw` — placing by arrival time, 1 is first, `0` if they did not answer
* `rank_adj` — placing by adjusted time, the ranking the quiz is scored on
* `correct` — `1`, `0`, or `-` if they did not answer

**`REVEAL|<qid>|<correct>|<tally>`** — the question has closed. `tally` is
four counts, `a;b;c;d`, of how many players chose each option.

**`BYE|<reason>`** — the host is dropping this client; it closes the peer
immediately afterwards. Sent on version mismatch and at host shutdown.

**`ERROR|<code>|<text>`** — the last message was not acted on. The connection
stays up. Codes are in §6.

**`PONG|<token>`** — the answer to `PING`.

## 5. Ranking, and why half the round trip

The host times every answer twice.

```
raw_ms = arrival_at_host - question_sent_to_that_peer
adj_ms = raw_ms - stats(peer)["rtt_ms"] / 2
```

`raw_ms` is what the host saw. It is not a fair ranking: a client one cluster
away is tens of milliseconds closer than one on the far side of the country,
and that difference is in the number whether the player was quick or not.
`adj_ms` subtracts an estimate of the path, and the scoreboard shows both
rankings side by side so the difference is visible. Scoring uses `adj_ms`.

`rtt_ms / 2` is an estimate of the one-way delay and not a measurement of
it. The host can only time a round trip, because it has the only clock
involved: it stamps a message on the way out and reads the clock again when
the acknowledgment covering it comes back. Nothing in that measurement says
how the total split between the two directions. Halving it assumes the two
legs were equal, and on a real path they are not: routing is often asymmetric,
so the packets need not even traverse the same links in both directions, and
queueing is asymmetric too, because the load on the two directions is
independent. The true one-way delay lies somewhere in the round trip, and
`rtt_ms / 2` is the midpoint of that interval rather than a point we measured.
Measuring it properly would need synchronised clocks at both ends, which UDP
does not supply.

Scoring, once the answer is judged:

```
correct   -> 100 + bonus,  bonus = round(50 * max(0, 1 - adj_ms/1000 / limit_s))
incorrect -> 0
no answer -> 0
```

So a correct answer is worth between 100 and 150 points, and the speed part of
it is computed from the adjusted time.

## 6. The cases that are easy to forget

| Situation | What the receiver does |
|-----------|------------------------|
| `JOIN` from a peer that has already joined, same name | Treated as a re-send, not a new player. No state is reset, no second row appears. The host replies `WELCOME`, `SCORE`, and the open `QUESTION` if that player has not answered it. |
| `JOIN` with a name another **online** peer holds | `ERROR|1`, and the host immediately sends the incumbent peer a `SCORE` message as a probe. The new peer stays connected and **must retry** `JOIN` every 2 s for up to 40 s. See the note below: this is the path a killed-and-restarted client actually takes. |
| `JOIN` with a name an **offline** player holds | Accepted. The player is reattached to the new peer id and keeps their score. This is the disconnect-and-rejoin case the project requires. |
| `JOIN` with a version other than `1` | `BYE|6`, then the host closes the peer. |
| Any message other than `JOIN` from a peer that has not joined | `ERROR|7`. |
| `ANSWER` with a `qid` that is not the open question | `ERROR|2`. This covers an answer that arrives after the question closed, and one that arrives after the player has left and rejoined. |
| A second `ANSWER` for a question this player already answered | `ERROR|3`. The first answer stands; a player cannot improve their time by answering twice. |
| `ANSWER` with a `choice` outside `A`–`D`, or a malformed `qid` | `ERROR|4`. |
| Unknown message type, or too few fields | `ERROR|5`. The connection stays up. |
| A message from a peer the transport has reported closed | Impossible to act on and ignored: the host drops its peer-to-name mapping the moment `peers()` stops listing the peer. |
| `SCORE` would exceed 1024 bytes | The host sends the top 12 rows by score only. The `n` field says how many rows are present, so the client never waits for rows that are not coming. |
| A name with characters outside `[A-Za-z0-9_-]`, or over 16 characters | The client refuses to send it; a host that receives one anyway answers `ERROR|4`. |
| `PING` with a token over 8 characters | `ERROR|4`. |

### Why `ERROR|1` is retried rather than fatal

A client that is killed does not send `LEAVE`, and the host does not find out
straight away: the transport reports a peer closed only after that peer fails
to acknowledge something, and between questions the host sends it nothing to
acknowledge. So a client restarted a second after being killed finds its own
previous connection still holding its name.

The host cannot tell that case apart from a second person choosing a name
already in use — both look like a `JOIN` for a name that is online — so it
asks the incumbent instead of guessing. `ERROR|1` goes to the newcomer, and a
`SCORE` message goes to the incumbent. If the incumbent is alive it
acknowledges it and keeps the name. If it is gone, that message is what starts
its retransmission timer; the transport reports it closed about five seconds
later, the host marks the player offline, and the newcomer's next retry is
accepted as a rejoin with the score intact.

That is why `ERROR|1` is a "try again", not a refusal. A client gives up after
40 seconds, by which time the name really is held by somebody else.

Error codes:

| Code | Meaning |
|------|---------|
| 1 | name already in use by an online player |
| 2 | no such open question |
| 3 | already answered this question |
| 4 | a field is out of range or malformed |
| 5 | unknown message type or wrong field count |
| 6 | protocol version mismatch |
| 7 | join first |

