#!/usr/bin/env python3
"""
Twitch chat, as an observation channel for the agent.

Anonymous read-only: Twitch lets any client read a public channel by logging in
as `justinfan<digits>` with no password, so this needs no account, no OAuth
token and no credentials on the box. It cannot post, and it is never asked to.

What the agent gets is message TEXT only -- no usernames, no user ids, no
badges. Partly because that is what was asked for, partly because it keeps
personal data out of the prompt and out of the run's logs.

Treat everything this returns as untrusted. It is text typed by strangers, fed
into a model that is choosing a move, so the only thing standing between chat
and the game is the action grammar. That is enough here: the model's output is
constrained to the verb set, so the worst chat can do is argue for a legal move
that happens to be a bad one -- which is the entertainment. It must never be
promoted into instructions, and nothing downstream may execute it.
"""

import collections
import random
import re
import socket
import ssl
import threading
import time

HOST, PORT = "irc.chat.twitch.tv", 6697
WINDOW = 20 * 60  # how far back "recent" reaches, in seconds
MAX_LEN = 160  # per message, so one paste cannot eat the prompt
KEEP = 400

# `:nick!nick@nick.tmi.twitch.tv PRIVMSG #channel :the message`
LINE = re.compile(r"^:[^ ]+ PRIVMSG #\S+ :(.*)$")
# Control characters, zero-width joiners and the like: strip them rather than
# letting them through into a prompt where they are invisible.
JUNK = re.compile(r"[\x00-\x1f\x7f​-‏‪-‮]")


class Chat:
    """Background reader. Never raises into the caller; a dead socket just
    means an empty window, and the agent goes on playing."""

    def __init__(self, channel, window=WINDOW):
        self.channel = channel.lstrip("#").lower()
        self.window = window
        self.msgs = collections.deque(maxlen=KEEP)
        self.connected = False
        self.errors = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self._session()
            except Exception:
                self.errors += 1
            self.connected = False
            # Reconnect, but not in a tight loop if Twitch is refusing us.
            self._stop.wait(5.0)

    def _session(self):
        ctx = ssl.create_default_context()
        with socket.create_connection((HOST, PORT), timeout=20) as raw:
            with ctx.wrap_socket(raw, server_hostname=HOST) as sock:
                nick = "justinfan%d" % random.randint(10000, 99999)
                sock.sendall(
                    ("NICK %s\r\nJOIN #%s\r\n" % (nick, self.channel)).encode()
                )
                sock.settimeout(30)
                self.connected = True
                buf = b""
                while not self._stop.is_set():
                    try:
                        chunk = sock.recv(8192)
                    except socket.timeout:
                        sock.sendall(b"PING :keepalive\r\n")
                        continue
                    if not chunk:
                        return
                    buf += chunk
                    while b"\r\n" in buf:
                        line, buf = buf.split(b"\r\n", 1)
                        self._line(sock, line.decode("utf-8", "replace"))

    def _line(self, sock, line):
        if line.startswith("PING"):
            sock.sendall(("PONG" + line[4:] + "\r\n").encode())
            return
        m = LINE.match(line)
        if not m:
            return
        text = JUNK.sub("", m.group(1)).strip()[:MAX_LEN]
        if text:
            with self._lock:
                self.msgs.append((time.time(), text))

    def recent(self, limit=25, window=None):
        """The last `limit` messages inside the window, oldest first.

        Consecutive duplicates collapse -- a chat spamming the same emote forty
        times should cost one line of prompt, not forty.
        """
        cutoff = time.time() - (window or self.window)
        with self._lock:
            msgs = [t for ts, t in self.msgs if ts >= cutoff]
        out = []
        for t in msgs:
            if not out or out[-1] != t:
                out.append(t)
        return out[-limit:]

    def status(self):
        return {
            "channel": self.channel,
            "connected": self.connected,
            "held": len(self.msgs),
            "errors": self.errors,
        }


if __name__ == "__main__":
    import sys

    c = Chat(sys.argv[1] if len(sys.argv) > 1 else "twitch")
    while True:
        time.sleep(5)
        print(c.status(), c.recent(5))
