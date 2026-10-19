"""Wire protocol: constants + pure encode/decode, no I/O.

All integers big-endian. Strings are [u16 byte_len][UTF-8 bytes].
Keep constants in sync with server/src/proto.h. Tested against shared/vectors.txt.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from .events import Event, ServerInfo

TCP_PORT = 5050
UDP_PORT = 5051
UDP_MAGIC = 0x4554
UDP_VERSION = 1
UDP_HEADER_LEN = 8               # u16 magic, u8 ver, u8 type, u32 session_id

MAX_FRAME = 64 * 1024            # u32 length field, includes the type byte
MAX_NICK = 31                    # bytes
MAX_PASS = 64                    # bytes
MAX_TEXT = 4000                  # bytes per chat message
MAX_FILE = 50 * 1024 * 1024


class T(IntEnum):                # TCP message types
    LOGIN = 0x01
    WELCOME = 0x02
    JOIN = 0x03
    LEAVE = 0x04
    SAY = 0x05
    MSG = 0x06
    HISTORY = 0x07
    USERS = 0x08
    USER_EVT = 0x09
    FILE_UP_REQ = 0x10
    FILE_UP_OK = 0x11
    FILE_EVT = 0x12
    FILE_DOWN_REQ = 0x13
    FILE_DOWN_OK = 0x14
    DATA_HELLO = 0x15
    ERROR = 0x7E
    BYE = 0x7F


class U(IntEnum):                # UDP datagram types
    DISCOVER = 0x01
    DISCOVER_REPLY = 0x02
    UDP_HELLO = 0x03
    TYPING = 0x04
    TYPING_EVT = 0x05
    PING = 0x06
    PONG = 0x07


class Err(IntEnum):
    NICK_ONLINE = 1
    BAD_ROOM = 2
    FILE_TOO_LARGE = 3
    BAD_TOKEN = 4
    FRAME_TOO_LARGE = 5
    NOT_LOGGED_IN = 6
    BAD_PASSWORD = 7
    TEXT_TOO_LONG = 8
    BAD_CREDENTIALS = 9
    NO_SUCH_FILE = 10


# ---- wire-only messages: consumed by net.py, never shown to the GUI ----

@dataclass
class FileUpOk:                  # 0x11
    transfer_id: int
    token: int


@dataclass
class FileDownOk:                # 0x14
    transfer_id: int
    token: int
    size: int


@dataclass
class PongEcho:                  # UDP 0x07, before net.py turns it into events.Pong(rtt_ms)
    client_time_ms: int


@dataclass
class Bye:                       # 0x7F
    pass


# ---- TCP, client -> server: each returns a complete frame [u32 length][u8 type][payload] ----

def encode_login(nickname: str, password: str) -> bytes:
    raise NotImplementedError


def encode_join(room_id: int) -> bytes:
    raise NotImplementedError


def encode_leave(room_id: int) -> bytes:
    raise NotImplementedError


def encode_say(room_id: int, text: str) -> bytes:
    raise NotImplementedError


def encode_file_up_req(room_id: int, filename: str, size: int) -> bytes:
    raise NotImplementedError


def encode_file_down_req(file_id: int) -> bytes:
    raise NotImplementedError


def encode_data_hello(transfer_id: int, token: int) -> bytes:
    raise NotImplementedError


def encode_bye() -> bytes:
    raise NotImplementedError


# ---- TCP, server -> client ----

def decode_tcp(msg_type: int, payload: bytes) -> Event | FileUpOk | FileDownOk | Bye:
    """Decode one frame body (type byte and payload, length prefix already stripped).
    ERROR -> events.Error. Raises ValueError on an unknown type or truncated payload."""
    raise NotImplementedError


# ---- UDP: each returns a complete datagram [magic][ver][type][session_id][payload] ----

def encode_discover() -> bytes:
    raise NotImplementedError


def encode_udp_hello(session_id: int) -> bytes:
    raise NotImplementedError


def encode_typing(session_id: int, room_id: int) -> bytes:
    raise NotImplementedError


def encode_ping(session_id: int, client_time_ms: int) -> bytes:
    raise NotImplementedError


def decode_udp(datagram: bytes) -> ServerInfo | Event | PongEcho | None:
    """DISCOVER_REPLY -> ServerInfo with host="" (net.py fills it from the sender address).
    Returns None for a wrong magic/version, an unknown type, or a truncated datagram."""
    raise NotImplementedError
