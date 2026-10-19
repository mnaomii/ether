"""The shapes the GUI expects from ether_core.

Events and exceptions live in ether_core/events.py (pure dataclasses, no
networking, so --demo still works). MainWindow.handle_event() dispatches on
the *class name* of each event. GUI-only types stay here.
"""
from __future__ import annotations

from dataclasses import dataclass

from ether_core.events import (Disconnected, Error, FileEvt, History, EtherError,  # noqa: F401
                                 LoginError, MsgReceived, Pong, ServerInfo, TypingSeen, UserEvt,
                                 Users, Welcome)


@dataclass
class ConnectTarget:           # what the discovery dialog returns
    host: str
    port: int
    nick: str
    password: str
    server_name: str


ERROR_TEXT = {
    1: "Nickname already online",
    2: "Unknown room",
    3: "File too large",
    4: "Transfer token rejected",
    5: "Frame too large",
    6: "Not logged in",
    7: "Wrong password",
    8: "Message too long",
    9: "Invalid nickname or password",
    10: "No such file",
}
