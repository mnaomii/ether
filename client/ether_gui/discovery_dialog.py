"""Startup dialog: live UDP-broadcast results + manual address fallback + nickname."""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIntValidator
from PyQt6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QPushButton, QVBoxLayout, QWidget)

from .contract import ConnectTarget, ServerInfo
from .theme import Palette
from .util import MAX_NICK_BYTES, MAX_PASS_BYTES, spawn, utf8_len

DiscoverFn = Callable[..., Awaitable[list[ServerInfo]]]
STALE_AFTER_S = 5.0


class _ServerRow(QWidget):
    def __init__(self, info: ServerInfo):
        super().__init__()
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 8, 12, 8)
        col = QVBoxLayout()
        col.setSpacing(1)
        self.name = QLabel(info.name)
        self.name.setObjectName("SectionTitle")
        self.addr = QLabel(f"{info.host}:{info.tcp_port}")
        self.addr.setProperty("mono", True)
        self.addr.setProperty("muted", True)
        col.addWidget(self.name)
        col.addWidget(self.addr)
        h.addLayout(col, 1)
        self.users = QLabel()
        self.users.setProperty("muted", True)
        h.addWidget(self.users)
        self.update_info(info)

    def update_info(self, info: ServerInfo) -> None:
        self.users.setText(f"{info.users_online} online")


class DiscoveryDialog(QDialog):
    def __init__(self, discover_fn: DiscoverFn, settings, pal: Palette, error: str | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.discover_fn, self.settings, self.pal = discover_fn, settings, pal
        self.servers: dict[str, tuple[ServerInfo, float]] = {}
        self._items: dict[str, tuple[QListWidgetItem, _ServerRow]] = {}
        self._target: ConnectTarget | None = None
        self._accepted = False
        self._blink = False

        self.setWindowTitle("Connect to Ether")
        self.setMinimumWidth(480)
        v = QVBoxLayout(self)
        v.setContentsMargins(22, 20, 22, 18)
        v.setSpacing(10)

        hdr = QHBoxLayout()
        title = QLabel("Servers on this network")
        title.setObjectName("SectionTitle")
        hdr.addWidget(title)
        hdr.addStretch(1)
        self.dot = QLabel("●")
        self.status = QLabel("broadcasting :5051")
        self.status.setProperty("mono", True)
        self.status.setProperty("muted", True)
        hdr.addWidget(self.dot)
        hdr.addWidget(self.status)
        v.addLayout(hdr)

        self.list = QListWidget()
        self.list.setMinimumHeight(132)
        self.list.itemSelectionChanged.connect(self._on_select)
        self.list.itemDoubleClicked.connect(lambda _i: self._try_accept())
        v.addWidget(self.list)

        self.hint = QLabel("Searching… If nothing appears, this network may block broadcast. "
                           "Enter the server address below.")
        self.hint.setWordWrap(True)
        self.hint.setProperty("muted", True)
        v.addWidget(self.hint)
        v.addSpacing(6)

        lab = QLabel("Or enter an address")
        lab.setProperty("muted", True)
        v.addWidget(lab)
        row = QHBoxLayout()
        self.host = QLineEdit(str(settings.value("last/host", "")))
        self.host.setPlaceholderText("192.168.1.x")
        self.port = QLineEdit(str(settings.value("last/port", "5050")))
        self.port.setValidator(QIntValidator(1, 65535, self))
        self.port.setFixedWidth(90)
        row.addWidget(self.host, 1)
        row.addWidget(self.port)
        v.addLayout(row)
        v.addSpacing(6)

        nrow = QHBoxLayout()
        nt = QLabel("Nickname")
        nt.setObjectName("SectionTitle")
        nrow.addWidget(nt)
        nrow.addStretch(1)
        self.counter = QLabel()
        self.counter.setProperty("mono", True)
        self.counter.setProperty("muted", True)
        nrow.addWidget(self.counter)
        v.addLayout(nrow)
        self.nick = QLineEdit(str(settings.value("last/nick", "")))
        self.nick.setMaxLength(MAX_NICK_BYTES)
        self.nick.setPlaceholderText("e.g. ana")
        self.nick.textChanged.connect(self._update_counter)
        v.addWidget(self.nick)
        v.addSpacing(6)

        pt = QLabel("Password")
        pt.setObjectName("SectionTitle")
        v.addWidget(pt)
        self.password = QLineEdit()  # never saved to QSettings
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("a new nickname creates the account")
        self.password.setToolTip("Sent unencrypted over TCP (see it in Wireshark)")
        v.addWidget(self.password)

        self.error = QLabel(error or "")
        self.error.setObjectName("Error")
        self.error.setWordWrap(True)
        self.error.setVisible(bool(error))
        v.addWidget(self.error)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("Primary")
        self.connect_btn.setDefault(True)
        self.connect_btn.clicked.connect(self._try_accept)
        btns.addWidget(cancel)
        btns.addWidget(self.connect_btn)
        v.addSpacing(4)
        v.addLayout(btns)

        self._update_counter()
        self._set_dot(True)
        next((w for w in (self.host, self.nick) if not w.text()), self.password).setFocus()

    async def run(self) -> ConnectTarget | None:
        """Show non-modally (keeps asyncio running) and resolve when closed."""
        fut = asyncio.get_event_loop().create_future()
        self.finished.connect(lambda _code: fut.done() or fut.set_result(None))
        task = spawn(self._discover_loop())
        self.open()
        await fut
        task.cancel()
        return self._target if self._accepted else None

    def other_servers(self, target: ConnectTarget) -> list[tuple[str, str, int]]:
        return [(i.name, i.host, i.tcp_port) for i, _ in self.servers.values()
                if (i.host, i.tcp_port) != (target.host, target.port)]

    async def _discover_loop(self) -> None:
        while True:
            try:
                found = await self.discover_fn(timeout=1.0)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # e.g. firewall / no broadcast-capable interface
                self.status.setText(f"broadcast failed: {e}")
                self._set_dot(False)
                await asyncio.sleep(2.0)
                continue
            now = time.monotonic()
            for info in found:
                self._upsert(info, now)
            self._prune(now)
            self._blink = not self._blink
            self._set_dot(True)
            self.status.setText("broadcasting :5051")

    def _upsert(self, info: ServerInfo, now: float) -> None:
        key = f"{info.host}:{info.tcp_port}"
        self.servers[key] = (info, now)
        if key in self._items:
            self._items[key][1].update_info(info)
            return
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, key)
        row = _ServerRow(info)
        item.setSizeHint(row.sizeHint())
        self.list.addItem(item)
        self.list.setItemWidget(item, row)
        self._items[key] = (item, row)
        if self.list.count() == 1 and not self.host.text():
            self.list.setCurrentItem(item)
        self.hint.setVisible(False)

    def _prune(self, now: float) -> None:
        for key, (_, seen) in list(self.servers.items()):
            if now - seen > STALE_AFTER_S:
                del self.servers[key]
                item, _ = self._items.pop(key)
                self.list.takeItem(self.list.row(item))
        self.hint.setVisible(not self.servers)

    def _on_select(self) -> None:
        items = self.list.selectedItems()
        if not items:
            return
        info, _ = self.servers[items[0].data(Qt.ItemDataRole.UserRole)]
        self.host.setText(info.host)
        self.port.setText(str(info.tcp_port))

    def _set_dot(self, ok: bool) -> None:
        color = self.pal.danger if not ok else (self.pal.udp if self._blink else self.pal.border)
        self.dot.setStyleSheet(f"color: {color}; font-size: 8pt;")

    def _update_counter(self) -> None:
        n = utf8_len(self.nick.text())
        self.counter.setText(f"{n} / {MAX_NICK_BYTES} bytes")

    def _show_error(self, text: str) -> None:
        self.error.setText(text)
        self.error.setVisible(True)

    def _try_accept(self) -> None:
        if self._accepted:
            return
        host, nick = self.host.text().strip(), self.nick.text().strip()
        if not host:
            return self._show_error("Pick a server or enter its address.")
        try:
            port = int(self.port.text())
        except ValueError:
            return self._show_error("Port must be a number.")
        nbytes = utf8_len(nick)
        if nbytes == 0:
            return self._show_error("Choose a nickname.")
        if nbytes > MAX_NICK_BYTES:
            return self._show_error(f"Nickname is {nbytes} bytes; the server allows {MAX_NICK_BYTES}.")
        if ":" in nick:
            return self._show_error("Nickname can't contain ':'.")
        password = self.password.text()
        pbytes = utf8_len(password)
        if pbytes == 0:
            return self._show_error("Enter a password.")
        if pbytes > MAX_PASS_BYTES:
            return self._show_error(f"Password is {pbytes} bytes; the server allows {MAX_PASS_BYTES}.")
        info = self.servers.get(f"{host}:{port}")
        self._target = ConnectTarget(host, port, nick, password, info[0].name if info else host)
        self.settings.setValue("last/host", host)
        self.settings.setValue("last/port", port)
        self.settings.setValue("last/nick", nick)
        self._accepted = True
        self.accept()
