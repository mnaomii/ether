"""proto.py against shared/vectors.txt. Run from client/: python -m pytest tests"""
from pathlib import Path

import pytest

from ether_core import events as ev
from ether_core import proto

SID, T_MS = 0x9A1F0001, 1_700_000_000_000


def load_vectors() -> dict[str, bytes]:
    path = Path(__file__).resolve().parents[2] / "shared" / "vectors.txt"
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or "|" not in line:
            continue
        name, hexbytes = line.split("|", 1)
        out[name.strip()] = bytes.fromhex(hexbytes)
    return out


V = load_vectors()


def split_frame(frame: bytes) -> tuple[int, bytes]:
    assert int.from_bytes(frame[:4], "big") == len(frame) - 4
    return frame[4], frame[5:]


ENCODE = {
    "tcp_login_ana": lambda: proto.encode_login("ana", "parola"),
    "tcp_join_1": lambda: proto.encode_join(1),
    "tcp_say_1_salut": lambda: proto.encode_say(1, "salut"),
    "tcp_say_2_buna": lambda: proto.encode_say(2, "bună"),
    "tcp_bye": proto.encode_bye,
    "udp_discover": proto.encode_discover,
    "udp_typing": lambda: proto.encode_typing(SID, 1),
    "udp_ping": lambda: proto.encode_ping(SID, T_MS),
}

DECODE_TCP = {
    "tcp_welcome": ev.Welcome(SID, [(1, "general"), (2, "misc")]),
    "tcp_msg": ev.MsgReceived(1, T_MS, "bob", "salut"),
    "tcp_error_bad_pass": ev.Error(7, "wrong password"),
}

DECODE_UDP = {
    "udp_discover_reply": ev.ServerInfo("ether-1", "", 5050, 3),
    "udp_pong": proto.PongEcho(T_MS),
    "udp_bad_magic": None,
}


@pytest.mark.parametrize("name", ENCODE)
def test_encode(name):
    assert ENCODE[name]() == V[name]


@pytest.mark.parametrize("name", DECODE_TCP)
def test_decode_tcp(name):
    assert proto.decode_tcp(*split_frame(V[name])) == DECODE_TCP[name]


@pytest.mark.parametrize("name", DECODE_UDP)
def test_decode_udp(name):
    assert proto.decode_udp(V[name]) == DECODE_UDP[name]


def test_decode_tcp_truncated_raises():
    msg_type, payload = split_frame(V["tcp_msg"])
    with pytest.raises(ValueError):
        proto.decode_tcp(msg_type, payload[:-1])


def test_decode_udp_truncated_is_none():
    assert proto.decode_udp(V["udp_pong"][:-1]) is None
