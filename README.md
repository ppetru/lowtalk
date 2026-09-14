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

## Using it

- Enter sends to currently connected friends. Incoming messages leave your draft
  and cursor alone. Nicknames can contain up to 32 characters.
- The top strip shows connections; `/who` shows addresses and connection errors.
  Connections recover automatically, though detecting a lost peer can take about
  30 seconds.
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

There is no sent-message recall or offline queue. Your own messages appear locally
even if nobody is connected; warnings identify unavailable recipients. A successful
send is not proof that someone saw the message, and interrupted messages are not
replayed.

## Privacy

Use Tailscale addresses: Lowtalk relies on Tailscale for encryption, not its own
cryptography. If it cannot discover or bind the Tailscale address, it warns and
listens on **all IPv4 interfaces**, still rejecting unlisted source IPs.

The peer list trusts **machines, not individual users or processes**. Nicknames
are self-chosen, not verified identities. Lowtalk does not save messages or drafts,
but a participant or terminal recorder can.
