"""In-process fake of ether_core so the GUI runs with no server: `python -m ether_gui --demo`.

Implements the same async API as ether_core.net and emits
contract.py events, scripted to show typing, joins, files and pings.
"""
from __future__ import annotations

import asyncio
import os
import random
import time

from .contract import (FileEvt, History, LoginError, MsgReceived, Pong, ServerInfo, TypingSeen,
                       UserEvt, Users, Welcome)


def _now_ms() -> int:
    return int(time.time() * 1000)


async def mock_discover(timeout: float = 1.0) -> list[ServerInfo]:
    await asyncio.sleep(min(timeout, 0.6))
    return [ServerInfo("ether-1", "192.168.1.40", 5050, 3),
            ServerInfo("ana-vm-test", "192.168.1.77", 5050, 0)]


class MockClient:
    ROOMS = [(1, "general"), (2, "misc"), (3, "memes")]
    LINES = ["cine mai e online?", "am pus vectorii de test în repo", "poll() merge, fără thread-uri",
             "wireshark vede DISCOVER-ul", "pornesc încă o instanță ca bob", "ok, testez acum"]

    def __init__(self, on_event):
        self.on_event = on_event
        self.nick = ""
        self.session_id = 0
        self.udp_port = 0
        self.joined: set[int] = set()
        self.members = {1: {"bob"}, 2: {"bob", "cris"}, 3: {"dana"}}
        t = _now_ms()
        self.history = {
            1: [MsgReceived(1, t - 900_000, "bob", "cine a pornit serverul pe VM?"),
                MsgReceived(1, t - 860_000, "bob", "îl văd pe 192.168.1.40")],
            2: [MsgReceived(2, t - 3_600_000, "cris", "am pus build-ul nou pe server")],
            3: [],
        }
        self.files: dict[int, int] = {}
        self._next_file = 3
        self._tasks: list[asyncio.Task] = []

    async def connect(self, host: str, port: int, nick: str, password: str) -> None:
        await asyncio.sleep(0.3)
        if nick.lower() in {"bob", "cris", "dana"}:
            raise LoginError(1, f"'{nick}' is already online")
        if password == "wrong":
            raise LoginError(7, "wrong password")
        self.nick = nick
        self.session_id = random.getrandbits(32)
        self.udp_port = random.randint(50000, 60000)
        self.on_event(Welcome(self.session_id, list(self.ROOMS)))
        self._tasks = [asyncio.ensure_future(self._ping_loop()), asyncio.ensure_future(self._script())]

    async def join(self, room_id: int) -> None:
        await asyncio.sleep(0.05)
        self.joined.add(room_id)
        self.members[room_id].add(self.nick)
        self.on_event(History(room_id, list(self.history[room_id][-50:])))
        self.on_event(Users(room_id, sorted(self.members[room_id])))

    async def leave(self, room_id: int) -> None:
        self.joined.discard(room_id)
        self.members[room_id].discard(self.nick)

    async def say(self, room_id: int, text: str) -> None:
        await asyncio.sleep(0.03)
        self._msg(room_id, self.nick, text)

    async def typing(self, room_id: int) -> None:
        pass

    async def upload(self, room_id: int, path: str, progress=None) -> None:
        size = os.path.getsize(path)
        await self._fake_stream(size, progress)
        self._file(room_id, self.nick, os.path.basename(path), size)

    async def download(self, file_id: int, dest: str, progress=None) -> None:
        # Demo only: simulates progress, writes nothing to dest.
        await self._fake_stream(self.files.get(file_id, 2_000_000), progress)

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()

    # ---- simulation ----
    async def _fake_stream(self, size: int, progress) -> None:
        done, step = 0, max(size // 30, 1)
        while done < size:
            await asyncio.sleep(0.07)
            done = min(size, done + step)
            if progress:
                progress(done, size)

    def _msg(self, rid: int, sender: str, text: str) -> None:
        m = MsgReceived(rid, _now_ms(), sender, text)
        self.history[rid].append(m)
        if rid in self.joined:
            self.on_event(m)

    def _typing(self, rid: int, nick: str) -> None:
        if rid in self.joined:
            self.on_event(TypingSeen(rid, nick))

    def _member(self, rid: int, nick: str, joined: bool) -> None:
        (self.members[rid].add if joined else self.members[rid].discard)(nick)
        if rid in self.joined:
            self.on_event(UserEvt(rid, joined, nick))

    def _file(self, rid: int, sender: str, name: str, size: int) -> None:
        fid = self._next_file
        self._next_file += 1
        self.files[fid] = size
        if rid in self.joined:
            self.on_event(FileEvt(rid, fid, name, size, sender))

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(2.0)
            self.on_event(Pong(random.uniform(1.5, 6.0)))

    async def _type_then_say(self, rid: int, nick: str, text: str, beats: int = 2) -> None:
        for _ in range(beats):
            self._typing(rid, nick)
            await asyncio.sleep(1.0)
        self._msg(rid, nick, text)

    async def _script(self) -> None:
        await asyncio.sleep(2.0)
        await self._type_then_say(1, "bob", "salut! merge serverul?", 3)
        await asyncio.sleep(3.0)
        self._member(1, "cris", True)
        await asyncio.sleep(2.0)
        await self._type_then_say(1, "cris", "merge, am testat framing-ul trimițând câte 1 byte")
        await asyncio.sleep(3.0)
        self._file(1, "bob", "notes.pdf", 2_097_152)
        await asyncio.sleep(4.0)
        await self._type_then_say(2, "cris", "cine face dissector-ul lua?")
        while True:
            await asyncio.sleep(random.uniform(6, 11))
            if random.random() < 0.2:
                self._member(1, "dana", "dana" not in self.members[1])
                continue
            rooms = sorted(self.joined) or [1]
            rid = random.choice(rooms)
            others = sorted(self.members[rid] - {self.nick})
            if others:
                await self._type_then_say(rid, random.choice(others), random.choice(self.LINES),
                                          random.randint(1, 3))
