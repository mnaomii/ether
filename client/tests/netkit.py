"""Shared helpers for the networking test scripts (net_server.py, net_client.py, net_e2e.py).

- An independent wire codec. It deliberately does NOT use ether_core.proto, so a codec
  bug on one side can't hide, or fake, a bug on the other. net_client.py checks it against
  shared/vectors.txt.
- Blocking socket helpers in which every wait has a timeout.
- A tiny test runner: one PASS / FAIL / SKIP line per test, then a summary (like test_vectors.c).

Not a pytest module: `python -m pytest tests` only runs the codec tests.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import socket
import struct
import sys
import threading
import time
import traceback
from collections import namedtuple
from pathlib import Path


class cfg:                       # filled in by main() from the command line / environment
    host = os.environ.get("ETHER_HOST", "127.0.0.1")
    t = float(os.environ.get("ETHER_TIMEOUT", "2"))     # seconds to wait for an expected reply
    slow = os.environ.get("ETHER_SLOW") == "1"


TCP_PORT, UDP_PORT = 5050, 5051
MAGIC, VERSION = 0x4554, 1
MAX_FRAME, MAX_NICK, MAX_PASS, MAX_TEXT, MAX_FILE = 64 * 1024, 31, 64, 4000, 50 * 1024 * 1024
ROOMS = {1: "general", 2: "homework", 3: "games"}

# TCP types
LOGIN, WELCOME, JOIN, LEAVE, SAY, MSG, HISTORY, USERS, USER_EVT = range(0x01, 0x0A)
FILE_UP_REQ, FILE_UP_OK, FILE_EVT, FILE_DOWN_REQ, FILE_DOWN_OK, DATA_HELLO = range(0x10, 0x16)
ERROR, BYE = 0x7E, 0x7F
TNAME = {v: k for k, v in dict(
    LOGIN=LOGIN, WELCOME=WELCOME, JOIN=JOIN, LEAVE=LEAVE, SAY=SAY, MSG=MSG, HISTORY=HISTORY,
    USERS=USERS, USER_EVT=USER_EVT, FILE_UP_REQ=FILE_UP_REQ, FILE_UP_OK=FILE_UP_OK,
    FILE_EVT=FILE_EVT, FILE_DOWN_REQ=FILE_DOWN_REQ, FILE_DOWN_OK=FILE_DOWN_OK,
    DATA_HELLO=DATA_HELLO, ERROR=ERROR, BYE=BYE).items()}

# UDP types
U_DISCOVER, U_DISCOVER_REPLY, U_UDP_HELLO, U_TYPING, U_TYPING_EVT, U_PING, U_PONG = range(1, 8)
UNAME = {1: "DISCOVER", 2: "DISCOVER_REPLY", 3: "UDP_HELLO", 4: "TYPING", 5: "TYPING_EVT",
         6: "PING", 7: "PONG"}


def tname(t) -> str:
    if isinstance(t, tuple):
        return "/".join(tname(x) for x in t)
    return TNAME.get(t, f"type 0x{t:02X}")


# ---- encoding ----

def u8(v): return struct.pack("!B", v)
def u16(v): return struct.pack("!H", v)
def u32(v): return struct.pack("!I", v)
def u64(v): return struct.pack("!Q", v)


def s(text) -> bytes:
    """[u16 byte_len][UTF-8 bytes]. Pass bytes to send something that isn't valid UTF-8."""
    b = text if isinstance(text, bytes) else text.encode()
    return u16(len(b)) + b


def frame(t: int, payload: bytes = b"") -> bytes:
    return u32(1 + len(payload)) + u8(t) + payload


def dgram(t: int, sid: int = 0, payload: bytes = b"") -> bytes:
    return u16(MAGIC) + u8(VERSION) + u8(t) + u32(sid) + payload


# client -> server
def login(nick, pw): return frame(LOGIN, s(nick) + s(pw))
def join(room): return frame(JOIN, u16(room))
def leave(room): return frame(LEAVE, u16(room))
def say(room, text): return frame(SAY, u16(room) + s(text))
def file_up_req(room, name, size): return frame(FILE_UP_REQ, u16(room) + s(name) + u64(size))
def file_down_req(fid): return frame(FILE_DOWN_REQ, u32(fid))
def data_hello(tid, token): return frame(DATA_HELLO, u32(tid) + u32(token))
def bye(): return frame(BYE)
def discover(): return dgram(U_DISCOVER)
def udp_hello(sid): return dgram(U_UDP_HELLO, sid)
def typing(sid, room): return dgram(U_TYPING, sid, u16(room))
def ping(sid, t): return dgram(U_PING, sid, u64(t))


# server -> client (used by the fake server in net_client.py)
def msg_body(room, ms, frm, text): return u16(room) + u64(ms) + s(frm) + s(text)
def welcome(sid, rooms): return frame(WELCOME, u32(sid) + u16(len(rooms)) + b"".join(u16(r) + s(n) for r, n in rooms))
def msg(room, ms, frm, text): return frame(MSG, msg_body(room, ms, frm, text))
def users(room, nicks): return frame(USERS, u16(room) + u16(len(nicks)) + b"".join(s(n) for n in nicks))
def user_evt(room, joined, nick): return frame(USER_EVT, u16(room) + u8(joined) + s(nick))
def file_evt(room, fid, name, size, frm): return frame(FILE_EVT, u16(room) + u32(fid) + s(name) + u64(size) + s(frm))
def file_up_ok(tid, token): return frame(FILE_UP_OK, u32(tid) + u32(token))
def file_down_ok(tid, token, size): return frame(FILE_DOWN_OK, u32(tid) + u32(token) + u64(size))
def error(code, text): return frame(ERROR, u16(code) + s(text))
def discover_reply(name, port, n): return dgram(U_DISCOVER_REPLY, 0, s(name) + u16(port) + u16(n))
def typing_evt(sid, room, nick): return dgram(U_TYPING_EVT, sid, u16(room) + s(nick))
def pong(sid, t): return dgram(U_PONG, sid, u64(t))


def history(room, msgs):
    """Every HISTORY entry is the full MSG payload,
    room_id included: u16 room, u64 unix_ms, str from, str text."""
    return frame(HISTORY, u16(room) + u16(len(msgs)) + b"".join(msg_body(*m) for m in msgs))


# ---- decoding ----

Welcome = namedtuple("Welcome", "sid rooms")
Msg = namedtuple("Msg", "room ms frm text")
History = namedtuple("History", "room msgs note")       # note: set if the entries lack room_id
Users = namedtuple("Users", "room nicks")
UserEvt = namedtuple("UserEvt", "room joined nick")
FileUpOk = namedtuple("FileUpOk", "tid token")
FileEvt = namedtuple("FileEvt", "room fid name size frm")
FileDownOk = namedtuple("FileDownOk", "tid token size")
Error = namedtuple("Error", "code text")
Bye = namedtuple("Bye", "")
Raw = namedtuple("Raw", "type payload")
# client -> server, as seen by the fake server
Login = namedtuple("Login", "nick pw")
Join = namedtuple("Join", "room")
Leave = namedtuple("Leave", "room")
Say = namedtuple("Say", "room text")
FileUpReq = namedtuple("FileUpReq", "room name size")
FileDownReq = namedtuple("FileDownReq", "fid")
DataHello = namedtuple("DataHello", "tid token")
# UDP
Dgram = namedtuple("Dgram", "type sid body")
DiscoverReply = namedtuple("DiscoverReply", "name port users")
TypingEvt = namedtuple("TypingEvt", "room nick")
Typing = namedtuple("Typing", "room")
Ping = namedtuple("Ping", "t")
Pong = namedtuple("Pong", "t")


class Reader:
    """Cursor over a payload. Raises ValueError (with the reason) instead of reading past the end."""

    def __init__(self, b: bytes):
        self.b, self.o = bytes(b), 0

    def take(self, n: int) -> bytes:
        if self.o + n > len(self.b):
            raise ValueError(f"truncated: needs {n} more byte(s) at offset {self.o}, "
                             f"only {len(self.b) - self.o} left")
        self.o += n
        return self.b[self.o - n:self.o]

    def u8(self): return self.take(1)[0]
    def u16(self): return struct.unpack("!H", self.take(2))[0]
    def u32(self): return struct.unpack("!I", self.take(4))[0]
    def u64(self): return struct.unpack("!Q", self.take(8))[0]

    def str(self):
        raw = self.take(self.u16())
        try:
            return raw.decode()
        except UnicodeDecodeError as e:
            raise ValueError(f"string is not valid UTF-8: {raw!r}") from e

    def end(self):
        if self.o != len(self.b):
            raise ValueError(f"{len(self.b) - self.o} unexpected byte(s) after the last field "
                             "(length field too big, or an extra field?)")


HISTORY_FORMAT = ("These tests expect each HISTORY entry to be the full MSG payload "
                  "(u16 room_id, u64 unix_ms, str from, str text). If an implementation chose otherwise "
                  "on purpose, client and server must agree, and only the HISTORY tests disagree.")


def _history(payload: bytes) -> History:
    """Full-MSG-payload entries. If they only parse WITHOUT room_id, the
    History is still returned, with .note set: only the HISTORY tests fail on that."""
    r = Reader(payload)
    room, n = r.u16(), r.u16()
    try:
        msgs = [Msg(r.u16(), r.u64(), r.str(), r.str()) for _ in range(n)]
        r.end()
        return History(room, msgs, "")
    except ValueError as e:
        r2 = Reader(payload)
        r2.u16(), r2.u16()
        try:
            msgs = [Msg(room, r2.u64(), r2.str(), r2.str()) for _ in range(n)]
            r2.end()
        except ValueError:
            raise ValueError(f"malformed HISTORY ({e}). {HISTORY_FORMAT}") from None
        return History(room, msgs, "HISTORY entries are MSG bodies WITHOUT room_id. " + HISTORY_FORMAT)


def parse(t: int, payload: bytes):
    """Server -> client frame body as a namedtuple. ValueError (with the reason) if malformed."""
    if t == HISTORY:
        return _history(payload)
    r = Reader(payload)
    if t == WELCOME:
        sid, n = r.u32(), r.u16()
        m = Welcome(sid, [(r.u16(), r.str()) for _ in range(n)])
    elif t == MSG:
        m = Msg(r.u16(), r.u64(), r.str(), r.str())
    elif t == USERS:
        room, n = r.u16(), r.u16()
        m = Users(room, [r.str() for _ in range(n)])
    elif t == USER_EVT:
        m = UserEvt(r.u16(), r.u8(), r.str())
    elif t == FILE_UP_OK:
        m = FileUpOk(r.u32(), r.u32())
    elif t == FILE_EVT:
        m = FileEvt(r.u16(), r.u32(), r.str(), r.u64(), r.str())
    elif t == FILE_DOWN_OK:
        m = FileDownOk(r.u32(), r.u32(), r.u64())
    elif t == ERROR:
        m = Error(r.u16(), r.str())
    elif t == BYE:
        m = Bye()
    else:
        return Raw(t, payload)
    r.end()
    return m


def parse_c2s(t: int, payload: bytes):
    """Client -> server frame body as a namedtuple (the fake server's view)."""
    r = Reader(payload)
    if t == LOGIN:
        m = Login(r.str(), r.str())
    elif t in (JOIN, LEAVE):
        m = (Join if t == JOIN else Leave)(r.u16())
    elif t == SAY:
        m = Say(r.u16(), r.str())
    elif t == FILE_UP_REQ:
        m = FileUpReq(r.u16(), r.str(), r.u64())
    elif t == FILE_DOWN_REQ:
        m = FileDownReq(r.u32())
    elif t == DATA_HELLO:
        m = DataHello(r.u32(), r.u32())
    elif t == BYE:
        m = Bye()
    else:
        return Raw(t, payload)
    r.end()
    return m


def parse_dgram(b: bytes) -> Dgram:
    """Any Ether datagram -> Dgram(type, sid, body). ValueError if malformed."""
    r = Reader(b)
    magic, ver, t, sid = r.u16(), r.u8(), r.u8(), r.u32()
    if magic != MAGIC or ver != VERSION:
        raise ValueError(f"bad header: magic 0x{magic:04X} (want 0x4554), version {ver} (want 1)")
    if t == U_DISCOVER_REPLY:
        body = DiscoverReply(r.str(), r.u16(), r.u16())
    elif t == U_TYPING_EVT:
        body = TypingEvt(r.u16(), r.str())
    elif t in (U_PING, U_PONG):
        body = (Ping if t == U_PING else Pong)(r.u64())
    elif t == U_TYPING:
        body = Typing(r.u16())
    elif t in (U_DISCOVER, U_UDP_HELLO):
        body = None
    else:
        raise ValueError(f"unknown UDP type 0x{t:02X}")
    r.end()
    return Dgram(t, sid, body)


def short(x, n=100) -> str:
    text = repr(x)
    return text if len(text) <= n else text[:n - 3] + "..."


def describe(t: int, payload: bytes, c2s: bool = False) -> str:
    try:
        m = (parse_c2s if c2s else parse)(t, payload)
    except ValueError as e:
        return f"{tname(t)}(malformed: {e}; {payload[:24].hex(' ')})"
    return f"{tname(t)}{short(tuple(m), 110)}"


def load_vectors() -> dict[str, bytes]:
    path = Path(__file__).resolve().parents[2] / "shared" / "vectors.txt"
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or "|" not in line:
            continue
        name, hexbytes = line.split("|", 1)
        out[name.strip()] = bytes.fromhex(hexbytes)
    return out


# ---- blocking sockets (net_server.py) ----

_open: list = []                 # closed by the runner after every test


class Closed(Exception):
    """The peer closed the TCP connection (EOF or reset)."""


class Conn:
    """Blocking TCP connection to the server with a frame buffer. Every wait has a timeout."""

    def __init__(self, host=None, port=TCP_PORT, rcvbuf=0):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if rcvbuf:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        sock.settimeout(max(cfg.t, 2))
        try:
            sock.connect((host or cfg.host, port))
        except OSError as e:
            sock.close()
            raise AssertionError(f"can't connect to {host or cfg.host}:{port} ({e}). "
                                 "Did the server crash or stop accepting in an earlier test?") from None
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock, self.buf, self.held = sock, b"", []
        self.nick, self.pw, self.sid, self.label = "", "", 0, ""
        _open.append(self)

    @property
    def who(self) -> str:
        if self.label and self.nick:
            return f"{self.label} ({self.nick})"
        return self.nick or self.label or "connection"

    def send(self, data: bytes):
        self.sock.settimeout(max(cfg.t, 2))
        self.sock.sendall(data)

    def send_slowly(self, data: bytes, gap=0.003):
        """One byte per send(), so the server's recv() sees partial frames."""
        for i in range(len(data)):
            self.sock.sendall(data[i:i + 1])
            time.sleep(gap)

    def _recv(self, timeout: float) -> bool:
        self.sock.settimeout(max(timeout, 0.001))
        try:
            chunk = self.sock.recv(65536)
        except (socket.timeout, BlockingIOError):
            return False
        except OSError as e:     # reset / aborted
            raise Closed(f"reset: {e}") from None
        if not chunk:
            raise Closed("EOF")
        self.buf += chunk
        return True

    def frame(self, timeout=None):
        """Next frame as (type, payload), or None if no complete frame arrives in time. Raises Closed."""
        if self.held:
            return self.held.pop(0)
        deadline = time.monotonic() + (cfg.t if timeout is None else timeout)
        while True:
            if len(self.buf) >= 4:
                n = int.from_bytes(self.buf[:4], "big")
                if not 1 <= n <= MAX_FRAME:
                    raise AssertionError(
                        f"{self.who}: server sent a frame with length field {n} (must be 1..{MAX_FRAME}); "
                        f"bytes: {self.buf[:16].hex(' ')}. The stream is out of sync: length = 1 + payload "
                        "size, written with put_u32 (big-endian), and each frame queued exactly once")
                if len(self.buf) >= 4 + n:
                    t, p = self.buf[4], self.buf[5:4 + n]
                    self.buf = self.buf[4 + n:]
                    return t, p
            left = deadline - time.monotonic()
            if left <= 0 or not self._recv(left):
                if time.monotonic() >= deadline:
                    return None

    def _parse(self, t, p):
        try:
            return parse(t, p)
        except ValueError as e:
            raise AssertionError(f"{self.who}: malformed {tname(t)} from the server: {e}\n"
                                 f"payload: {p[:48].hex(' ')}") from None

    def expect(self, t, pred=None, timeout=None, why=""):
        """Next frame of type t (or any type in a tuple t) matching pred, parsed. Frames in between
        are kept for later expect() calls (other tests' clients can cause stray USER_EVTs)."""
        types = t if isinstance(t, tuple) else (t,)
        timeout = cfg.t if timeout is None else timeout
        deadline = time.monotonic() + timeout
        skipped = []
        try:
            while True:
                try:
                    f = self.frame(max(deadline - time.monotonic(), 0))
                except Closed as e:
                    raise AssertionError(f"{self.who}: expected {tname(t)} but the server closed the "
                                         f"connection ({e}).{_seen(skipped)}{_why(why)}") from None
                if f is None:
                    raise AssertionError(f"{self.who}: expected {tname(t)}"
                                         f"{' (matching this test)' if pred else ''} within {timeout:g} s, "
                                         f"got nothing.{_seen(skipped)}{_why(why)}")
                m = self._parse(*f)
                if f[0] in types and (pred is None or pred(m)):
                    return m
                skipped.append(f)
        finally:
            self.held[:0] = skipped

    def quiet(self, t, pred=None, wait=0.4, why=""):
        """Assert that no frame of type t matching pred arrives within `wait` seconds.
        Other frames are kept for later expect() calls."""
        types = t if isinstance(t, tuple) else (t,)
        deadline = time.monotonic() + wait
        kept, todo, self.held = [], self.held, []
        try:
            while True:
                if todo:
                    f = todo.pop(0)
                else:
                    left = deadline - time.monotonic()
                    try:
                        f = self.frame(left) if left > 0 else None
                    except Closed:
                        f = None
                    if f is None:
                        return
                m = self._parse(*f)
                if f[0] in types and (pred is None or pred(m)):
                    raise AssertionError(f"{self.who}: did not expect {describe(*f)}.{_why(why)}")
                kept.append(f)
        finally:
            self.held = kept + todo + self.held

    def wait_closed(self, timeout=None, why=""):
        """Read until the server closes the connection; returns the frames that came first."""
        timeout = cfg.t if timeout is None else timeout
        got = list(self.held)
        self.held = []
        data, how = self.read_all(total=timeout)
        got += split_frames(data)
        if how not in ("eof", "reset"):
            seen = "; received first: " + ", ".join(describe(*g) for g in got) if got else ""
            raise AssertionError(f"{self.who}: the server did not close the connection within "
                                 f"{timeout:g} s{seen}.{_why(why)}")
        return got

    def read_all(self, limit=None, idle=None, total=None):
        """Raw bytes until the server closes (data connections). Returns (data, how), how being
        'eof' / 'reset' / 'timeout' (nothing new for `idle` s, or `total` s overall) / 'too much'."""
        idle = cfg.t if idle is None else idle
        end = time.monotonic() + total if total is not None else None
        data = bytearray(self.buf)
        self.buf = b""
        while limit is None or len(data) <= limit:
            wait = idle if end is None else min(idle, end - time.monotonic())
            if wait <= 0:
                return bytes(data), "timeout"
            self.sock.settimeout(wait)
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, BlockingIOError):
                return bytes(data), "timeout"
            except OSError:
                return bytes(data), "reset"
            if not chunk:
                return bytes(data), "eof"
            data += chunk
        return bytes(data), "too much"

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def abort(self):
        """Close with RST instead of FIN (what a crashing client looks like)."""
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        except OSError:
            pass
        self.close()


def split_frames(data: bytes) -> list:
    """Best effort: the complete frames at the start of data, as (type, payload)."""
    out = []
    while len(data) >= 5:
        n = int.from_bytes(data[:4], "big")
        if not 1 <= n <= MAX_FRAME or len(data) < 4 + n:
            break
        out.append((data[4], data[5:4 + n]))
        data = data[4 + n:]
    return out


def _seen(frames) -> str:
    if not frames:
        return ""
    shown = ", ".join(describe(*f) for f in frames[-6:])
    more = f" (+{len(frames) - 6} earlier)" if len(frames) > 6 else ""
    return f" Received meanwhile{more}: {shown}."


def _why(why: str) -> str:
    return f"\n  hint: {why}" if why else ""


class Udp:
    """Blocking UDP socket bound to an ephemeral port."""

    def __init__(self, bind=("0.0.0.0", 0)):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(bind)
        self.port = self.sock.getsockname()[1]
        self.icmp = False
        _open.append(self)

    def send(self, data: bytes, addr=None):
        self.sock.sendto(data, addr or (cfg.host, UDP_PORT))

    def recv(self, timeout=None, pred=None, why=""):
        """Next well-formed datagram matching pred (a Dgram), or None after `timeout` seconds."""
        deadline = time.monotonic() + (cfg.t if timeout is None else timeout)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self.sock.settimeout(left)
            try:
                data, addr = self.sock.recvfrom(65536)
            except (socket.timeout, BlockingIOError):
                return None
            except ConnectionResetError:     # Windows: ICMP port unreachable from an earlier send
                self.icmp = True
                continue
            try:
                d = parse_dgram(data)
            except ValueError as e:
                raise AssertionError(f"malformed datagram from {addr[0]}:{addr[1]}: {e}\n"
                                     f"bytes: {data[:48].hex(' ')}{_why(why)}") from None
            if pred is None or pred(d):
                return d

    def expect(self, t, pred=None, timeout=None, why=""):
        d = self.recv(timeout, lambda d: d.type == t and (pred is None or pred(d)), why)
        if d is None:
            icmp = (" Windows reported ICMP 'port unreachable': nothing listens on UDP "
                    f"{cfg.host}:{UDP_PORT} (bind_udp?)." if self.icmp else "")
            raise AssertionError(f"expected UDP {UNAME[t]} within {cfg.t if timeout is None else timeout:g} s, "
                                 f"got nothing.{icmp}{_why(why)}")
        return d

    def close(self):
        self.sock.close()


def cleanup():
    while _open:
        _open.pop().close()


# ---- asyncio helpers (net_client.py, net_e2e.py) ----

def run_async(fn, limit: float = 30):
    """Run the coroutine function fn() on a fresh event loop. Never hangs: the body gets `limit`
    seconds, leftover tasks are cancelled and get 1 s to finish."""
    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(fn())
        done, _ = loop.run_until_complete(asyncio.wait({task}, timeout=limit))
        if not done:
            task.cancel()
            loop.run_until_complete(asyncio.wait({task}, timeout=1))
            raise AssertionError(f"test body still running after {limit:g} s: something never returned")
        return task.result()
    finally:
        rest = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in rest:
            t.cancel()
        if rest:
            loop.run_until_complete(asyncio.wait(rest, timeout=1))
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), 1))
        except Exception:
            pass
        loop.close()


class Events:
    """on_event callback that records events. Tests match them by class name, like the GUI does."""

    def __init__(self):
        self.items = []

    def __call__(self, ev):
        self.items.append(ev)

    def named(self, name: str, since: int = 0) -> list:
        return [e for e in self.items[since:] if type(e).__name__ == name]

    def summary(self, since: int = 0) -> str:
        items = self.items[since:]
        return ", ".join(short(e, 90) for e in items[-8:]) or "no events"

    async def wait(self, name, pred=None, timeout=None, why="", since=0):
        timeout = cfg.t if timeout is None else timeout
        end = time.monotonic() + timeout
        while True:
            for e in self.items[since:]:
                if type(e).__name__ == name and (pred is None or pred(e)):
                    return e
            if time.monotonic() >= end:
                raise AssertionError(f"expected a {name} event within {timeout:g} s; events so far: "
                                     f"{self.summary(since)}.{_why(why)}")
            await asyncio.sleep(0.01)

    async def none(self, name, pred=None, wait=0.3, why="", since=0):
        await asyncio.sleep(wait)
        bad = [e for e in self.named(name, since) if pred is None or pred(e)]
        if bad:
            raise AssertionError(f"unexpected {short(bad[0], 120)}.{_why(why)}")


# ---- runner ----

_TESTS: list = []


class Skipped(Exception):
    pass


def skip(reason: str):
    raise Skipped(reason)


def check(cond, msg: str):
    """Like assert, but survives python -O and always carries a message."""
    if not cond:
        raise AssertionError(msg)


def test(fn=None, *, slow=False, name=None):
    """Register a test function (decorator)."""
    def reg(f):
        _TESTS.append((name or f.__name__, f, slow))
        return f
    return reg(fn) if fn else reg


def _explain(e: BaseException, script: str, verbose: bool) -> str:
    if verbose:
        return "".join(traceback.format_exception(e)).rstrip()
    tb = traceback.extract_tb(e.__traceback__)
    where = next((f for f in tb if Path(f.filename).name == script and f.name != "<lambda>"), None)
    if isinstance(e, AssertionError):
        text = str(e) or "assertion failed"
    elif isinstance(e, NotImplementedError):
        text = "NotImplementedError: not implemented yet"
    else:
        text = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
    loc = []
    if tb and not isinstance(e, AssertionError):
        last = tb[-1]
        parts = Path(last.filename).parts
        loc.append(f"raised at {'/'.join(parts[-2:])}:{last.lineno} in {last.name}()")
    if where:
        loc.append(f"test: {script}:{where.lineno}")
    return text + (f"\n  ({'; '.join(loc)})" if loc else "")


def main(title: str, before=None, uses_host=True, limit: float = 90):
    """Parse the command line, run the registered tests, print one line each, exit 0 if none failed.
    before(): called once first; if it raises Skipped, every test is skipped with that reason."""
    if hasattr(sys.stdout, "reconfigure"):          # piped / redirected: UTF-8, never crash on ă or 🙂
        enc = None if sys.stdout.isatty() else "utf-8"
        sys.stdout.reconfigure(encoding=enc, errors="backslashreplace", line_buffering=True)
    ap = argparse.ArgumentParser(description=title)
    if uses_host:
        ap.add_argument("--host", default=cfg.host,
                        help="server address (default: $ETHER_HOST, else 127.0.0.1)")
    ap.add_argument("--slow", action="store_true", default=cfg.slow,
                    help="also run the slow tests (or set ETHER_SLOW=1)")
    ap.add_argument("--timeout", type=float, default=cfg.t,
                    help="seconds to wait for each expected reply (default: $ETHER_TIMEOUT, else 2)")
    ap.add_argument("-k", metavar="TEXT", action="append",
                    help="only run tests whose name contains TEXT (repeatable)")
    ap.add_argument("-v", "--verbose", action="store_true", help="full tracebacks on FAIL")
    args = ap.parse_args()
    cfg.host = getattr(args, "host", cfg.host)
    cfg.slow, cfg.t = args.slow, args.timeout
    script = Path(sys.argv[0]).name

    tests = [x for x in _TESTS if not args.k or any(k in x[0] for k in args.k)]
    print(title.format(host=cfg.host))
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    reason = None
    if before:
        try:
            before()
        except Skipped as e:
            reason = str(e)
            print(f"skipping every test: {reason}")
    for name, fn, slow in tests:
        if reason:
            status, detail = "SKIP", ""
        elif slow and not cfg.slow:
            status, detail = "SKIP", "slow test: run with --slow or ETHER_SLOW=1"
        else:
            status, detail = _run_one(fn, script, args.verbose, limit * (4 if slow else 1))
        counts[status] += 1
        if status == "FAIL":
            print(f"FAIL {name}")
            print("    " + detail.replace("\n", "\n    "))
        else:
            print(f"{status} {name}" + (f": {detail}" if detail else ""))
    print(f"\n{counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped")
    sys.exit(1 if counts["FAIL"] else 0)


def _run_one(fn, script, verbose, limit):
    """Run one test in a daemon thread so even a bug in a helper can't hang the whole run."""
    box = {}

    def body():
        try:
            fn()
            box["r"] = ("PASS", "")
        except Skipped as e:
            box["r"] = ("SKIP", str(e))
        except BaseException as e:   # noqa: BLE001 - report anything the code under test raises
            box["r"] = ("FAIL", _explain(e, script, verbose))

    th = threading.Thread(target=body, daemon=True)
    th.start()
    end = time.monotonic() + limit
    while th.is_alive() and time.monotonic() < end:
        th.join(0.2)                 # short joins keep Ctrl+C working on Windows
    cleanup()
    if th.is_alive():
        th.join(2)
        return "FAIL", f"test still running after {limit:g} s and was abandoned"
    return box["r"]
