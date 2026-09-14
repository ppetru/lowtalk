# Lowtalk

Small, serverless terminal chat for trusted friends on Tailscale. Python 3.10+
with standard-library `curses`, on Linux or macOS. No pip packages, accounts,
chat server, database, or persisted chat logs.

## Run

Keep `lowtalk.py` and the four `chat_*.py` modules together. In your working
directory, create `friends_tailscale_ips.txt` listing **the other participants**:

```text
# Tailscale IPv4 address or DNS name; optional destination port
100.101.102.103
friend-machine.your-tailnet.ts.net 8888
```

Then run:

```sh
python3 /path/to/lowtalk.py guest
# Or listen locally on a different port:
python3 /path/to/lowtalk.py guest 8888
```

The local listening port defaults to **7777**. Each peer's destination port also
independently defaults to **7777**; the CLI port does not change destinations.
Everyone maintains their own file listing everyone else, with the port on which
each friend actually listens. Blank lines and `#` comments are supported.

The peer file is loaded from the **current working directory**, not the script's
directory. Names resolve to IPv4 addresses once at startup; restart after file
edits or DNS changes. The friends file is private local configuration and is
excluded from Git by `.gitignore`.

Entries must not resolve to overlapping addresses, since
inbound membership is identified by source IP. IPv6-only peers are not supported.
Configuration errors and an occupied listening port stop startup.

Tailscale must already be running and its access rules (and host firewall) must
permit the chosen **TCP** ports between participants. This is not wire-compatible
with the original UDP shell prototype.

## Interface

- Compact friends/status strip at the top, normally just one row. `/who` shows
  addresses, ports, and the last connection error in more detail.
- Message list with local `HH:MM` timestamps, nicknames, and incoming source IPs.
  Times reflect local receipt/send attempts, not the sender's clock.
- Separate, horizontally scrolling input line. Incoming messages leave the draft
  and cursor untouched. Enter sends; whitespace-only input does nothing.
- Page Up / Page Down browse up to **1,000 in-memory entries**. While browsing,
  incoming messages increment an unread count without jumping to the bottom.
  Page Down returns to the live view. Oldest entries are eventually evicted.
- `/quit` or Ctrl-C exits. Use `//text` to send a literal leading `/text`.

Editing is a small readline-style subset, not GNU readline:

| Keys | Action |
| --- | --- |
| Left / Right, Ctrl-B / Ctrl-F | Move one character |
| Home / End, Ctrl-A / Ctrl-E | Beginning / end |
| Backspace / Delete, Ctrl-D | Delete before / at cursor |
| Ctrl-W | Cut preceding whitespace-delimited word |
| Ctrl-U / Ctrl-K | Cut to beginning / end |
| Ctrl-Y | Paste the last cut (one slot, no kill ring) |
| Ctrl-L | Repaint |

Input is single-line, with no sent-message recall or multiline paste mode.
Pasted newlines send messages, just like Enter. Character widths use Unicode's
combining and East Asian width properties; complex emoji/grapheme editing may
not match every terminal. Very small terminals show a resize notice while
networking continues.

## Connection and security behavior

- Best-effort `tailscale ip -4` discovery, with a three-second timeout. If discovery
  fails, or the discovered address cannot be bound, the app visibly warns and
  listens on all IPv4 interfaces. A bind conflict is never silently bypassed.
- **Every inbound connection is checked against the resolved peer-address
  allowlist before any chat data is sent.** This identifies machines, not users
  or processes. Nicknames are self-chosen, not authenticated identities.
- There is no application encryption. Use Tailscale addresses/names: explicitly
  configuring a LAN/public address can send unencrypted traffic outside Tailscale.
  Listening broadly exposes the TCP port even though unlisted clients are rejected.
- One TCP session per peer pair, with automatic reconnection and exponential
  backoff (jittered, capped at 30 seconds). Both parties may attempt to connect;
  the lexicographically smaller `(IPv4 string, listening port)` initiates the
  surviving session. Both parties must permit inbound connections so this
  deterministic rule can work regardless of which endpoint sorts first.
- Online means a valid chat handshake was received. Heartbeats run every ten
  seconds; no received data for thirty seconds closes a session. TCP closure is
  detected sooner. The strip updates immediately; offline notices are delayed
  two seconds to suppress brief leave/join noise. This is not perfect instantaneous
  failure detection, and it is not Tailscale device presence.
- Only currently online peers receive send attempts. There is **no offline queue,
  reconnect replay, or application delivery acknowledgment**. TCP orders bytes
  reliably within a live session, but cannot prove the remote app displayed them.
  Your own message appears locally even if nobody is online; warnings identify
  unavailable recipients and known failures. Data already handed to the OS can
  still be lost when a connection fails, without a conclusive delivery result.
- Messages are limited to 4,000 characters; nicknames to 32. Terminal control and
  formatting characters are rejected on the wire and filtered from local display.
  Frames and outgoing buffers are bounded; a slow peer cannot grow memory forever.

The app never writes chat contents or drafts to disk. This does not prevent
terminal recording, screenshots, OS swap/core dumps, or a peer saving messages.
There is no independent security audit. Linux loopback/PTY tests are included;
actual macOS/Tailscale interoperability still needs verification on those machines.

## Maintenance and tests

No installation or development dependencies are needed:

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q lowtalk.py chat_config.py chat_protocol.py chat_network.py chat_ui.py tests
```

Tests use local loopback sockets and a pseudo-terminal, not your live Tailscale
peers. They cover configuration, protocol framing/validation, reconnects, duplicate
connection arbitration, allowlist rejection, backpressure, heartbeat expiry,
editing, and actual curses input during message receipt and terminal resize.

Code map:

- `lowtalk.py`: arguments, startup/bind policy, terminal lifecycle, cleanup.
- `chat_config.py`: peer-file parsing, DNS resolution, optional CLI discovery.
- `chat_protocol.py`: bounded JSON framing and input validation.
- `chat_network.py`: selector-driven connection state and events.
- `chat_ui.py`: editor, bounded scrollback, compact curses layout.

One thread owns everything. Each UI iteration polls sockets and input with bounded
work; there are no threads racing the terminal. DNS and CLI discovery happen only
before curses starts. Connection state is explicit in `Peer` and `Connection`.
Timing uses monotonic time; wall-clock time is only for displayed timestamps.

### Wire protocol, version 1

UTF-8 JSON objects, one per newline, at most 16,384 bytes excluding the newline.
The first frame in each direction must be:

```json
{"type":"hello","version":1,"nick":"guest","port":7777}
```

The advertised listening port must match the recipient's peer-file entry.
Subsequent frames are `{"type":"message","text":"hello"}`, `{"type":"ping"}`,
or `{"type":"pong"}`. A ping gets a pong; a pong gets no reply. Bad framing,
invalid fields, an unexpected hello, or a handshake exceeding five seconds closes
the connection. Extra object fields are ignored. Outgoing buffers are capped at
128 KiB per peer; overflow disconnects that peer and reports possible loss.
