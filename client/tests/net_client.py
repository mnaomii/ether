"""Tests of ether_core/net.py against a fake server.

    python tests/net_client.py [--slow] [-k NAME] [-v]

No real server needed. The fake runs inside the test: TCP on a free port, UDP on 127.0.0.1:5051
(net.py sends UDP there). If 5051 is taken (a real server running, e.g. in WSL), the fake uses a
free port and patches UDP_PORT in proto/net; UDP tests skip if net.py doesn't follow the patch.
The fake speaks through netkit.py's own encoders, checked against shared/vectors.txt.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import socket
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))       # client/, for ether_core

import netkit as nk                                                 # noqa: E402
from netkit import cfg, check, skip, test                           # noqa: E402

from ether_core import net, proto                                   # noqa: E402
from ether_core.events import (Error, EtherError, FileEvt, History, LoginError,  # noqa: E402
                               MsgReceived, TypingSeen, UserEvt, Users, Welcome)

V = nk.load_vectors()
SID, T_MS = 0x9A1F0001, 1_700_000_000_000
ROOMS2 = [(1, "general"), (2, "misc")]                               # = vector tcp_welcome


# ---------------------------------------------------------------- fake server

@contextmanager
def udp_port_patched(port):
    """Point UDP_PORT at `port` in proto (and in net, if it has its own copy)."""
    saved = [(m, m.UDP_PORT) for m in (proto, net) if hasattr(m, "UDP_PORT")]
    for m, _ in saved:
        m.UDP_PORT = port
    try:
        yield
    finally:
        for m, v in saved:
            m.UDP_PORT = v


class FakeConn:
    """One TCP connection accepted by the fake server."""

    def __init__(self, r, w):
        self.r, self.w = r, w

    async def frame(self, timeout=None, why=""):
        timeout = cfg.t if timeout is None else timeout
        try:
            head = await asyncio.wait_for(self.r.readexactly(4), timeout)
            n = int.from_bytes(head, "big")
            check(1 <= n <= nk.MAX_FRAME, f"the client sent a frame with length field {n} (bytes "
                                          f"{head.hex(' ')}): length = 1 + payload size, big-endian")
            body = await asyncio.wait_for(self.r.readexactly(n), timeout)
        except asyncio.IncompleteReadError as e:
            raise AssertionError(f"the client closed the connection instead of sending a frame"
                                 f"{f' (after {len(e.partial)} bytes)' if e.partial else ''}.{nk._why(why)}") from None
        except TimeoutError:
            raise AssertionError(f"the client sent no complete frame within {timeout:g} s.{nk._why(why)}") from None
        except ConnectionError as e:
            raise AssertionError(f"the client reset the connection ({e}).{nk._why(why)}") from None
        return body[0], body[1:]

    async def expect(self, t, timeout=None, why=""):
        ft, p = await self.frame(timeout, why)
        check(ft == t, f"expected {nk.tname(t)} from the client, got {nk.describe(ft, p, c2s=True)}.{nk._why(why)}")
        try:
            return nk.parse_c2s(ft, p)
        except ValueError as e:
            raise AssertionError(f"malformed {nk.tname(t)} from the client: {e}; payload {p[:48].hex(' ')}") from None

    async def raw(self, n, timeout=None):
        """Up to n raw bytes. Returns (data, how): how = 'n' (got n), 'eof' or 'timeout'."""
        loop = asyncio.get_running_loop()
        end = loop.time() + (cfg.t if timeout is None else timeout)
        data = bytearray()
        while len(data) < n:
            left = end - loop.time()
            if left <= 0:
                return bytes(data), "timeout"
            try:
                chunk = await asyncio.wait_for(self.r.read(n - len(data)), left)
            except TimeoutError:
                return bytes(data), "timeout"
            except ConnectionError:
                return bytes(data), "eof"
            if not chunk:
                return bytes(data), "eof"
            data += chunk
        return bytes(data), "n"

    async def eof(self, timeout=1.0) -> bool:
        """True if the client closes its end within `timeout` (bytes before that are discarded)."""
        _, how = await self.raw(1 << 30, timeout)
        return how == "eof"

    async def silent(self, wait=0.3, why=""):
        data, _ = await self.raw(1 << 16, wait)
        if data:
            frames = nk.split_frames(data)
            what = ", ".join(nk.describe(t, p, c2s=True) for t, p in frames) or data[:32].hex(" ")
            raise AssertionError(f"the client unexpectedly sent {what}.{nk._why(why)}")

    def send(self, data: bytes):
        self.w.write(data)

    async def send_slowly(self, data: bytes):
        """One byte per write, so the client's reader sees partial frames."""
        sock = self.w.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        for i in range(len(data)):
            self.w.write(data[i:i + 1])
            await self.w.drain()
            await asyncio.sleep(0.002)

    def close(self):
        self.w.close()

    def abort(self):
        self.w.transport.abort()


class _Collect(asyncio.DatagramProtocol):
    def __init__(self, q):
        self.q = q

    def datagram_received(self, data, addr):
        self.q.put_nowait((data, addr))

    def error_received(self, exc):
        pass


class Fake:
    """Fake ether-server. `async with Fake() as fake:`; clients made with fake.client() are closed
    on exit, before the fake shuts down."""

    def __init__(self, udp=True):
        self.want_udp = udp

    async def __aenter__(self):
        self.conns, self.dgrams = asyncio.Queue(), asyncio.Queue()
        self.all, self.tasks, self.clients, self.calls = [], set(), [], []
        self.udp, self.fallback, self.got_udp = None, False, False
        self.server = await asyncio.start_server(self._accepted, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self._patch = None
        if self.want_udp:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.bind(("127.0.0.1", nk.UDP_PORT))
            except OSError:
                sock.bind(("127.0.0.1", 0))
                self.fallback = True
            self.udp_port = sock.getsockname()[1]
            self._patch = udp_port_patched(self.udp_port)
            self._patch.__enter__()
            loop = asyncio.get_running_loop()
            self.udp, _ = await loop.create_datagram_endpoint(lambda: _Collect(self.dgrams), sock=sock)
        return self

    async def __aexit__(self, *exc):
        for t in self.calls:                           # a failed test may leave a call running
            if not t.done():
                t.cancel()
        if self.calls:
            await asyncio.wait(self.calls, timeout=1)
        for t in self.calls:                           # mark errors as seen: no "never retrieved" noise
            if t.done() and not t.cancelled():
                t.exception()
        for c in self.clients:
            try:
                await asyncio.wait_for(c.close(), 2)
            except BaseException:
                pass
        for t in self.tasks:
            t.cancel()
        for c in self.all:
            c.abort()
        self.server.close()
        try:
            await asyncio.wait_for(self.server.wait_closed(), 1)
        except BaseException:
            pass
        if self.udp:
            self.udp.close()
        if self._patch:
            self._patch.__exit__(None, None, None)

    async def _accepted(self, r, w):
        self.tasks.add(asyncio.current_task())
        c = FakeConn(r, w)
        self.all.append(c)
        await self.conns.put(c)

    def client(self, on_event):
        c = net.EtherClient(on_event)
        self.clients.append(c)
        return c

    def call(self, coro) -> asyncio.Task:
        """Run a client call (connect/upload/...) as a task the fake cleans up after."""
        t = asyncio.ensure_future(coro)
        self.calls.append(t)
        return t

    async def accept(self, timeout=None) -> FakeConn:
        timeout = cfg.t if timeout is None else timeout
        try:
            return await asyncio.wait_for(self.conns.get(), timeout)
        except TimeoutError:
            raise AssertionError(f"no TCP connection reached the fake server within {timeout:g} s") from None

    async def udp_wait(self, t, what, timeout=None, why=""):
        """Next datagram of type t from the client: (raw, addr, Dgram). Fails, or skips when the
        cause is just port 5051 being taken on this machine."""
        loop = asyncio.get_running_loop()
        timeout = cfg.t if timeout is None else timeout
        end = loop.time() + timeout
        while True:
            left = end - loop.time()
            try:
                raw, addr = await asyncio.wait_for(self.dgrams.get(), max(left, 0.001))
            except TimeoutError:
                break
            self.got_udp = True
            try:
                d = nk.parse_dgram(raw)
            except ValueError as e:
                raise AssertionError(f"malformed datagram from the client: {e}; bytes {raw[:32].hex(' ')}") from None
            if d.type == t:
                return raw, addr, d
        self.missing(what, timeout, why)

    def missing(self, what, timeout, why=""):
        """Fail because `what` never arrived; skip instead if port 5051 being taken explains it."""
        if self.fallback and not self.got_udp:
            skip(f"no {what} arrived, and UDP 5051 is taken on this machine (a real server running, e.g. in "
                 f"WSL?), so the fake listens on {self.udp_port} and patched proto.UDP_PORT. net.py seems to "
                 "hardcode 5051: stop the server and re-run, or read proto.UDP_PORT at send time")
        raise AssertionError(f"no {what} datagram reached the fake server (127.0.0.1:{self.udp_port}) "
                             f"within {timeout:g} s.{nk._why(why)}")


# ---------------------------------------------------------------- helpers

def atest(fn=None, *, slow=False, limit=30, name=None):
    """Register an async test; it runs on its own event loop via netkit.run_async."""
    def reg(f):
        test(lambda: nk.run_async(f, limit), slow=slow, name=name or f.__name__)
        return f
    return reg(fn) if fn else reg


async def within(coro, timeout, what):
    """Await a client call; its own exceptions propagate. Fails (instead of hanging) on timeout."""
    task = asyncio.ensure_future(coro)
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        task.cancel()
        raise AssertionError(f"{what} (still running after {timeout:g} s)")
    return task.result()


async def during(tasks, coro, what=""):
    """Await a fake-server step while client call(s) run. If a call fails first, raise its error,
    so a NotImplementedError isn't reported as 'nothing arrived'."""
    tasks = tasks if isinstance(tasks, (list, tuple, set)) else [tasks]
    fut = asyncio.ensure_future(coro)
    pending = set(tasks)
    try:
        while True:
            await asyncio.wait({fut, *pending}, return_when=asyncio.FIRST_COMPLETED)
            if fut.done():
                return fut.result()
            for t in [t for t in pending if t.done()]:
                pending.discard(t)
                if t.cancelled():
                    raise AssertionError(f"the client call was cancelled. {what}")
                if t.exception() is not None:
                    raise _call_failed(t.exception(), what)
    finally:
        if not fut.done():
            fut.cancel()


def _call_failed(e, what):
    """A client call's unexpected exception, with where it came from. Stubs stay NotImplementedError."""
    if isinstance(e, (NotImplementedError, AssertionError)):
        return e
    tb = traceback.extract_tb(e.__traceback__)
    where = next((f for f in reversed(tb) if "ether_core" in f.filename), tb[-1] if tb else None)
    at = f" (at {Path(where.filename).name}:{where.lineno} in {where.name})" if where else ""
    step = f" while waiting for: {what}" if what else ""
    return AssertionError(f"the client call raised {type(e).__name__}: {e}{at}{step}")


async def finish(task, what, timeout=None):
    """Result of a client call that should complete now."""
    timeout = cfg.t + 1 if timeout is None else timeout
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        task.cancel()
        raise AssertionError(f"{what} (still running after {timeout:g} s)")
    return task.result()


async def raised(task, what, timeout=None):
    """The exception a client call raises. NotImplementedError is re-raised as is."""
    timeout = cfg.t if timeout is None else timeout
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        task.cancel()
        raise AssertionError(f"{what}, but it was still running after {timeout:g} s")
    if task.cancelled():
        raise AssertionError(f"{what}, but it raised CancelledError, which the apps don't catch "
                             "(it's a BaseException): raise ConnectionError instead")
    exc = task.exception()
    if exc is None:
        raise AssertionError(f"{what}, but it returned {nk.short(task.result())}")
    if isinstance(exc, NotImplementedError):
        raise exc
    return exc


async def no_raise(coro, what):
    try:
        await within(coro, 1, f"{what} must return right away")
    except NotImplementedError:
        raise
    except AssertionError:
        raise
    except BaseException as e:
        raise AssertionError(f"{what} raised {type(e).__name__}: {e}. typing() must never raise: it's only "
                             "a hint, so catch and log any error") from None


async def connected(fake, ev=None, sid=SID, nick="ana", pw="parola"):
    """A client logged in to the fake: (client, events, control FakeConn, Welcome)."""
    ev = ev if ev is not None else nk.Events()
    c = fake.client(ev)
    task = fake.call(c.connect("127.0.0.1", fake.port, nick, pw))
    ctl = await during(task, fake.accept(), "connect() opens a TCP connection to host:port")
    await during(task, ctl.expect(nk.LOGIN, why="the first frame on the control connection is LOGIN"))
    ctl.send(nk.welcome(sid, ROOMS2))
    w = await finish(task, "connect() should return right after WELCOME")
    return c, ev, ctl, w


_ASYNCIO_DIR = os.path.dirname(asyncio.__file__)


def _infrastructure(t) -> bool:
    """asyncio's own tasks (the runner's wait(), the proactor's accept loop)."""
    code = getattr(t.get_coro(), "cr_code", None)
    return code is not None and code.co_filename.startswith(_ASYNCIO_DIR)


async def leftover(fake=None, settle=0.3):
    """Tasks still running that the test or the fake didn't start (i.e. the client's)."""
    await asyncio.sleep(settle)
    me = asyncio.current_task()
    ours = fake.tasks if fake else set()
    return [t for t in asyncio.all_tasks()
            if t is not me and not t.done() and t not in ours and not _infrastructure(t)]


def datagram_transports(c):
    """(owner, attribute, transport) for every DatagramTransport the client holds (1 level deep)."""
    out = []
    for owner in [c] + [v for v in vars(c).values() if hasattr(v, "__dict__") and not callable(v)]:
        for k, v in list(vars(owner).items()):
            if isinstance(v, asyncio.DatagramTransport):
                out.append((owner, k, v))
    return out


class _Broken:
    def __init__(self, real):
        self._real = real

    def sendto(self, *a, **k):
        raise OSError("simulated UDP send failure")

    def __getattr__(self, name):
        return getattr(self._real, name)


def tcp_events(items):
    """Drop UDP events: they have no order relative to TCP ones."""
    return [e for e in items if type(e).__name__ not in ("Pong", "TypingSeen")]


def free_port(kind=socket.SOCK_DGRAM) -> int:
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def tmpdir():
    return tempfile.TemporaryDirectory(prefix="ether-test-", ignore_cleanup_errors=True)


# ---------------------------------------------------------------- the test helpers themselves

@test
def wire_helpers_match_vectors():
    built = {
        "tcp_login_ana": nk.login("ana", "parola"), "tcp_join_1": nk.join(1),
        "tcp_say_1_salut": nk.say(1, "salut"), "tcp_say_2_buna": nk.say(2, "bună"), "tcp_bye": nk.bye(),
        "tcp_welcome": nk.welcome(SID, ROOMS2), "tcp_msg": nk.msg(1, T_MS, "bob", "salut"),
        "tcp_error_bad_pass": nk.error(7, "wrong password"), "udp_discover": nk.discover(),
        "udp_discover_reply": nk.discover_reply("ether-1", 5050, 3), "udp_typing": nk.typing(SID, 1),
        "udp_ping": nk.ping(SID, T_MS), "udp_pong": nk.pong(SID, T_MS)}
    for name, b in built.items():
        check(b == V[name], f"bug in tests/netkit.py: it encodes {name} differently from shared/vectors.txt")
    check(set(V) - set(built) == {"udp_bad_magic"}, "a vector in shared/vectors.txt isn't cross-checked here")


# ---------------------------------------------------------------- discover

@atest
async def discover_returns_empty_list_when_nobody_answers():
    with udp_port_patched(free_port()):
        t0 = time.monotonic()
        res = await within(net.discover(timeout=0.5), 5, "discover(timeout=0.5) must return")
        dt = time.monotonic() - t0
    check(isinstance(res, list), f"discover() must return a list, got {type(res).__name__}")
    if res:
        skip(f"something answered DISCOVER on this network ({res}), so the empty case can't be checked here")
    check(0.4 <= dt <= 1.5, f"discover(timeout=0.5) took {dt:.2f} s: send one DISCOVER, collect replies for "
                            "`timeout` seconds, return")


@atest
async def discover_finds_a_server_by_broadcast():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("0.0.0.0", nk.UDP_PORT))
        fallback = False
    except OSError:
        sock.bind(("0.0.0.0", 0))
        fallback = True
    name, got = "fake-" + secrets.token_hex(3), []

    class Answer(asyncio.DatagramProtocol):
        def connection_made(self, tr):
            self.tr = tr

        def datagram_received(self, data, addr):
            got.append(data)
            for _ in range(2):                         # twice: discover() must dedupe
                self.tr.sendto(nk.discover_reply(name, 5050, 4), addr)

    loop = asyncio.get_running_loop()
    tr, _ = await loop.create_datagram_endpoint(Answer, sock=sock)
    try:
        with udp_port_patched(sock.getsockname()[1]):
            res = await within(net.discover(timeout=1.0), 5, "discover(timeout=1.0) must return")
    finally:
        tr.close()
    if not got:
        busy = " (or: 5051 is taken here, the fake used another port, and net.py hardcodes 5051)" if fallback else ""
        skip("the DISCOVER broadcast didn't loop back to this machine (several network adapters or the "
             f"firewall){busy}. Check discovery against a real server")
    check(got[0] == V["udp_discover"], f"DISCOVER datagram: expected {V['udp_discover'].hex(' ')}, got {got[0].hex(' ')}")
    mine = [s for s in res if s.name == name]
    keys = [(s.host, s.tcp_port) for s in mine]          # one per address: a multi-homed host shows up once per adapter
    check(mine and len(keys) == len(set(keys)), f"the fake answered twice from each address: expected one ServerInfo "
                                                f"named {name!r} per (host, tcp_port), got {res}")
    s = mine[0]
    check(s.host != "", "ServerInfo.host is empty: fill it from the reply's source address (addr[0])")
    check((s.tcp_port, s.users_online) == (5050, 4), f"expected tcp_port 5050, users_online 4, got {s}")


@atest
async def discover_closes_its_socket_even_when_cancelled():
    loop = asyncio.get_running_loop()
    made, real = [], loop.create_datagram_endpoint

    async def spy(*a, **k):
        tr, pr = await real(*a, **k)
        made.append(tr)
        return tr, pr

    loop.create_datagram_endpoint = spy
    try:
        with udp_port_patched(free_port()):
            await within(net.discover(timeout=0.2), 5, "discover(timeout=0.2) must return")
            if not made:
                skip("discover() doesn't use loop.create_datagram_endpoint, so this test can't see its socket")
            check(all(t.is_closing() for t in made), "discover() returned but left its UDP socket open")
            made.clear()
            task = asyncio.create_task(net.discover(timeout=5))
            await asyncio.sleep(0.3)
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=2)
            check(done, "discover() didn't stop within 2 s of being cancelled")
    finally:
        del loop.create_datagram_endpoint
    check(made and all(t.is_closing() for t in made),
          "after cancelling discover() its UDP socket is still open. The GUI cancels discovery when the "
          "dialog closes: close the transport in a finally")


# ---------------------------------------------------------------- connect

@atest
async def connect_sends_login_and_returns_welcome():
    async with Fake() as fake:
        ev = nk.Events()
        c = fake.client(ev)
        task = fake.call(c.connect("127.0.0.1", fake.port, "ana", "parola"))
        ctl = await during(task, fake.accept(), "connect() opens a TCP connection to host:port")
        ft, p = await during(task, ctl.frame(why="connect() sends LOGIN right after connecting"))
        check(nk.frame(ft, p) == V["tcp_login_ana"],
              f"LOGIN 'ana' 'parola' must be the vector tcp_login_ana, got {nk.frame(ft, p).hex(' ')}")
        ctl.send(V["tcp_welcome"])
        w = await finish(task, "connect() should return right after WELCOME")
        want = Welcome(SID, ROOMS2)
        check(w == want, f"connect() returned {w!r}, expected {want!r}")
        check(c.session_id == SID, f"client.session_id = {c.session_id!r} after connect(), expected 0x9A1F0001")
        check(isinstance(c.udp_port, int) and c.udp_port > 0,
              f"client.udp_port = {c.udp_port!r} after connect(): bind the UDP socket to port 0 and store the "
              "port the OS picked (transport.get_extra_info('sockname')[1])")
        got = ev.named("Welcome")
        check(got == [want], f"connect() must emit exactly one Welcome event, got: {ev.summary()}")


@atest
async def welcome_is_emitted_last():
    async with Fake() as fake:
        ev, seen = nk.Events(), {}

        def on_event(e):                               # what the GUI does (main_window.py:204)
            ev(e)
            if type(e).__name__ == "Welcome" and not seen:
                seen.update(sid=c.session_id, udp=c.udp_port, first=len(tcp_events(ev.items)) == 1,
                            join=asyncio.get_running_loop().create_task(c.join(1)))

        c = fake.client(on_event)
        task = fake.call(c.connect("127.0.0.1", fake.port, "ana", "parola"))
        ctl = await during(task, fake.accept())
        await during(task, ctl.expect(nk.LOGIN))
        ctl.send(nk.welcome(SID, ROOMS2))
        await finish(task, "connect() should return right after WELCOME")
        check(seen, f"no Welcome event: connect() must call on_event(welcome) (got {ev.summary()})")
        check(seen["first"], f"Welcome must be the first event, got: {ev.summary()}")
        check(seen["sid"] == SID and seen["udp"], "when Welcome was emitted, session_id/udp_port weren't set yet: "
                                                  "get everything ready, then emit Welcome last")
        try:
            await ctl.expect(nk.JOIN, why="the GUI calls join() from its Welcome handler; that JOIN must reach the server")
        except AssertionError:
            j = seen["join"]
            if j.done() and not j.cancelled() and j.exception():
                raise AssertionError(f"join() called from the Welcome handler raised {j.exception()!r}: "
                                     "emit Welcome only once the connection is fully set up") from None
            raise
        ctl.send(nk.history(1, []) + nk.users(1, ["ana"]))
        await ev.wait("History", why="the reader task must already be running when Welcome is emitted")
        await ev.wait("Users")


@atest
async def udp_hello_carries_the_session_id():
    async with Fake() as fake:
        c, ev, ctl, _ = await connected(fake)
        raw, addr, _ = await fake.udp_wait(nk.U_UDP_HELLO, "UDP_HELLO",
                                           why="right after WELCOME, send UDP_HELLO with the session id to (server ip, 5051)")
        want = bytes.fromhex("45 54 01 03 9A 1F 00 01")
        check(raw == want, f"UDP_HELLO for session 0x9A1F0001 should be {want.hex(' ')}, got {raw.hex(' ')}")
        check(addr[1] == c.udp_port, f"UDP_HELLO came from port {addr[1]} but client.udp_port = {c.udp_port}: "
                                     "report the port of the socket that actually sends UDP")


@atest
async def login_error_raises_login_error_and_cleans_up():
    async with Fake() as fake:
        ev = nk.Events()
        c = net.EtherClient(ev)                        # the apps never close() after a failed connect
        task = fake.call(c.connect("127.0.0.1", fake.port, "ana", "parola"))
        ctl = await during(task, fake.accept())
        await during(task, ctl.expect(nk.LOGIN))
        ctl.send(V["tcp_error_bad_pass"])
        exc = await raised(task, "connect() should raise LoginError when the server answers ERROR")
        check(isinstance(exc, LoginError), f"connect() raised {type(exc).__name__}: {exc}; expected LoginError")
        check(exc.code == 7 and "wrong password" in str(exc),
              f"LoginError must carry code 7 and the server's text 'wrong password' (the GUI shows str(e)), "
              f"got code {getattr(exc, 'code', None)}, str {str(exc)!r}")
        check(not ev.items, f"a failed connect() emits nothing (no Welcome, no Disconnected), got: {ev.summary()}")
        check(await ctl.eof(1.0), "connect() raised but left the TCP connection open: close everything it opened "
                                  "before re-raising (the apps never call close() after a failed connect)")
        left = await leftover(fake)
        check(not left, f"connect() raised but left {len(left)} task(s) running: {left}")


@atest
async def connect_raises_connection_error_when_server_hangs_up():
    async with Fake() as fake:
        ev = nk.Events()
        c = net.EtherClient(ev)
        task = fake.call(c.connect("127.0.0.1", fake.port, "ana", "parola"))
        ctl = await during(task, fake.accept())
        await during(task, ctl.expect(nk.LOGIN))
        ctl.close()
        exc = await raised(task, "connect() should raise when the server closes instead of answering LOGIN", timeout=3)
        check(isinstance(exc, OSError), f"connect() raised {type(exc).__name__} ({exc}); expected ConnectionError "
                                        "(an OSError, which the apps catch). Turn asyncio.IncompleteReadError "
                                        "into ConnectionError")
        check(not ev.items, f"a failed connect() emits nothing, got: {ev.summary()}")


@atest
async def connect_to_closed_port_raises_oserror():
    ev = nk.Events()
    c = net.EtherClient(ev)
    task = asyncio.create_task(c.connect("127.0.0.1", free_port(socket.SOCK_STREAM), "ana", "parola"))
    exc = await raised(task, "connect() to a port where nothing listens should raise", timeout=8)
    check(isinstance(exc, OSError), f"connect() raised {type(exc).__name__} ({exc}); expected an OSError "
                                    "(ConnectionRefusedError)")
    check(not ev.items, f"a failed connect() emits nothing, got: {ev.summary()}")
    left = await leftover()
    check(not left, f"connect() raised but left {len(left)} task(s) running: {left}")


@atest(limit=30)
async def connect_times_out_when_server_never_answers():
    async with Fake() as fake:
        c = net.EtherClient(nk.Events())
        task = fake.call(c.connect("127.0.0.1", fake.port, "ana", "parola"))
        ctl = await during(task, fake.accept())
        await during(task, ctl.expect(nk.LOGIN))
        exc = await raised(task, "connect() must give up when the server never answers LOGIN: wrap steps 1-3 "
                                 "in asyncio.wait_for(..., 5)", timeout=9)
        check(isinstance(exc, OSError), f"connect() raised {type(exc).__name__} ({exc}); expected TimeoutError "
                                        "(an OSError since Python 3.11)")
        check(await ctl.eof(1.0), "after timing out, connect() must close the TCP connection it opened")


# ---------------------------------------------------------------- calls that just send

@atest
async def join_leave_say_send_one_frame_and_return():
    async with Fake() as fake:
        c, ev, ctl, _ = await connected(fake)
        calls = [(lambda: c.join(1), V["tcp_join_1"], "join(1)"),
                 (lambda: c.leave(3), nk.leave(3), "leave(3)"),
                 (lambda: c.say(1, "salut"), V["tcp_say_1_salut"], "say(1, 'salut')"),
                 (lambda: c.say(2, "bună"), V["tcp_say_2_buna"], "say(2, 'bună')")]
        for call, want, what in calls:
            await within(call(), 1, f"{what} must send its frame and return without waiting for a reply")
            ft, p = await ctl.frame(why=f"{what} sends one frame")
            got = nk.frame(ft, p)
            check(got == want, f"{what}: expected {want.hex(' ')}, got {got.hex(' ')}")


@atest
async def own_message_comes_only_from_the_server_echo():
    async with Fake() as fake:
        c, ev, ctl, _ = await connected(fake)
        n0 = len(ev.items)
        await within(c.say(1, "salut"), 1, "say() must return without waiting for the echo")
        await ctl.expect(nk.SAY)
        await ev.none("MsgReceived", since=n0, why="don't show your own message locally: the server echoes it "
                                                   "back as a normal MSG")
        ctl.send(nk.msg(1, T_MS, "ana", "salut"))
        await ev.wait("MsgReceived", since=n0)
        await asyncio.sleep(0.2)
        got = ev.named("MsgReceived", n0)
        check(got == [MsgReceived(1, T_MS, "ana", "salut")], f"expected exactly one MsgReceived (the echo), got {got}")


@atest
async def say_limit_is_4000_bytes():
    async with Fake() as fake:
        c, ev, ctl, _ = await connected(fake)
        long = "ă" * 2001                              # 4002 bytes, 2001 characters
        try:
            await within(c.say(1, long), 1, "say() must not wait for a reply")
            refused = False
        except ValueError:
            refused = True
        if refused:
            await ctl.silent(0.3, why="say() raised ValueError, so it must not have sent the SAY as well")
        else:                                          # also allowed: send it, the server answers ERROR 8
            m = await ctl.expect(nk.SAY, why="say() of 4002 bytes didn't raise ValueError, so it must send the SAY")
            check(m.text == long, "the SAY text changed on the way")
        exact = "ă" * 2000                             # 4000 bytes
        try:
            await within(c.say(1, exact), 1, "say() must not wait for a reply")
        except ValueError:
            raise AssertionError("say() rejected exactly 4000 bytes: only len(text.encode()) > 4000 is too long") from None
        m = await ctl.expect(nk.SAY, why="a 4000-byte text is allowed")
        check(m.text == exact, "the SAY text changed on the way")


@atest
async def unsolicited_error_becomes_error_event():
    async with Fake() as fake:
        c, ev, ctl, _ = await connected(fake)
        await within(c.join(99), 1, "join() returns without waiting for a reply")
        await ctl.expect(nk.JOIN)
        ctl.send(nk.error(2, "no such room"))
        e = await ev.wait("Error", why="an ERROR answering JOIN/SAY becomes an Error event")
        check((e.code, e.message) == (2, "no such room"), f"expected Error(2, 'no such room'), got {e}")


# ---------------------------------------------------------------- typing

@atest
async def typing_sends_one_udp_datagram():
    async with Fake() as fake:
        c, ev, ctl, _ = await connected(fake)
        await no_raise(c.typing(1), "typing(1)")
        raw, _, _ = await fake.udp_wait(nk.U_TYPING, "TYPING", why="typing(room) sends one UDP TYPING with the "
                                                                    "session id to (server ip, 5051)")
        check(raw == V["udp_typing"], f"TYPING for session 0x9A1F0001, room 1: expected {V['udp_typing'].hex(' ')}, "
                                      f"got {raw.hex(' ')}")


@atest
async def typing_before_connect_does_not_raise():
    c = net.EtherClient(nk.Events())
    await no_raise(c.typing(1), "typing() before connect()")


@atest
async def typing_survives_icmp_port_unreachable():
    async with Fake() as fake:
        c, ev, ctl, _ = await connected(fake)
        fake.udp.close()                               # server's UDP port closed: Windows reports ICMP
        await asyncio.sleep(0.1)                       # "port unreachable" as ConnectionResetError
        for _ in range(3):
            await no_raise(c.typing(1), "typing() after the server's UDP port closed")
            await asyncio.sleep(0.1)
        await ev.none("Disconnected", why="a UDP error (ICMP port unreachable) must not end the session: implement "
                                          "error_received() in the datagram protocol and just log")
        await within(c.say(1, "still here"), 1, "say() after UDP errors")
        await ctl.expect(nk.SAY, why="the TCP session must keep working after UDP errors")


@atest
async def typing_swallows_send_errors():
    async with Fake() as fake:
        c, ev, ctl, _ = await connected(fake)
        spots = datagram_transports(c)
        if not spots:
            skip("couldn't find the client's UDP transport to break it")
        for owner, attr, real in spots:
            setattr(owner, attr, _Broken(real))
        try:
            await no_raise(c.typing(1), "typing() when the UDP send raises OSError")
        finally:
            for owner, attr, real in spots:
                setattr(owner, attr, real)


@atest
async def typing_after_close_does_not_raise():
    async with Fake() as fake:
        c, ev, ctl, _ = await connected(fake)
        await within(c.close(), 2, "close() must finish within 2 s")
        await no_raise(c.typing(1), "typing() after close()")


# ---------------------------------------------------------------- the reader

@atest
async def frames_split_into_single_bytes_decode():
    async with Fake() as fake:
        ev = nk.Events()
        c = fake.client(ev)
        task = fake.call(c.connect("127.0.0.1", fake.port, "ana", "parola"))
        ctl = await during(task, fake.accept())
        await during(task, ctl.expect(nk.LOGIN))
        await ctl.send_slowly(V["tcp_welcome"])
        await finish(task, "connect() with WELCOME arriving 1 byte at a time should return: readexactly(4), "
                           "then readexactly(length)", timeout=5)
        n0 = len(ev.items)
        await ctl.send_slowly(nk.users(1, ["ana", "bob"]) + nk.user_evt(1, 1, "cris") +
                              nk.msg(1, T_MS, "bob", "bună 🙂") + nk.file_evt(1, 3, "notes ă.pdf", 2_000_000, "bob") +
                              nk.error(8, "text too long") + nk.user_evt(1, 0, "cris"))
        want = [Users(1, ["ana", "bob"]), UserEvt(1, True, "cris"), MsgReceived(1, T_MS, "bob", "bună 🙂"),
                FileEvt(1, 3, "notes ă.pdf", 2_000_000, "bob"), Error(8, "text too long"), UserEvt(1, False, "cris")]
        try:
            await ev.wait("UserEvt", lambda e: not e.joined, since=n0, timeout=3)
        except AssertionError:
            pass
        got = tcp_events(ev.items[n0:])
        check(got == want, f"events from frames sent 1 byte at a time:\n  got  {got}\n  want {want}")
        check(all(isinstance(e.joined, bool) for e in got if isinstance(e, UserEvt)),
              "UserEvt.joined should be a bool (joined != 0)")


@atest
async def many_frames_in_one_write_and_unknown_types_are_skipped():
    async with Fake() as fake:
        c, ev, ctl, _ = await connected(fake)
        n0 = len(ev.items)
        ctl.send(nk.history(1, []) + nk.frame(0x55, b"from the future") + nk.users(1, ["ana"]) +
                 nk.msg(1, T_MS, "bob", "salut"))
        await ev.wait("MsgReceived", since=n0, why="frames that arrive in one read must all be decoded; an unknown "
                                                   "type is skipped (the length keeps the stream in sync)")
        want = [History(1, []), Users(1, ["ana"]), MsgReceived(1, T_MS, "bob", "salut")]
        got = tcp_events(ev.items[n0:])
        check(got == want, f"expected {want}, got {got}")


@atest
async def history_entries_decode():
    async with Fake() as fake:
        c, ev, ctl, _ = await connected(fake)
        ctl.send(nk.history(2, [(2, T_MS, "bob", "salut"), (2, T_MS + 1, "cris", "bună")]))
        h = await ev.wait("History", why=nk.HISTORY_FORMAT)
        want = History(2, [MsgReceived(2, T_MS, "bob", "salut"), MsgReceived(2, T_MS + 1, "cris", "bună")])
        check(h == want, f"expected {want}, got {h}. {nk.HISTORY_FORMAT}")


@atest
async def oversized_frame_from_server_disconnects():
    async with Fake() as fake:
        c, ev, ctl, _ = await connected(fake)
        ctl.send(b"\xff\xff\xff\xf0\x06" + b"x" * 10)
        await ev.wait("Disconnected", why="a length field of ~4 GB is garbage: check it against MAX_FRAME before "
                                          "readexactly(length) and treat the connection as broken")


# ---------------------------------------------------------------- disconnect / close

def _server_hangs_up(how):
    async def t():
        async with Fake() as fake:
            c, ev, ctl, _ = await connected(fake)
            n0 = len(ev.items)
            if how == "bye":
                ctl.send(nk.bye())
                ctl.close()
            elif how == "reset":
                ctl.abort()
            else:
                ctl.close()
            await ev.wait("Disconnected", since=n0, why="when the control connection drops (EOF, reset or server "
                                                        "BYE), emit Disconnected (the reader task's finally)")
            await asyncio.sleep(0.5)
            got = ev.named("Disconnected")
            check(len(got) == 1, f"Disconnected must be emitted exactly once, got {len(got)}: {got}")
            await within(c.close(), 2, "close() after the server hung up must finish within 2 s")
            await asyncio.sleep(0.2)
            check(len(ev.named("Disconnected")) == 1, "close() after a Disconnected emitted another one")
    atest(t, name=f"disconnected_once_when_server_hangs_up[{how}]")


for _how in ("close", "reset", "bye"):
    _server_hangs_up(_how)


@atest
async def close_sends_bye_and_emits_nothing():
    async with Fake() as fake:
        c, ev, ctl, _ = await connected(fake)
        n0 = len(ev.items)
        await within(c.close(), 2, "close() must finish within 2 s (the GUI gives it 2 s)")
        ft, p = await ctl.frame(why="close() sends BYE first")
        check(nk.frame(ft, p) == V["tcp_bye"], f"expected BYE (00 00 00 01 7F), got {nk.describe(ft, p, c2s=True)}")
        check(await ctl.eof(1.0), "after BYE, close() must close the TCP connection")
        await ev.none("Disconnected", since=n0, why="no Disconnected when the app itself called close()")
        await within(c.close(), 2, "a second close() must be harmless and quick")
        left = await leftover(fake)
        check(not left, f"after close() {len(left)} task(s) are still running: {left}")
        open_udp = [a for _, a, t in datagram_transports(c) if not t.is_closing()]
        check(not open_udp, f"after close() the UDP transport ({open_udp}) is still open")


@atest
async def close_without_connect_is_safe():
    ev = nk.Events()
    c = net.EtherClient(ev)
    await within(c.close(), 2, "close() on a client that never connected")
    await within(c.close(), 2, "a second close()")
    check(not ev.items, f"close() without connect() emitted {ev.summary()}")


def _pending_upload_fails(how):
    async def t():
        async with Fake() as fake:
            c, ev, ctl, _ = await connected(fake)
            with tmpdir() as d:
                path = os.path.join(d, "a.txt")
                Path(path).write_bytes(b"x" * 100)
                up = fake.call(c.upload(1, path))
                await during(up, ctl.expect(nk.FILE_UP_REQ))
                if how == "disconnect":
                    ctl.close()
                else:
                    await within(c.close(), 2, "close() must finish within 2 s even with an upload waiting")
                exc = await raised(up, f"upload() waiting for FILE_UP_OK must fail promptly when "
                                       f"{'the connection drops' if how == 'disconnect' else 'the app calls close()'}: "
                                       "fail the pending future", timeout=2.5)
                check(isinstance(exc, (OSError, EtherError)),
                      f"upload() raised {type(exc).__name__}: {exc}; expected ConnectionError (an OSError)")
    atest(t, name=f"pending_upload_fails_on[{how}]")


for _how in ("disconnect", "close"):
    _pending_upload_fails(_how)


# ---------------------------------------------------------------- upload

@atest
async def upload_streams_the_file_on_a_new_connection():
    async with Fake() as fake:
        c, ev, ctl, _ = await connected(fake)
        with tmpdir() as d:
            content = os.urandom(300_000)
            path = os.path.join(d, "notes ă.bin")
            Path(path).write_bytes(content)
            prog = []
            up = fake.call(c.upload(2, path, progress=lambda done, total: prog.append((done, total))))
            req = await during(up, ctl.expect(nk.FILE_UP_REQ, why="upload() first sends FILE_UP_REQ on the control connection"))
            check(req == (2, "notes ă.bin", 300_000), f"FILE_UP_REQ should be (room 2, 'notes ă.bin', 300000), got "
                                                     f"{req}. Send only os.path.basename(path)")
            ctl.send(nk.file_up_ok(7, 0x51A2B3C4))
            dc = await during(up, fake.accept(), "after FILE_UP_OK, upload() opens a NEW TCP connection to host:port")
            hello, _ = await dc.raw(13)
            want = nk.data_hello(7, 0x51A2B3C4)
            check(hello == want, f"the data connection must start with DATA_HELLO = {want.hex(' ')}, got {hello.hex(' ')}")
            body, how = await dc.raw(len(content) + 1, timeout=5)
            if body != content:
                i = next((k for k, (x, y) in enumerate(zip(body, content)) if x != y), min(len(body), len(content)))
                raise AssertionError(f"after DATA_HELLO the fake got {len(body)} raw bytes ({how}), expected exactly "
                                     f"{len(content)}; first difference at offset {i}")
            check(how == "eof", "upload() sent every byte but didn't close the data connection")
            await finish(up, "upload() should return once every byte is sent")
            check(prog and prog[-1] == (300_000, 300_000), f"progress must end at (size, size), got {prog[-3:]}")
            dones = [p[0] for p in prog]
            check(dones == sorted(dones) and all(t == 300_000 and 0 <= x <= t for x, t in prog),
                  f"progress(done, total) must count up to total: {prog[:4]}...")
            await ctl.silent(0.2, why="file bytes go on the data connection, never on the control connection")


@atest
async def upload_error_answer_raises_ether_error():
    async with Fake() as fake:
        c, ev, ctl, _ = await connected(fake)
        with tmpdir() as d:
            path = os.path.join(d, "big.bin")
            Path(path).write_bytes(b"x" * 10)
            up = fake.call(c.upload(1, path))
            await during(up, ctl.expect(nk.FILE_UP_REQ))
            ctl.send(nk.error(3, "file too large"))
            exc = await raised(up, "upload() must raise EtherError when the server answers ERROR")
            check(isinstance(exc, EtherError) and exc.code == 3, f"expected EtherError with code 3, got "
                                                                 f"{type(exc).__name__}: {exc}")
            await ev.none("Error", wait=0.2, why="an ERROR that answers a pending file request is raised by "
                                                 "upload(), not also emitted as an Error event")
            check(fake.conns.empty(), "upload() opened a data connection although the server refused")


@atest
async def unrelated_error_during_upload_is_an_event():
    async with Fake() as fake:
        c, ev, ctl, _ = await connected(fake)
        with tmpdir() as d:
            path = os.path.join(d, "a.bin")
            Path(path).write_bytes(b"abc")
            up = fake.call(c.upload(1, path))
            await during(up, ctl.expect(nk.FILE_UP_REQ))
            ctl.send(nk.error(8, "text too long") + nk.file_up_ok(5, 99))
            dc = await during(up, fake.accept(), "ERROR 8 can't answer FILE_UP_REQ (only 2 or 3 can), so it is an "
                                                 "Error event and upload() keeps waiting for FILE_UP_OK")
            data, _ = await dc.raw(13 + 3 + 1)
            check(data == nk.data_hello(5, 99) + b"abc", f"data connection got {data.hex(' ')}")
            await finish(up, "upload() should return once every byte is sent")
            await ev.wait("Error", lambda e: e.code == 8)


@atest
async def concurrent_uploads_each_use_their_own_token():
    async with Fake() as fake:
        c, ev, ctl, _ = await connected(fake)
        with tmpdir() as d:
            files = {"a.bin": b"A" * 1000, "b.bin": b"B" * 2000}
            for name, data in files.items():
                Path(d, name).write_bytes(data)
            ups = [fake.call(c.upload(1, os.path.join(d, n))) for n in files]
            reqs = {}

            async def answer():                        # like the server: one answer per request, in order
                for i in range(2):
                    req = await ctl.expect(nk.FILE_UP_REQ, timeout=5)
                    reqs[0xA000 + i] = req
                    ctl.send(nk.file_up_ok(100 + i, 0xA000 + i))

            fake.tasks.add(asyncio.create_task(answer()))
            got = {}
            for n in range(2):
                try:
                    dc = await during(ups, fake.accept(timeout=5), "each upload opens its own data connection")
                except AssertionError as e:
                    if n == 0 or "no TCP connection" not in str(e):
                        raise
                    raise AssertionError("two uploads started together, but only one opened its data connection: "
                                         "the other never got its FILE_UP_OK. With two requests in flight each "
                                         "answer must go to its own request: use an asyncio.Lock around 'send REQ, "
                                         "wait for OK'") from None
                hello, _ = await dc.raw(13)
                h = nk.parse_c2s(hello[4], hello[5:]) if len(hello) == 13 else None
                check(isinstance(h, nk.DataHello), f"data connection must start with DATA_HELLO, got {hello.hex(' ')}")
                got[h.token], _ = await dc.raw(10_000, timeout=3)
            done, _ = await asyncio.wait(ups, timeout=5)
            check(len(done) == 2, "an upload is still waiting for its FILE_UP_OK: with two requests in flight, both "
                                  "must get their answer. Use an asyncio.Lock around 'send REQ, wait for OK'")
            for u in ups:
                u.result()
            for token, data in got.items():
                req = reqs.get(token)
                check(req is not None and data == files.get(req.name),
                      f"the data sent with token 0x{token:X} doesn't match the file of the request it answered "
                      f"({req}): FILE_UP_OK answers go to the requests in order")


# ---------------------------------------------------------------- download

@atest
async def download_writes_dest_and_reports_progress():
    async with Fake() as fake:
        c, ev, ctl, _ = await connected(fake)
        with tmpdir() as d:
            content, dest, prog = os.urandom(200_000), os.path.join(d, "out.bin"), []
            down = fake.call(c.download(3, dest, progress=lambda done, total: prog.append((done, total))))
            req = await during(down, ctl.expect(nk.FILE_DOWN_REQ, why="download() first sends FILE_DOWN_REQ"))
            check(req.fid == 3, f"FILE_DOWN_REQ should ask for file 3, got {req}")
            ctl.send(nk.file_down_ok(9, 0xBEEF, len(content)))
            dc = await during(down, fake.accept(), "after FILE_DOWN_OK, download() opens a new TCP connection")
            hello, _ = await dc.raw(13)
            check(hello == nk.data_hello(9, 0xBEEF), f"expected DATA_HELLO(9, 0xBEEF), got {hello.hex(' ')}")
            for i in range(0, len(content), 50_000):
                dc.send(content[i:i + 50_000])
                await dc.w.drain()
                await asyncio.sleep(0.01)
            dc.close()
            await finish(down, "download() should return once it has `size` bytes")
            check(Path(dest).read_bytes() == content, "dest doesn't hold exactly the bytes the server sent")
            check(prog and prog[-1] == (200_000, 200_000), f"progress must end at (size, size), got {prog[-3:]}")


@atest
async def download_cut_short_raises_and_deletes_partial_file():
    async with Fake() as fake:
        c, ev, ctl, _ = await connected(fake)
        with tmpdir() as d:
            dest = os.path.join(d, "out.bin")
            down = fake.call(c.download(4, dest))
            await during(down, ctl.expect(nk.FILE_DOWN_REQ))
            ctl.send(nk.file_down_ok(9, 0xBEEF, 100_000))
            dc = await during(down, fake.accept())
            await dc.raw(13)
            dc.send(b"z" * 40_000)
            await dc.w.drain()
            dc.close()
            exc = await raised(down, "download() must raise when the server closes before `size` bytes arrived", timeout=3)
            check(isinstance(exc, (OSError, EtherError)), f"download() raised {type(exc).__name__}: {exc}; expected "
                                                          "ConnectionError")
            check(not os.path.exists(dest), "download() failed but left the partial file at dest: delete it")


@atest
async def download_error_answer_raises():
    async with Fake() as fake:
        c, ev, ctl, _ = await connected(fake)
        with tmpdir() as d:
            dest = os.path.join(d, "out.bin")
            down = fake.call(c.download(77, dest))
            await during(down, ctl.expect(nk.FILE_DOWN_REQ))
            # ERROR 10 = unknown file. A pending download() should fail on ERROR 2 or 10.
            ctl.send(nk.error(10, "no such file"))
            done, _ = await asyncio.wait({down}, timeout=3)
            if not done:
                down.cancel()
                skip("download() was still waiting 3 s after ERROR 10 'no such file' "
                     "(route codes 2 and 10 to a pending download)")
            exc = await raised(down, "download() must not succeed after an ERROR answer")
            if isinstance(exc, EtherError):
                check(exc.code == 10, f"EtherError should carry code 10, got {exc.code}")
            check(not os.path.exists(dest), "download() failed but created dest")


# ---------------------------------------------------------------- UDP session

@atest
async def ping_gets_pong_event_with_rtt():
    async with Fake() as fake:
        c, ev, ctl, _ = await connected(fake)
        raw, addr, d = await fake.udp_wait(nk.U_PING, "PING", timeout=3.5, why="send a UDP PING about every 2 s")
        check(d.sid == SID, f"PING must carry the session id 0x9A1F0001, got 0x{d.sid:08X}")
        await asyncio.sleep(0.15)
        fake.udp.sendto(nk.pong(SID, d.body.t), addr)
        p = await ev.wait("Pong", why="a PONG echoing the PING's value becomes Pong(rtt_ms)")
        check(100 <= p.rtt_ms <= 2000, f"Pong.rtt_ms = {p.rtt_ms!r}, but the PONG came back ~150 ms after the PING: "
                                       "rtt_ms = now - echoed client_time_ms, both in milliseconds "
                                       "(time.monotonic_ns() // 1_000_000)")


@atest(limit=30)
async def ping_repeats_about_every_2_s():
    async with Fake() as fake:
        await connected(fake)
        loop = asyncio.get_running_loop()
        end, pings = loop.time() + 4.6, 0
        while loop.time() < end:
            try:
                raw, _ = await asyncio.wait_for(fake.dgrams.get(), end - loop.time())
            except TimeoutError:
                break
            fake.got_udp = True
            pings += raw[3:4] == bytes([nk.U_PING])
        if not pings:
            fake.missing("PING", 4.6, "send a UDP PING about every 2 s")
        check(2 <= pings <= 10, f"{pings} PING(s) in 4.6 s: send one about every 2 s")


@atest
async def typing_evt_becomes_typing_seen():
    async with Fake() as fake:
        c, ev, ctl, _ = await connected(fake)
        _, addr, _ = await fake.udp_wait(nk.U_UDP_HELLO, "UDP_HELLO")
        fake.udp.sendto(nk.typing_evt(SID, 1, "bob"), addr)
        e = await ev.wait("TypingSeen", why="a UDP TYPING_EVT from the server becomes a TypingSeen event")
        check(e == TypingSeen(1, "bob"), f"expected TypingSeen(1, 'bob'), got {e}")


@atest
async def udp_from_strangers_is_ignored():
    async with Fake() as fake:
        c, ev, ctl, _ = await connected(fake)
        _, addr, _ = await fake.udp_wait(nk.U_UDP_HELLO, "UDP_HELLO")
        stranger = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            stranger.bind(("127.0.0.2", 0))
            stranger.sendto(nk.typing_evt(SID, 1, "mallory"), ("127.0.0.1", addr[1]))
        except OSError as e:
            skip(f"can't send from 127.0.0.2 on this machine ({e})")
        finally:
            stranger.close()
        await asyncio.sleep(0.3)
        fake.udp.sendto(nk.typing_evt(SID, 1, "bob"), addr)
        await ev.wait("TypingSeen", lambda e: e.nick == "bob")
        check(not [e for e in ev.named("TypingSeen") if e.nick == "mallory"],
              "a TYPING_EVT from 127.0.0.2 (not the server) was shown: ignore datagrams that don't come from the "
              "server's address")


@atest(slow=True, limit=40)
async def udp_hello_repeats_every_10_s():
    async with Fake() as fake:
        await connected(fake)
        loop = asyncio.get_running_loop()
        end, hellos = loop.time() + 12.5, 0
        while loop.time() < end:
            try:
                raw, _ = await asyncio.wait_for(fake.dgrams.get(), end - loop.time())
            except TimeoutError:
                break
            fake.got_udp = True
            hellos += raw[3:4] == bytes([nk.U_UDP_HELLO])
        if not hellos:
            fake.missing("UDP_HELLO", 12.5)
        check(hellos >= 2, f"{hellos} UDP_HELLO(s) in 12.5 s: resend it every 10 s as a keepalive")


if __name__ == "__main__":
    nk.main("Ether net.py tests against a fake server", uses_host=False)
