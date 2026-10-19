"""asyncio network client: TCP control connection, UDP endpoint, file data connections.

Everything runs on the event loop that ether_gui shares with Qt (qasync), so never block.
on_event is called on that loop with events.* dataclasses.
"""
from __future__ import annotations

from typing import Callable

from .events import Event, ServerInfo, Welcome

Progress = Callable[[int, int], None]      # progress(done_bytes, total_bytes)


async def discover(timeout: float = 1.0) -> list[ServerInfo]:
    """Broadcast one DISCOVER on UDP_PORT and collect replies for `timeout` seconds.
    The GUI calls this again every ~1 s, so one broadcast per call is enough."""


    
    raise NotImplementedError


class EtherClient:
    def __init__(self, on_event: Callable[[Event], None]):
        self.on_event = on_event
        self.session_id = 0                 # set by connect(); shown in the GUI status bar
        self.udp_port = 0                   # local UDP port (bound to 0 = OS picks); status bar

    async def connect(self, host: str, port: int, nickname: str, password: str) -> Welcome:
        """Log in over TCP and set up the UDP endpoint (UDP_HELLO, resent every 10 s).
        Emits the Welcome event and returns it. Raises LoginError if the server answers ERROR,
        OSError if unreachable. From then on: incoming frames -> on_event, a periodic
        PING whose PONG becomes Pong(rtt_ms), and Disconnected(reason) when the connection drops."""
        raise NotImplementedError

    async def close(self) -> None:
        """Send BYE, close every socket, stop background tasks. Safe to call twice."""
        raise NotImplementedError

    async def join(self, room_id: int) -> None:
        """Server answers with History then Users events."""
        raise NotImplementedError

    async def leave(self, room_id: int) -> None:
        raise NotImplementedError

    async def say(self, room_id: int, text: str) -> None:
        """Our own message comes back as a MsgReceived event (server orders all messages)."""
        raise NotImplementedError

    async def typing(self, room_id: int) -> None:
        """UDP TYPING. The GUI already throttles to 1/s."""
        raise NotImplementedError

    async def upload(self, room_id: int, path: str, progress: Progress | None = None) -> None:
        """FILE_UP_REQ -> FILE_UP_OK, then stream the file on a new data connection.
        Returns when every byte is sent. Raises EtherError if the server answers ERROR
        (FILE_UP_OK carries no request id: replies arrive in request order)."""
        raise NotImplementedError

    async def download(self, file_id: int, dest: str, progress: Progress | None = None) -> None:
        """FILE_DOWN_REQ -> FILE_DOWN_OK, then receive `size` bytes into dest on a new data connection.
        Raises EtherError if the server answers ERROR."""
        raise NotImplementedError
