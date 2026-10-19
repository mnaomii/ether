"""Right-hand chat dock: room tabs, message log (QTextBrowser), typing line, composer."""
from __future__ import annotations

import html
import os
import time
from dataclasses import dataclass, field
from datetime import datetime

from PyQt6.QtCore import QTimer, Qt, QUrl, pyqtSignal
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QTabBar,
                             QTextBrowser, QToolButton, QVBoxLayout, QWidget)

from .theme import MONO_FONT, UI_FONT, Palette
from .util import TYPING_SEND_INTERVAL, TYPING_TTL, human_size

MAX_ENTRIES = 500
GROUP_WINDOW_S = 300


@dataclass
class Entry:
    kind: str            # "msg" | "file" | "event"
    ts: float = 0.0      # unix seconds
    sender: str = ""
    text: str = ""       # message text / filename / event text
    file_id: int = 0
    size: int = 0
    error: bool = False


@dataclass
class Room:
    room_id: int
    name: str
    entries: list[Entry] = field(default_factory=list)
    unread: int = 0
    typing: dict[str, float] = field(default_factory=dict)  # nick -> expiry (monotonic)


def _esc(s: str) -> str:
    return html.escape(s).replace("\n", "<br>")


class ChatPanel(QWidget):
    send_requested = pyqtSignal(int, str)
    typing_activity = pyqtSignal(int)
    attach_requested = pyqtSignal(int)
    download_requested = pyqtSignal(int)
    join_requested = pyqtSignal(int)
    leave_requested = pyqtSignal(int)

    def __init__(self, pal: Palette, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("ChatPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.pal = pal
        self.self_nick = ""
        self.all_rooms: dict[int, str] = {}
        self.rooms: dict[int, Room] = {}
        self._connected = False
        self._last_typing_sent = 0.0

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        top = QHBoxLayout()
        top.setContentsMargins(10, 10, 10, 10)
        top.setSpacing(4)
        self.tabs = QTabBar()
        self.tabs.setDrawBase(False)
        self.tabs.setExpanding(False)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.tabs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tabs.customContextMenuRequested.connect(self._tab_menu)
        top.addWidget(self.tabs, 1)
        self.room_btn = QToolButton()
        self.room_btn.setText("Rooms ▾")
        self.room_btn.setToolTip("Join or leave rooms")
        self.room_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.room_menu = QMenu(self.room_btn)
        self.room_menu.aboutToShow.connect(self._build_room_menu)
        self.room_btn.setMenu(self.room_menu)
        top.addWidget(self.room_btn)
        root.addLayout(top)

        rule = QFrame()
        rule.setObjectName("Rule")
        rule.setFixedHeight(1)
        root.addWidget(rule)

        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.anchorClicked.connect(self._on_anchor)
        self.view.document().setDocumentMargin(16)
        root.addWidget(self.view, 1)

        self.typing_label = QLabel("")
        self.typing_label.setObjectName("Typing")
        self.typing_label.setFixedHeight(20)
        self.typing_label.setContentsMargins(18, 0, 18, 0)
        root.addWidget(self.typing_label)

        wrap = QHBoxLayout()
        wrap.setContentsMargins(12, 4, 12, 12)
        composer = QFrame()
        composer.setObjectName("Composer")
        cl = QHBoxLayout(composer)
        cl.setContentsMargins(8, 4, 4, 4)
        cl.setSpacing(4)
        self.input = QLineEdit()
        self.input.setMaxLength(4000)
        self.input.returnPressed.connect(self._send)
        self.input.textEdited.connect(self._typed)
        self.attach_btn = QToolButton()
        self.attach_btn.setText("Attach")
        self.attach_btn.setToolTip("Share a file in this room (max 50 MiB)")
        self.attach_btn.clicked.connect(self._attach)
        self.send_btn = QPushButton("Send")
        self.send_btn.setObjectName("Primary")
        self.send_btn.clicked.connect(self._send)
        cl.addWidget(self.input, 1)
        cl.addWidget(self.attach_btn)
        cl.addWidget(self.send_btn)
        wrap.addWidget(composer)
        root.addLayout(wrap)

        self._typing_timer = QTimer(self)
        self._typing_timer.setInterval(400)
        self._typing_timer.timeout.connect(self._refresh_typing)
        self._typing_timer.start()

        self._update_input()
        self._render()

    # ---- public API ----
    def set_palette(self, pal: Palette) -> None:
        self.pal = pal
        self._render()

    def set_connected(self, ok: bool) -> None:
        self._connected = ok
        self._update_input()

    def set_rooms(self, rooms: list[tuple[int, str]]) -> None:
        self.all_rooms = dict(rooms)

    def reset(self) -> None:
        self.tabs.blockSignals(True)
        while self.tabs.count():
            self.tabs.removeTab(0)
        self.tabs.blockSignals(False)
        self.rooms.clear()
        self.all_rooms.clear()
        self._connected = False
        self._update_input()
        self._render()

    def current_room(self) -> int | None:
        i = self.tabs.currentIndex()
        return self.tabs.tabData(i) if i >= 0 else None

    def joined_rooms(self) -> list[int]:
        return [self.tabs.tabData(i) for i in range(self.tabs.count())]

    def room_joined(self, rid: int) -> None:
        if rid in self.rooms:
            self.tabs.setCurrentIndex(self._tab_index(rid))
            return
        self.rooms[rid] = Room(rid, self.all_rooms.get(rid, f"room-{rid}"))
        i = self.tabs.addTab(f"#{self.rooms[rid].name}")
        self.tabs.setTabData(i, rid)
        self.tabs.setCurrentIndex(i)
        self._update_input()
        self._render(force_bottom=True)

    def room_left(self, rid: int) -> None:
        i = self._tab_index(rid)
        self.rooms.pop(rid, None)
        if i >= 0:
            self.tabs.removeTab(i)
        self._update_input()
        self._render(force_bottom=True)

    def add_history(self, rid: int, msgs: list[tuple[int, str, str]]) -> None:
        st = self.rooms.get(rid)
        if not st:
            return
        hist = [Entry("msg", ms / 1000, sender, text) for ms, sender, text in msgs]
        st.entries = hist + st.entries
        if rid == self.current_room():
            self._render(force_bottom=True)

    def add_message(self, rid: int, unix_ms: int, sender: str, text: str) -> None:
        st = self.rooms.get(rid)
        if st:
            st.typing.pop(sender, None)
        self._append(rid, Entry("msg", unix_ms / 1000, sender, text))

    def add_file(self, rid: int, file_id: int, filename: str, size: int, sender: str) -> None:
        self._append(rid, Entry("file", time.time(), sender, filename, file_id, size))

    def add_event(self, rid: int | None, text: str, error: bool = False) -> None:
        rid = self.current_room() if rid is None else rid
        if rid is not None:
            self._append(rid, Entry("event", time.time(), text=text, error=error), count_unread=False)

    def set_typing(self, rid: int, nick: str) -> None:
        st = self.rooms.get(rid)
        if st:
            st.typing[nick] = time.monotonic() + TYPING_TTL
            self._refresh_typing()

    # ---- internals ----
    def _tab_index(self, rid: int) -> int:
        for i in range(self.tabs.count()):
            if self.tabs.tabData(i) == rid:
                return i
        return -1

    def _append(self, rid: int, entry: Entry, count_unread: bool = True) -> None:
        st = self.rooms.get(rid)
        if not st:
            return
        st.entries.append(entry)
        del st.entries[:-MAX_ENTRIES]
        if rid == self.current_room():
            self._render()
        elif count_unread:
            st.unread += 1
            self._update_tab(rid)

    def _update_tab(self, rid: int) -> None:
        i, st = self._tab_index(rid), self.rooms.get(rid)
        if i >= 0 and st:
            self.tabs.setTabText(i, f"#{st.name}" + (f"  {st.unread}" if st.unread else ""))

    def _on_tab_changed(self, _i: int) -> None:
        rid = self.current_room()
        if rid in self.rooms:
            self.rooms[rid].unread = 0
            self._update_tab(rid)
        self._update_input()
        self._refresh_typing()
        self._render(force_bottom=True)

    def _update_input(self) -> None:
        rid = self.current_room()
        enabled = self._connected and rid is not None
        for w in (self.input, self.attach_btn, self.send_btn):
            w.setEnabled(enabled)
        if not self._connected:
            self.input.setPlaceholderText("Not connected")
        elif rid is None:
            self.input.setPlaceholderText("Join a room to start chatting")
        else:
            self.input.setPlaceholderText(f"Message #{self.rooms[rid].name}")
        self.room_btn.setEnabled(self._connected)

    def _render(self, force_bottom: bool = False) -> None:
        rid = self.current_room()
        st = self.rooms.get(rid) if rid is not None else None
        sb = self.view.verticalScrollBar()
        stick = force_bottom or sb.value() >= sb.maximum() - 8
        self.view.setHtml(self._html(st))
        if stick:
            sb.setValue(sb.maximum())
            QTimer.singleShot(0, lambda: sb.setValue(sb.maximum()))

    def _html(self, st: Room | None) -> str:
        p = self.pal
        out = [f"<html><body style=\"color:{p.text}; font-family:'{UI_FONT}'; font-size:10pt;\">"]
        if st is None:
            out.append(f'<p align="center" style="color:{p.muted}; margin-top:40px;">'
                       f'Pick a room from <b>Rooms</b> to start chatting.</p>')
        elif not st.entries:
            out.append(f'<p style="color:{p.muted};">No messages in #{_esc(st.name)} yet.</p>')
        prev_sender, prev_ts = None, 0.0
        for e in (st.entries if st else []):
            if e.kind == "event":
                color = p.danger if e.error else p.muted
                out.append(f'<p style="margin-top:10px; margin-bottom:0px; color:{color}; font-size:9pt;">'
                           f'{_esc(e.text)}</p>')
                prev_sender = None
                continue
            grouped = e.sender == prev_sender and e.ts - prev_ts < GROUP_WINDOW_S
            prev_sender, prev_ts = e.sender, e.ts
            if not grouped:
                who = p.accent if e.sender == self.self_nick else p.text
                hhmm = datetime.fromtimestamp(e.ts).strftime("%H:%M")
                out.append(f'<p style="margin-top:14px; margin-bottom:0px;">'
                           f'<span style="font-weight:600; color:{who};">{_esc(e.sender)}</span>'
                           f'&nbsp;&nbsp;<span style="color:{p.muted}; font-size:8.5pt;">{hhmm}</span></p>')
            if e.kind == "msg":
                out.append(f'<p style="margin-top:2px; margin-bottom:0px; line-height:140%;">{_esc(e.text)}</p>')
            else:
                ext = (os.path.splitext(e.text)[1][1:5] or "file").upper()
                out.append(
                    f'<table cellspacing="0" cellpadding="9" border="1" bgcolor="{p.surface}" '
                    f'style="margin-top:6px; border-style:solid; border-color:{p.border};"><tr><td>'
                    f'<span style="font-family:\'{MONO_FONT}\'; font-size:8pt; color:{p.muted};">{_esc(ext)}</span>'
                    f'&nbsp;&nbsp;&nbsp;<b>{_esc(e.text)}</b>'
                    f'&nbsp;&nbsp;<span style="color:{p.muted}; font-size:8.5pt;">{human_size(e.size)}</span>'
                    f'&nbsp;&nbsp;&nbsp;&nbsp;<a href="ether:download/{e.file_id}" '
                    f'style="color:{p.accent}; text-decoration:none; font-weight:600;">Download</a>'
                    f'</td></tr></table>')
        out.append("</body></html>")
        return "".join(out)

    def _refresh_typing(self) -> None:
        now = time.monotonic()
        for st in self.rooms.values():
            for nick in [n for n, dl in st.typing.items() if dl < now]:
                del st.typing[nick]
        rid = self.current_room()
        names = sorted(self.rooms[rid].typing) if rid in self.rooms else []
        if not names:
            text = ""
        elif len(names) == 1:
            text = f"{names[0]} is typing…"
        elif len(names) == 2:
            text = f"{names[0]} and {names[1]} are typing…"
        else:
            text = "Several people are typing…"
        self.typing_label.setText(text)

    def _send(self) -> None:
        rid, text = self.current_room(), self.input.text().strip()
        if rid is None or not text or not self._connected:
            return
        self.send_requested.emit(rid, text)
        self.input.clear()

    def _typed(self, _text: str) -> None:
        rid, now = self.current_room(), time.monotonic()
        if rid is not None and now - self._last_typing_sent >= TYPING_SEND_INTERVAL:
            self._last_typing_sent = now
            self.typing_activity.emit(rid)

    def _attach(self) -> None:
        rid = self.current_room()
        if rid is not None:
            self.attach_requested.emit(rid)

    def _on_anchor(self, url: QUrl) -> None:
        if url.scheme() == "ether" and url.path().startswith("download/"):
            try:
                self.download_requested.emit(int(url.path().split("/", 1)[1]))
            except ValueError:
                pass

    def _build_room_menu(self) -> None:
        m = self.room_menu
        m.clear()
        if not self.all_rooms:
            m.addAction("No rooms yet").setEnabled(False)
            return
        for rid, name in self.all_rooms.items():
            act = m.addAction(f"#{name}")
            act.setCheckable(True)
            act.setChecked(rid in self.rooms)
            act.triggered.connect(
                lambda checked, r=rid: (self.join_requested if checked else self.leave_requested).emit(r))

    def _tab_menu(self, pos) -> None:
        i = self.tabs.tabAt(pos)
        if i < 0:
            return
        rid = self.tabs.tabData(i)
        m = QMenu(self)
        m.addAction(f"Leave #{self.rooms[rid].name}").triggered.connect(lambda: self.leave_requested.emit(rid))
        m.exec(self.tabs.mapToGlobal(pos))
