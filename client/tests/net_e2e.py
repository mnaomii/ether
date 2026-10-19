"""End to end: ether_core/net.py against the real server.

    python tests/net_e2e.py [--host 192.168.1.40] [-k NAME] [-v]

Needs both halves working. When something fails here, net_server.py and net_client.py point at
the guilty side more precisely. Skips everything if nothing listens on HOST:5050.
Nicknames come from a fixed pool (te00, te01, ...; password "pw-<nick>"), so repeated runs don't
keep adding accounts to the server's accounts.txt (MAX_ACCOUNTS is 256).
"""
from __future__ import annotations

import asyncio
import os
import secrets
import socket
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))       # client/, for ether_core

import netkit as nk                                                 # noqa: E402
from netkit import cfg, check, skip, test                           # noqa: E402

from ether_core import net                                          # noqa: E402
from ether_core.events import LoginError                            # noqa: E402


def etest(fn):
    test(lambda: nk.run_async(fn, 40), name=fn.__name__)
    return fn


_pool = iter(range(10_000))


def nick():
    return f"te{next(_pool):02d}"


async def within(coro, timeout, what):
    task = asyncio.ensure_future(coro)
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        task.cancel()
        raise AssertionError(f"{what} (still running after {timeout:g} s)")
    return task.result()


@asynccontextmanager
async def users(*labels):
    """One logged-in client per label: [(client, events, nick)], closed on exit."""
    out = []
    try:
        for _ in labels:
            ev, n = nk.Events(), nick()
            c = net.EtherClient(ev)
            out.append((c, ev, n))
            try:
                await within(c.connect(cfg.host, nk.TCP_PORT, n, "pw-" + n), 8, f"connect() as {n}")
            except (NotImplementedError, AssertionError):
                raise
            except Exception as e:
                raise AssertionError(f"connect() as {n} to {cfg.host}:{nk.TCP_PORT} raised {type(e).__name__}"
                                     f"{': ' + str(e) if str(e) else ''}. Does net_server.py pass?") from None
        yield out
    finally:
        for c, _, _ in out:
            try:
                await asyncio.wait_for(c.close(), 2)
            except BaseException:
                pass


async def join(c, ev, room, *others):
    n0 = len(ev.items)
    await within(c.join(room), 2, f"join({room})")
    await ev.wait("History", lambda e: e.room_id == room, since=n0, why=f"JOIN {room} -> HISTORY -> History event")
    return await ev.wait("Users", lambda e: e.room_id == room and set(others) <= set(e.nicks), since=n0,
                         why=f"JOIN {room} -> USERS listing {others}")


def probe():
    try:
        socket.create_connection((cfg.host, nk.TCP_PORT), timeout=min(cfg.t, 3)).close()
    except OSError as e:
        skip(f"no Ether server accepts TCP at {cfg.host}:{nk.TCP_PORT} ({e}). Start it, or pass --host / set ETHER_HOST.")


@etest
async def two_clients_chat():
    async with users("ana", "bob") as ((a, ea, an), (b, eb, bn)):
        await join(a, ea, 1, an)
        await join(b, eb, 1, an, bn)
        await ea.wait("UserEvt", lambda e: e.nick == bn and e.joined, why="ana sees bob join")
        text = "salut " + secrets.token_hex(3)
        await within(b.say(1, text), 2, "say()")
        for who, ev in ((an, ea), (bn, eb)):
            await ev.wait("MsgReceived", lambda e: e.text == text and e.sender == bn, why=f"{who} gets bob's message")
        await asyncio.sleep(0.3)
        mine = [e for e in eb.named("MsgReceived") if e.text == text]
        check(len(mine) == 1, f"the sender must see its own message exactly once (the server's echo), saw {len(mine)}")


@etest
async def history_reaches_a_later_joiner():
    async with users("ana", "cris") as ((a, ea, an), (c, ec, cn)):
        await join(a, ea, 2, an)
        text = "for later " + secrets.token_hex(3)
        await within(a.say(2, text), 2, "say()")
        await ea.wait("MsgReceived", lambda e: e.text == text)
        n0 = len(ec.items)
        await within(c.join(2), 2, "join(2)")
        h = await ec.wait("History", lambda e: e.room_id == 2, since=n0)
        check(h.messages and h.messages[-1].text == text and h.messages[-1].sender == an,
              f"History should end with ana's message {text!r}, got {[m.text for m in h.messages[-3:]]}. "
              "If the server suite passes, the client and server disagree on the HISTORY entry format")


@etest
async def typing_is_seen_by_the_other_client():
    async with users("ana", "bob") as ((a, ea, an), (b, eb, bn)):
        await join(a, ea, 3, an)
        await join(b, eb, 3, an, bn)
        await asyncio.sleep(0.3)                     # both UDP_HELLOs processed
        await within(a.typing(3), 1, "typing()")
        await eb.wait("TypingSeen", lambda e: e.nick == an and e.room_id == 3, timeout=3,
                      why="UDP TYPING -> server -> TYPING_EVT to bob's UDP address (from his UDP_HELLO)")


@etest
async def pong_arrives():
    async with users("ana") as ((a, ea, an),):
        p = await ea.wait("Pong", timeout=5, why="PING every ~2 s, the server echoes PONG, the core emits Pong(rtt_ms)")
        check(0 <= p.rtt_ms < 2000, f"Pong.rtt_ms = {p.rtt_ms!r} looks wrong (milliseconds, server on the LAN)")


@etest
async def upload_then_download():
    async with users("ana", "bob") as ((a, ea, an), (b, eb, bn)):
        await join(a, ea, 3, an)
        await join(b, eb, 3, an, bn)
        with tempfile.TemporaryDirectory(prefix="ether-e2e-", ignore_cleanup_errors=True) as d:
            content = os.urandom(250_000)
            name = f"notes-{secrets.token_hex(3)}.bin"
            Path(d, name).write_bytes(content)
            prog = []
            await within(a.upload(3, os.path.join(d, name), progress=lambda x, t: prog.append((x, t))), 15, "upload()")
            check(prog and prog[-1] == (len(content), len(content)), f"upload progress should end at (size, size): {prog[-2:]}")
            fa = await ea.wait("FileEvt", lambda e: e.filename == name, why="the uploader gets FILE_EVT too")
            fb = await eb.wait("FileEvt", lambda e: e.filename == name)
            check(fa == fb and fa.size == len(content) and fa.sender == an, f"FileEvt mismatch: {fa} vs {fb}")
            dest = os.path.join(d, "copy.bin")
            await within(b.download(fb.file_id, dest), 15, "download()")
            check(Path(dest).read_bytes() == content, "the downloaded file differs from the uploaded one")


@etest
async def wrong_password_raises_login_error():
    n = nick()
    c = net.EtherClient(nk.Events())
    await within(c.connect(cfg.host, nk.TCP_PORT, n, "pw-" + n), 8, "connect()")
    await within(c.close(), 2, "close()")
    for _ in range(20):                               # the server may not have dropped the first session yet
        c2 = net.EtherClient(nk.Events())
        try:
            await within(c2.connect(cfg.host, nk.TCP_PORT, n, "wrong"), 8, "connect() with a wrong password")
        except LoginError as e:
            if e.code == 1:
                await asyncio.sleep(0.1)
                continue
            check(e.code == 7 and str(e), f"expected LoginError 7 with the server's message, got code {e.code}: {e}")
            return
        await c2.close()
        raise AssertionError("connect() with a wrong password succeeded")
    raise AssertionError("still 'already online' 2 s after the first session closed")


@etest
async def leave_and_close_are_seen_by_others():
    async with users("ana", "bob") as ((a, ea, an), (b, eb, bn)):
        await join(a, ea, 1, an)
        await join(a, ea, 2, an)
        await join(b, eb, 1, an, bn)
        await join(b, eb, 2, an, bn)
        await within(b.leave(1), 2, "leave(1)")
        await ea.wait("UserEvt", lambda e: e.room_id == 1 and e.nick == bn and not e.joined, why="LEAVE -> USER_EVT left")
        n0 = len(eb.items)
        await within(b.close(), 2, "close()")
        await ea.wait("UserEvt", lambda e: e.room_id == 2 and e.nick == bn and not e.joined,
                      why="bob's close -> server drop_client -> USER_EVT left to his rooms")
        await eb.none("Disconnected", since=n0, why="no Disconnected after the app's own close()")


if __name__ == "__main__":
    nk.main("Ether end-to-end tests (net.py + server at {host}:5050)", before=probe)
