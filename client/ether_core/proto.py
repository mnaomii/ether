"""Wire protocol: constants + pure encode/decode, no I/O.

All integers big-endian. Strings are [u16 byte_len][UTF-8 bytes].
Keep constants in sync with server/src/proto.h. Tested against shared/vectors.txt.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import struct

from .events import Event, ServerInfo, TypingSeen, Welcome, MsgReceived, History, UserEvt, Users, FileEvt, Error

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


# [len][str]
def _str(s: str) -> bytes:
    b = s.encode();
    return struct.pack('!H', len(b)) + b

# [len][type][payload]
def _frame(t: int, payload: bytes = b"") -> bytes:
    return struct.pack('!IB', len(payload)+1, t )+ payload

# [magic][ver][type][sid][payload]
def _udp(t: int, sid: int, payload: bytes = b"") -> bytes:
    return struct.pack('!HBBI',UDP_MAGIC, UDP_VERSION,t,sid)  + payload


# [room_id][time][sender][text]
def _msg(r: _R) -> MsgReceived:
    # python evaluates args left to right = wire order (used by MSG and every HISTORY entry)
    return MsgReceived(r.num('!H'), r.num('!Q'),  r.text(), r.text())

class _R: # like rbuf in C
    def __init__(self, b:bytes):
        self.b = b
        self.o = 0

    def num(self, fmt: str) -> int:
        (v,) = struct.unpack_from(fmt, self.b, self.o)  # too short -> struct.error
        self.o += struct.calcsize(fmt)                  # move by the field's SIZE, not its value

        return v
    
    def text(self) -> str:
        n = self.num('!H')
        if self.o + n > len(self.b):  # slicing never raises (b"abc"[1:99] = b"bc"), so check it ourselves
            raise ValueError()
        s = self.b[self.o:self.o+n].decode()
        self.o += n
        return s

# ---- TCP, client -> server: each returns a complete frame [u32 length][u8 type][payload] ----

def encode_login(nickname: str, password: str) -> bytes:
    return _frame(T.LOGIN, _str(nickname) + _str(password))

def encode_join(room_id: int) -> bytes:
    return _frame(T.JOIN, struct.pack('!H',room_id))


def encode_leave(room_id: int) -> bytes:
    return _frame(T.LEAVE, struct.pack('!H', room_id))


def encode_say(room_id: int, text: str) -> bytes:
    return _frame(T.SAY, struct.pack('!H',room_id) + _str(text))


def encode_file_up_req(room_id: int, filename: str, size: int) -> bytes:
    return _frame(T.FILE_UP_REQ, struct.pack('!H',room_id) + _str(filename) + struct.pack('!Q',size))

def encode_file_down_req(file_id: int) -> bytes:
    return _frame(T.FILE_DOWN_REQ, struct.pack('!I', file_id))

def encode_data_hello(transfer_id: int, token: int) -> bytes:
    return _frame(T.DATA_HELLO, struct.pack('!II', transfer_id, token))

def encode_bye() -> bytes:
    return _frame(T.BYE)

# ---- TCP, server -> client ----

def decode_tcp(msg_type: int, payload: bytes) -> Event | FileUpOk | FileDownOk | Bye:
    """Decode one frame body (type byte and payload, length prefix already stripped).
    ERROR -> events.Error. Raises ValueError on an unknown type or truncated payload."""

    rbuf = _R(payload)
    try:
        match msg_type:

            case T.MSG:
                return _msg(rbuf)
            
            case T.ERROR:
                code = rbuf.num('!H')
                return Error(code, rbuf.text())
            
            case T.WELCOME:
                sid = rbuf.num('!I')
                n = rbuf.num('!H')
                vec = []
                for _ in range(0,n):
                    vec.append((rbuf.num('!H'), rbuf.text()))

                return Welcome(sid, vec)

            case T.HISTORY:
                room = rbuf.num('!H')
                n = rbuf.num('!H')
                vec = []
                for _ in range(0,n):
                    vec.append(_msg(rbuf))

                return History(room, vec)
                    
            case T.USERS:
                room = rbuf.num('!H')
                n = rbuf.num('!H')
                vec = []
                for _ in range(0,n):
                    vec.append(rbuf.text())

                return Users(room, vec)

            case T.USER_EVT:
                room = rbuf.num('!H')
                joined = rbuf.num('!B')
                nick = rbuf.text()

                return UserEvt(room, bool(joined), nick)

            case T.FILE_EVT:
                room = rbuf.num('!H')
                id = rbuf.num('!I')
                name = rbuf.text()
                size = rbuf.num('!Q')
                frm = rbuf.text()
            
                return FileEvt(room, id, name, size , frm)
            
            case T.FILE_UP_OK:
                xfer_id = rbuf.num('!I')
                token = rbuf.num('!I')

                return  FileUpOk(xfer_id,token)

            case T.FILE_DOWN_OK:
                xfer_id = rbuf.num('!I')
                token = rbuf.num('!I')
                size = rbuf.num('!Q')

                return FileDownOk(xfer_id,token,size)
            
            case T.BYE:
                return Bye()

            case _:
                raise ValueError(f"unknown frame type {msg_type:#x}")
    except struct.error as e:  # turn it into ValueError: callers catch one kind of error
        raise ValueError(f"truncated payload: {e}")



# ---- UDP: each returns a complete datagram [magic][ver][type][session_id][payload] ----

def encode_discover() -> bytes:
    return _udp(U.DISCOVER, 0)


def encode_udp_hello(session_id: int) -> bytes:
    return _udp(U.UDP_HELLO, session_id)

def encode_typing(session_id: int, room_id: int) -> bytes:
    return _udp(U.TYPING, session_id, struct.pack('!H', room_id))

def encode_ping(session_id: int, client_time_ms: int) -> bytes:
    return _udp(U.PING, session_id, struct.pack('!Q', client_time_ms))

def decode_udp(datagram: bytes) -> ServerInfo | Event | PongEcho | None:
    """DISCOVER_REPLY -> ServerInfo with host="" (net.py fills it from the sender address).
    Returns None for a wrong magic/version, an unknown type, or a truncated datagram."""


    try:
        rbuf = _R(datagram)

        magic = rbuf.num('!H')
        ver   = rbuf.num('!B')
        type  = rbuf.num('!B')
        sid   = rbuf.num('!I')

        if magic != UDP_MAGIC or ver != UDP_VERSION:
            return None

        match type:
            case U.DISCOVER_REPLY:
                name = rbuf.text()
                port = rbuf.num('!H')
                users = rbuf.num('!H')
                return ServerInfo(name, "", port, users)  # host "": net.py fills it from the sender's ip
            
            case U.TYPING_EVT:
                room = rbuf.num('!H')
                nick = rbuf.text()
                return TypingSeen(room,nick)
            
            case U.PONG:
                time = rbuf.num('!Q')
                return PongEcho(time)
            case _:
                return None


    except (struct.error, ValueError):  # never raises: UDP gets junk from anyone on the LAN
        return None