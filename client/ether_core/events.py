"""Events the core passes to on_event(...), plus the exceptions its calls raise.

ether_gui dispatches on the *class name*, so keep names and fields stable.
No networking here: the GUI's --demo mode imports this without a server.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ServerInfo:              # UDP DISCOVER_REPLY (host comes from the sender address)
    name: str
    host: str
    tcp_port: int
    users_online: int


@dataclass
class Welcome:                 # 0x02
    session_id: int
    rooms: list[tuple[int, str]]


@dataclass
class MsgReceived:             # 0x06  ("from" is a keyword, so: sender)
    room_id: int
    unix_ms: int
    sender: str
    text: str


@dataclass
class History:                 # 0x07
    room_id: int
    messages: list[MsgReceived] = field(default_factory=list)


@dataclass
class Users:                   # 0x08
    room_id: int
    nicks: list[str]


@dataclass
class UserEvt:                 # 0x09
    room_id: int
    joined: bool
    nick: str


@dataclass
class TypingSeen:              # UDP 0x05
    room_id: int
    nick: str


@dataclass
class FileEvt:                 # 0x12
    room_id: int
    file_id: int
    filename: str
    size: int
    sender: str


@dataclass
class Pong:                    # UDP 0x07, RTT already computed by the core
    rtt_ms: float


@dataclass
class Error:                   # 0x7E not tied to a pending call
    code: int
    message: str


@dataclass
class Disconnected:            # TCP closed / BYE
    reason: str = ""


Event = (Welcome | MsgReceived | History | Users | UserEvt | TypingSeen | FileEvt | Pong | Error
         | Disconnected)


class EtherError(Exception):
    """A call failed because the server answered with ERROR."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class LoginError(EtherError):
    """connect() got ERROR instead of WELCOME (codes 1, 7, 9)."""
