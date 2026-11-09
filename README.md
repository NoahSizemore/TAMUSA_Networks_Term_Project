<div align="center">

# TAMUSA Computer Networks Term Project

![Project](https://img.shields.io/badge/Project-TAMUSA-red)
![Python](https://img.shields.io/badge/Python-3.6%2B-blue)
![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)

**Reliable Transport and a Live Quiz**

[Noah Sizemore](https://github.com/NoahSizemore) · transport
&nbsp;•&nbsp;
[William Hale](https://github.com/williamhale210-ai) · application

</div>

## 📝 Project Scope

Assigned by the TAMUSA Computer Networks professor (CSCI 4306). For the full
scope see `documentation/Team Project - Reliable Transport and an Application.pdf`.

Two halves, built at the same time against a fixed interface:

| Half | Owner | Files |
|------|-------|-------|
| Transport | Noah | `src/transport.py`, `src/test_conformance.py`, `src/bench_burst.py` |
| Application | William | `src/app/`, `src/stub/transport.py`, `documentation/protocol.md` |

## 📖 About The Project

A reliable message transport over UDP, and a terminal quiz that runs on top of
it without knowing anything about how it works.

**The transport** (`src/transport.py`) is one UDP socket serving many peers. It
has a 16-byte header with a one's-complement checksum, per-message sequence
numbers, cumulative acknowledgments, a measured retransmission timeout with
Karn's algorithm and exponential backoff, duplicate detection, and per-peer
state demultiplexed from the single socket. It is single-threaded: every timer
is serviced inside `send()` and `recv()`, so a transport whose application
stops calling it stops retransmitting — which is the contract, not a
limitation. It defaults to stop-and-wait and does Go-Back-N over a window when
asked.

**The quiz** (`src/app/`) is a host with a question bank and any number of
terminal clients. The host scores answers on correctness and on speed, and the
hard part is the speed: a client on a cluster across the country is tens of
milliseconds further away, and that shows up in its arrival time whether the
player was quick or not. The scoreboard shows the raw arrival order and the
order after subtracting each client's measured path, side by side.

Both programs show a live status line of `rtt_ms`, `resent` and `dropped` per
peer, read from `stats()`, so the retransmissions are visible while the demo
runs at `TRANSPORT_LOSS=0.1`.

<div align="center">
  <img src="images/temp.png" width="1200">

  **TODO: screenshot of the host at TRANSPORT_LOSS=0.1 with resends climbing**
    <br>
</div>

## How to use the code

Python 3 and nothing else — no packages to install, no virtual environment.

### Simulating a real network

Two processes on one machine lose nothing and answer in microseconds, so both
programs read two environment variables:

| Variable | Meaning |
|----------|---------|
| `TRANSPORT_LOSS` | 0 to 1. That fraction of **every** outgoing datagram is discarded, acknowledgments included. |
| `TRANSPORT_DELAY` | Milliseconds of one-way delay. Set at both ends, 17.5 gives a 35 ms path. |
| `TRANSPORT_WINDOW` | Messages outstanding at once. Default 1 (stop-and-wait); 8 enables Go-Back-N. |

### Running the quiz

Which `transport.py` gets imported is decided entirely by `PYTHONPATH`, and
nothing in the application changes between the two. Run these from the
repository root, each in its own SSH session:

```sh
# host, over the real transport
PYTHONPATH=src python3 src/app/quiz_host.py --port 9000

# three clients at three distances
TRANSPORT_DELAY=2  PYTHONPATH=src python3 src/app/quiz_client.py --name wisconsin
TRANSPORT_DELAY=25 PYTHONPATH=src python3 src/app/quiz_client.py --name clemson
TRANSPORT_DELAY=45 PYTHONPATH=src python3 src/app/quiz_client.py --name utah

# the same thing over the stub transport, for the fallback demo
PYTHONPATH=src/stub python3 src/app/quiz_host.py --port 9000

# with a tenth of the datagrams discarded
TRANSPORT_LOSS=0.1 PYTHONPATH=src python3 src/app/quiz_host.py --port 9000
```

> The application lives in `src/app/`, a directory with no `transport.py` in
> it, and that is not an accident. Python puts a script's own directory ahead
> of `PYTHONPATH`, so an application sitting next to the real transport can
> **never** be pointed at the stub -> `PYTHONPATH=stub python3 quiz_host.py`
> silently keeps using the real one. The host prints which file it loaded on
> startup so this is never in doubt.

Keys — host: `n` next question, `e` close it early, `r` repaint, `q` quit.
Client: `A` `B` `C` `D` or `1`–`4` to answer, `r` repaint, `q` quit.
Add `--auto` to the host and `--autoplay` to the clients to let it play itself,
which is how the recording is made; drive it by hand for the live demo.

### Running the tests

```sh
# the conformance checklist: 8 cases at TRANSPORT_LOSS = 0, 0.1 and 0.2
python3 src/test_conformance.py

# one setting, or a subset of cases, while working on it
python3 src/test_conformance.py --loss 0.1 --only 4 7

# case 8 needs a VM on another cluster: on the far VM, then on this one
python3 src/test_conformance.py --serve 9100
python3 src/test_conformance.py --peer dev.NAME.networksclass-pg0.CLUSTER.cloudlab.us:9100 --ping-ms 34.8

# the whole checklist, with the output saved for the report's appendix
scripts/run_checklist.sh

# the quiz end to end, as four real processes
python3 src/test_quiz.py                 # over the real transport
python3 src/test_quiz.py --loss 0.1      # with losses
python3 src/test_quiz.py --stub          # over the stub

# the 100-message burst: stop-and-wait against a window of 8
python3 src/bench_burst.py --count 100 --windows 1 8 --delays 0 17.5
```

### Layout

```
src/
  transport.py          the real transport
  test_conformance.py   the eight conformance cases, and the case-8 responder
  bench_burst.py        the 100-message measurement
  test_quiz.py          the application, end to end, as real processes
  stub/transport.py     the stub: seven methods, none of the machinery
  app/
    quiz_host.py        the quiz host
    quiz_client.py      the quiz client
    terminal.py         raw keyboard and a diffing screen; no networking
documentation/
  protocol.md           the application protocol, version 1
  report/report.md      the report
scripts/
  run_checklist.sh      the checklist, with its output kept for the appendix
  record_demo.sh        the asciinema recording
```

## AI Usage Statement

We thoughtfully disclose our use of LLMs. LLMs were used for (i) assisting in improving, debugging, and cleaning author-written code, with all changes verified before implementation; (ii) improving comments in the code to ensure proper readability; (iii) ensuring functionality across multiple machines (e.g., macOS, Linux, Windows); (iv) adjusting grammar and spelling wherever documentation is present (i.e., Report, README.md, protocol.md, etc.); and (v) assisting in making the programs better than the intial scope (i.e., LLMs suggest an imporvement, author inquires on solution, athour then implements solution after verification). LLMs were not used for creating the core of the project, the idea of the project (given to use by the professor), how testing was done, how documentation and reporting was made, or other less-major tasks. No LLM output was implemented without verification by the authors.
