# Lowtalk

Small, serverless terminal chat for trusted friends on Tailscale. Runs on Linux
and macOS with Python 3.9+ and `curses`; no pip packages or chat server needed.
No saved chat logs.

## Run

Keep `lowtalk.py` and the four `chat_*.py` modules together. Create
`friends_tailscale_ips.txt` in the **directory you run from**, listing everyone
else's Tailscale IPv4 address or DNS name:

```text
# Optional second column: the port that friend listens on
100.101.102.103
friend-machine.your-tailnet.ts.net 8888
```

```sh
python3 /path/to/lowtalk.py your-nickname
# Optional local listening port:
python3 /path/to/lowtalk.py your-nickname 8888
```

Ports default to **7777**. Changing your local port does not change your friends'
destination ports. Everyone needs their own file listing everyone else; entries
must resolve to distinct IPv4 addresses. Restart after editing the file or changing
DNS. The friends file is excluded from Git.

Tailscale must be running, and Tailscale access rules and host firewalls must allow
inbound **TCP** on each participant's listening port.

All participants must use **protocol v2**. It cannot connect to the old v1
implementation. Update all five Python files together and restart.
Version mismatches appear in `/who`; failed handshakes do not repeatedly add
warnings to scrollback.

## Using it

- Enter sends to currently connected friends. If nothing can be queued, the draft
  and cursor stay intact for an explicit Enter retry; reconnecting never sends it
  automatically. A partial send clears the draft and lists unavailable recipients.
  Incoming messages leave your draft and cursor alone. Nicknames can contain up
  to 32 characters.
- The top strip shows connections and retry countdowns; `/who` shows addresses
  and connection errors.
  Connections recover automatically, though detecting a lost peer can take about
  30 seconds. Routine connects, disconnects, and timeouts do not add scrollback
  entries. Pending-data loss and unexpected protocol errors still produce warnings.
  `/who` shows the current connection error; reconnecting clears it.
- A friend's new running instance appends a timestamped history separator:
  `--- Bob has a new Lowtalk session; previous scrollback was cleared. ---`
  Sleep/wake and reconnects to the same instance add no separator. First contact
  is unknown, not evidence of a restart. The separator appears when the new
  connection commits, not at the actual restart time. It means that friend's
  previous in-memory history was lost, not that every entry above the separator
  is absent from their current buffer: another friend may have sent messages to
  both of you before you reconnected. Entries below it are not delivery or read
  receipts either.
- Page Up / Page Down browse the last **1,000 in-memory entries**, with an unread
  count while browsing. Page down to the bottom to return to live messages.
- `/quit` or Ctrl-C exits. `//text` sends a literal leading `/text`.
- Messages are limited to **4,000 characters**. Excess input is discarded with a
  warning. **Pasted newlines send messages**, just like Enter.

| Keys | Action |
| --- | --- |
| Left / Right, Ctrl-B / Ctrl-F | Move cursor |
| Home / End, Ctrl-A / Ctrl-E | Beginning / end |
| Backspace / Delete, Ctrl-D | Delete before / at cursor |
| Ctrl-W | Cut preceding word |
| Ctrl-U / Ctrl-K | Cut to beginning / end |
| Ctrl-Y | Paste the last cut |
| Ctrl-L | Repaint |

There is no sent-message recall or offline queue. A local `(you)` echo means the
message was queued for at least one live session, not delivered or read. Rejected
messages stay only in your draft; interrupted messages are not replayed.

## Privacy

Use Tailscale addresses: Lowtalk relies on Tailscale for encryption, not its own
cryptography. If it cannot discover or bind the Tailscale address, it warns and
listens on **all IPv4 interfaces**, still rejecting unlisted source IPs.

The peer list trusts **machines, not individual users or processes**. Nicknames
are self-chosen, not verified identities. Lowtalk does not save messages or drafts,
but a participant or terminal recorder can.

## Protocol and connection transitions

The wire format is newline-delimited UTF-8 JSON, with a maximum of 16,384 bytes
per frame excluding the newline. Hello is:

```json
{"type":"hello","version":2,"nick":"bob","port":7777,"instance":"9f34236dac274eefb882213f6518a47d2"}
```

`instance` is a random 128-bit identifier, represented by 32 lowercase hexadecimal
characters, generated once per running instance and kept only in memory. It
identifies the scrollback lifetime and elects the smaller identifier as connection
coordinator; it is not authentication. Extra fields are ignored; unsupported
versions, invalid fields, and out-of-order frames are explicit protocol errors.

Each peer retains at most one candidate socket in each direction. Either
direction can establish the connection, independent of retry backoff. The
coordinator selects the first candidate with a valid hello. Only the selected
socket carries `select`, `accept`, and `ready`, each as `{"type":"..."}`.

| Local phase | Event | Action and next phase |
| --- | --- | --- |
| CONNECTING | TCP connect succeeds | HELLO; local hello is already queued |
| HELLO | Valid remote hello; local instance is smaller | Retire competing candidate; send select; SELECTED |
| HELLO | Valid remote hello; remote instance is smaller | Wait for selection; CANDIDATE |
| CANDIDATE | Receive select from coordinator | Retire competing candidate; send accept; ACCEPTED |
| SELECTED | Receive accept | Queue ready before application output; commit READY |
| ACCEPTED | Receive ready | Commit READY |
| READY | Receive message, ping, or pong | Deliver message, reply to ping, or consume pong |

Selection is irrevocable while the socket exists: later candidates cannot
displace it. Candidate failure must not clear the selected connection. A closed
socket is retired, with reconnect backoff when no candidates remain. Handshakes
have an absolute five-second deadline; receiving bytes does not extend it.
Readiness is derived from the phase, not independently mutable state.

On commitment, compare the remote instance with the last committed instance for
that configured friend. First contact or the same identifier is quiet; a changed
identifier appends one boundary before incoming chat from the new session.
Failed or rejected candidates cannot change this remembered identity.

## Development checks

Runtime remains Python 3.9+ with the standard library and curses. Pyright and
Ruff are development tools only; no installation step is added for users.
`pyproject.toml` enables strict Pyright for all five application modules, targeting
Python 3.9, and Ruff's core error/unused-code checks.

```sh
python3 -m unittest discover -s tests -v
pyright
ruff check .
# On Nix, provide the development tools without installing runtime dependencies:
nix shell nixpkgs#python3 nixpkgs#pyright nixpkgs#ruff
```

Validated frames are a discriminated standard-library `TypedDict` union.
Exhaustive frame and phase handling uses a Python-3.9-compatible `NoReturn`
helper. Internal assertions check candidate ownership and commitment invariants;
remote-input validation uses exceptions and remains active with `python -O`.
Tests exercise real loopback TCP and a curses subprocess in a PTY, without
Tailscale or the local friends file. No TLC/Java tooling is required.
