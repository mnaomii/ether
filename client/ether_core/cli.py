"""Tiny text client: debugging + second user in demos.

    python -m ether_core.cli [host] --nick ana [--port 5050]

No host: broadcast DISCOVER and use the first server that answers.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
from datetime import datetime

from . import events as ev
from .net import EtherClient, discover
from .proto import TCP_PORT

HELP = "commands: /join N  /leave N  /up PATH  /down FILE_ID DEST  /quit   (other text = say)"


def hhmm(unix_ms: int) -> str:
    return datetime.fromtimestamp(unix_ms / 1000).strftime("%H:%M")


def show(e) -> None:
    match e:
        case ev.Welcome():
            rooms = ", ".join(f"{rid}={name}" for rid, name in e.rooms)
            print(f"logged in, session {e.session_id:08X}. rooms: {rooms}")
        case ev.MsgReceived():
            print(f"[{e.room_id}] {hhmm(e.unix_ms)} {e.sender}: {e.text}")
        case ev.History():
            for m in e.messages:
                show(m)
        case ev.Users():
            print(f"[{e.room_id}] online: {', '.join(e.nicks)}")
        case ev.UserEvt():
            print(f"[{e.room_id}] {e.nick} {'joined' if e.joined else 'left'}")
        case ev.TypingSeen():
            print(f"[{e.room_id}] {e.nick} is typing...")
        case ev.FileEvt():
            print(f"[{e.room_id}] {e.sender} shared {e.filename} ({e.size} B): /down {e.file_id} DEST")
        case ev.Error():
            print(f"error {e.code}: {e.message}")
        case ev.Disconnected():
            print(f"disconnected {e.reason}".rstrip() + ". /quit to exit")
        # Pong: too chatty for a terminal


def progress(done: int, total: int) -> None:
    print(f"\r{done * 100 // max(total, 1)}%", end="", flush=True)


async def run(args, password: str) -> None:
    host, port = args.host, args.port
    if not host:
        servers = await discover()
        if not servers:
            print("no server answered DISCOVER; pass the host explicitly")
            return
        host, port = servers[0].host, servers[0].tcp_port
        print(f"found {servers[0].name} at {host}:{port}")

    client = EtherClient(on_event=show)
    try:
        await client.connect(host, port, args.nick, password)
    except ev.LoginError as e:
        print(f"login failed: {e} (error {e.code})")
        return
    print(HELP)

    room = None
    try:
        while True:
            line = (await asyncio.to_thread(input)).strip()  # keeps the loop free for incoming events
            if not line:
                continue
            if line == "/quit":
                break
            cmd, _, arg = line.partition(" ")
            try:
                if cmd == "/join":
                    room = int(arg)
                    await client.join(room)
                elif cmd == "/leave":
                    await client.leave(int(arg))
                elif cmd == "/down":
                    file_id, _, dest = arg.partition(" ")
                    await client.download(int(file_id), dest, progress=progress)
                    print()
                elif cmd.startswith("/") and cmd != "/up":
                    print(HELP)
                elif room is None:
                    print("join a room first: /join N")
                elif cmd == "/up":
                    await client.upload(room, arg, progress=progress)
                    print()
                else:
                    await client.say(room, line)
            except (ValueError, OSError, ev.EtherError) as e:
                print(f"! {e}")
    finally:
        await client.close()


def main() -> None:
    ap = argparse.ArgumentParser(prog="ether_core.cli", description="Ether text client")
    ap.add_argument("host", nargs="?", help="server address (default: discover on the LAN)")
    ap.add_argument("--port", type=int, default=TCP_PORT)
    ap.add_argument("--nick", required=True)
    args = ap.parse_args()
    password = getpass.getpass(f"password for {args.nick}: ")
    try:
        asyncio.run(run(args, password))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
