"""Main window: top bar, network map (left), chat dock (right), status bar.

All network calls go through self.client (ether_core.EtherClient or the mock).
Events from the core arrive in handle_event() on the Qt/asyncio thread.
"""
from __future__ import annotations

import os
import time
from typing import Awaitable, Callable

from PyQt6.QtCore import QRectF, QStandardPaths, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QKeySequence, QPainter, QShortcut
from PyQt6.QtWidgets import (QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow,
                             QSplitter, QToolButton, QVBoxLayout, QWidget)

from . import theme
from .chat_panel import ChatPanel
from .contract import ERROR_TEXT, ConnectTarget
from .map_view import NetworkMapView
from .theme import Palette
from .util import MAX_FILE_BYTES, human_size, log, spawn


class PingBars(QWidget):
    def __init__(self):
        super().__init__()
        self.rtt: float | None = None
        self.pal: Palette | None = None
        self.setFixedSize(15, 14)

    def paintEvent(self, e) -> None:
        if not self.pal:
            return
        p = QPainter(self)
        if self.rtt is None:
            lit, color = 0, self.pal.border
        elif self.rtt < 20:
            lit, color = 3, self.pal.ok
        elif self.rtt < 80:
            lit, color = 2, self.pal.udp
        else:
            lit, color = 1, self.pal.danger
        for i, h in enumerate((5, 9, 14)):
            p.fillRect(QRectF(i * 5, 14 - h, 3, h), QColor(color if i < lit else self.pal.border))


class TopBar(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("TopBar")
        h = QHBoxLayout(self)
        h.setContentsMargins(16, 8, 10, 8)
        h.setSpacing(12)
        brand = QLabel("Ether")
        brand.setObjectName("Brand")
        self.chip = QLabel("not connected")
        self.chip.setObjectName("Chip")
        h.addWidget(brand)
        h.addWidget(self.chip)
        h.addStretch(1)
        self.bars = PingBars()
        self.ping = QLabel("— ms")
        self.ping.setProperty("mono", True)
        self.ping.setToolTip("UDP round-trip time to the server (PING / PONG)")
        self.theme_btn = QToolButton()
        h.addWidget(self.bars)
        h.addWidget(self.ping)
        h.addSpacing(4)
        h.addWidget(self.theme_btn)

    def set_ping(self, rtt: float | None) -> None:
        self.bars.rtt = rtt
        self.bars.update()
        self.ping.setText("— ms" if rtt is None else f"{rtt:.0f} ms")

    def set_palette(self, pal: Palette) -> None:
        self.bars.pal = pal
        self.bars.update()
        self.theme_btn.setText("☀" if pal.dark else "☾")
        self.theme_btn.setToolTip(f"{'Light' if pal.dark else 'Dark'} mode  (Ctrl+Shift+L)")


class MainWindow(QMainWindow):
    closed = pyqtSignal()

    def __init__(self, settings, pal: Palette):
        super().__init__()
        self.settings, self.pal = settings, pal
        self.client = None
        self.target: ConnectTarget | None = None
        self.nick = ""
        self.rooms: dict[int, str] = {}
        self.room_users: dict[int, set[str]] = {}
        self.files: dict[int, object] = {}
        self._xfers: set[str] = set()
        self._conn_state = "idle"

        self.setWindowTitle("Ether")
        self.resize(1120, 700)
        geo = settings.value("win/geometry")
        if geo is not None:
            self.restoreGeometry(geo)

        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.topbar = TopBar()
        v.addWidget(self.topbar)
        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.setHandleWidth(1)
        self.split.setChildrenCollapsible(False)
        self.map = NetworkMapView(pal)
        self.chat = ChatPanel(pal)
        self.chat.setMinimumWidth(320)
        self.split.addWidget(self.map)
        self.split.addWidget(self.chat)
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 0)
        self.split.setSizes([720, 400])
        state = settings.value("win/split")
        if state is not None:
            self.split.restoreState(state)
        v.addWidget(self.split, 1)
        self.setCentralWidget(central)

        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        self.conn_label = QLabel()
        self.conn_label.setTextFormat(Qt.TextFormat.RichText)
        self.tcp_label, self.udp_label, self.session_label = QLabel(), QLabel(), QLabel()
        self.xfer_label = QLabel()
        for w in (self.conn_label, self.tcp_label, self.udp_label, self.session_label):
            sb.addWidget(w)
        sb.addPermanentWidget(self.xfer_label)

        self.chat.send_requested.connect(self._say)
        self.chat.typing_activity.connect(lambda rid: self._net(self.client.typing(rid)))
        self.chat.join_requested.connect(lambda rid: spawn(self._join(rid), on_error=self._net_error))
        self.chat.leave_requested.connect(lambda rid: spawn(self._leave(rid), on_error=self._net_error))
        self.chat.attach_requested.connect(self._upload)
        self.chat.download_requested.connect(self._download)
        self.topbar.theme_btn.clicked.connect(self.toggle_theme)
        QShortcut(QKeySequence("Ctrl+Shift+L"), self).activated.connect(self.toggle_theme)

        self._handlers: dict[str, Callable] = {
            "Welcome": self._on_welcome, "MsgReceived": self._on_msg, "History": self._on_history,
            "Users": self._on_users, "UserEvt": self._on_user_evt, "TypingSeen": self._on_typing,
            "FileEvt": self._on_file, "Pong": self._on_pong, "Error": self._on_error,
            "Disconnected": self._on_disconnected,
        }
        self._apply_palette_local()
        self._set_conn("idle")

    # ---- lifecycle ----
    def attach(self, client, target: ConnectTarget) -> None:
        self.client, self.target, self.nick = client, target, target.nick
        self.chat.self_nick = target.nick
        self.map.set_server(target.server_name, target.host, target.port)
        self.map.set_self(target.nick)
        self.topbar.chip.setText(f"{target.server_name} · {target.host}")
        self.setWindowTitle(f"Ether — {target.server_name} ({target.host})")
        self.tcp_label.setText(f"TCP {target.host}:{target.port}")
        self._set_conn("connecting")

    def detach(self) -> None:
        self.client = None
        self.rooms.clear()
        self.room_users.clear()
        self.files.clear()
        self.map.clear()
        self.chat.reset()
        self.topbar.set_ping(None)
        self._set_conn("idle")

    def on_connected(self) -> None:
        port = getattr(self.client, "udp_port", None)
        self.udp_label.setText(f"UDP :{port}" if port else "UDP")
        self.chat.set_connected(True)
        self.map.set_connected(True)
        self._set_conn("ok")

    def handle_event(self, ev) -> None:
        fn = self._handlers.get(type(ev).__name__)
        if fn:
            fn(ev)
        else:
            log.debug("unhandled event %r", ev)

    def toggle_theme(self) -> None:
        self.pal = theme.LIGHT if self.pal.dark else theme.DARK
        theme.apply(QApplication.instance(), self.pal)
        self.settings.setValue("theme", self.pal.name)
        self._apply_palette_local()

    def closeEvent(self, e) -> None:
        self.settings.setValue("win/geometry", self.saveGeometry())
        self.settings.setValue("win/split", self.split.saveState())
        self.closed.emit()
        super().closeEvent(e)

    # ---- core events ----
    def _on_welcome(self, ev) -> None:
        self.rooms = dict(ev.rooms)
        self.chat.set_rooms(list(ev.rooms))
        self.session_label.setText(f"session {ev.session_id:08X}")
        saved = str(self.settings.value(self._rooms_key(), "") or "")
        by_name = {name: rid for rid, name in self.rooms.items()}
        wanted = [by_name[n] for n in saved.split(",") if n in by_name] or list(self.rooms)[:1]
        for rid in wanted:
            spawn(self._join(rid), on_error=self._net_error)

    def _on_history(self, ev) -> None:
        self.chat.add_history(ev.room_id, [(m.unix_ms, m.sender, m.text) for m in ev.messages])

    def _on_msg(self, ev) -> None:
        self.chat.add_message(ev.room_id, ev.unix_ms, ev.sender, ev.text)
        self.map.clear_typing(ev.sender)
        self.map.pulse_message(ev.sender, self.room_users.get(ev.room_id, set()))

    def _on_users(self, ev) -> None:
        self.room_users[ev.room_id] = set(ev.nicks)
        self._sync_map()

    def _on_user_evt(self, ev) -> None:
        if ev.nick == self.nick:
            return
        users = self.room_users.setdefault(ev.room_id, set())
        name = self.rooms.get(ev.room_id, "?")
        if ev.joined:
            users.add(ev.nick)
            self.chat.add_event(ev.room_id, f"{ev.nick} joined #{name}")
        else:
            users.discard(ev.nick)
            self.chat.add_event(ev.room_id, f"{ev.nick} left #{name}")
        self._sync_map()

    def _on_typing(self, ev) -> None:
        if ev.nick == self.nick:
            return
        self.chat.set_typing(ev.room_id, ev.nick)
        self.map.set_typing(ev.nick, self.rooms.get(ev.room_id, "?"))
        self.map.pulse_message(ev.nick, [self.nick], udp=True)

    def _on_file(self, ev) -> None:
        self.files[ev.file_id] = ev
        self.chat.add_file(ev.room_id, ev.file_id, ev.filename, ev.size, ev.sender)
        self.map.pulse_message(ev.sender, self.room_users.get(ev.room_id, set()))

    def _on_pong(self, ev) -> None:
        self.topbar.set_ping(ev.rtt_ms)
        self.map.set_ping(ev.rtt_ms)

    def _on_error(self, ev) -> None:
        text = f"{ERROR_TEXT.get(ev.code, 'Error')} ({ev.code}): {ev.message}"
        self.chat.add_event(None, text, error=True)
        self.statusBar().showMessage(text, 6000)

    def _on_disconnected(self, ev) -> None:
        self._set_conn("lost")
        self.chat.set_connected(False)
        self.map.set_connected(False)
        self.topbar.set_ping(None)
        self.map.set_ping(None)
        for rid in self.chat.joined_rooms():
            self.chat.add_event(rid, f"Disconnected from server{': ' + ev.reason if ev.reason else ''}", error=True)

    # ---- actions ----
    async def _join(self, rid: int) -> None:
        self.room_users.setdefault(rid, set())
        self.chat.room_joined(rid)       # tab exists before HISTORY/USERS arrive
        self._save_rooms()
        await self.client.join(rid)

    async def _leave(self, rid: int) -> None:
        self.chat.room_left(rid)
        self.room_users.pop(rid, None)
        self._sync_map()
        self._save_rooms()
        await self.client.leave(rid)

    def _say(self, rid: int, text: str) -> None:
        self._net(self.client.say(rid, text))

    def _upload(self, rid: int) -> None:
        path, _ = QFileDialog.getOpenFileName(self, f"Share a file in #{self.rooms.get(rid, '')}")
        if not path:
            return
        size = os.path.getsize(path)
        if size > MAX_FILE_BYTES:
            self.chat.add_event(rid, f"{os.path.basename(path)} is {human_size(size)}; the limit is 50 MiB.", error=True)
            return
        name = os.path.basename(path)
        spawn(self._transfer(f"up:{time.monotonic()}", name, "up", size,
                             lambda cb: self.client.upload(rid, path, progress=cb)))

    def _download(self, file_id: int) -> None:
        fe = self.files.get(file_id)
        name = os.path.basename(getattr(fe, "filename", f"file-{file_id}"))  # never trust a path
        folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
        dest, _ = QFileDialog.getSaveFileName(self, "Save file", os.path.join(folder, name))
        if not dest:
            return
        spawn(self._transfer(f"down:{file_id}:{time.monotonic()}", name, "down", getattr(fe, "size", 0),
                             lambda cb: self.client.download(file_id, dest, progress=cb), dest))

    async def _transfer(self, key: str, name: str, direction: str, size: int,
                        start: Callable[[Callable[[int, int], None]], Awaitable], dest: str | None = None) -> None:
        def progress(done: int, total: int) -> None:
            self.map.transfers.update_transfer(key, name, direction, done, total)

        self._xfers.add(key)
        self._update_xfer_label()
        progress(0, size or 1)
        try:
            await start(progress)
            progress(1, 1)
            if dest:
                self.statusBar().showMessage(f"Saved {dest}", 6000)
        except Exception as e:
            self.map.transfers.fail(key, str(e))
            self.chat.add_event(None, f"Transfer of {name} failed: {e}", error=True)
        finally:
            self._xfers.discard(key)
            self._update_xfer_label()

    # ---- helpers ----
    def _net(self, coro) -> None:
        if self.client is not None:
            spawn(coro, on_error=self._net_error)
        else:
            coro.close()

    def _net_error(self, e: BaseException) -> None:
        self.chat.add_event(None, f"Network error: {e}", error=True)

    def _sync_map(self) -> None:
        by_nick: dict[str, list[str]] = {}
        for rid, users in self.room_users.items():
            for n in users:
                by_nick.setdefault(n, []).append(self.rooms.get(rid, "?"))
        self.map.set_users(by_nick.keys(), by_nick)

    def _rooms_key(self) -> str:
        return f"rooms/{self.target.server_name if self.target else 'default'}"

    def _save_rooms(self) -> None:
        names = [self.rooms[r] for r in self.chat.joined_rooms() if r in self.rooms]
        self.settings.setValue(self._rooms_key(), ",".join(names))

    def _update_xfer_label(self) -> None:
        n = len(self._xfers)
        self.xfer_label.setText("" if n == 0 else f"{n} transfer{'s' if n > 1 else ''}")

    def _set_conn(self, state: str) -> None:
        self._conn_state = state
        p = self.pal
        color, text = {
            "idle": (p.border, "Not connected"),
            "connecting": (p.udp, "Connecting…"),
            "ok": (p.ok, "Connected"),
            "lost": (p.danger, "Disconnected"),
        }[state]
        self.conn_label.setText(f'<span style="color:{color}">●</span>&nbsp;{text}')

    def _apply_palette_local(self) -> None:
        self.map.set_palette(self.pal)
        self.chat.set_palette(self.pal)
        self.topbar.set_palette(self.pal)
        self._set_conn(self._conn_state)
