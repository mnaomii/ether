"""Network map: the server in the middle, everyone you share a room with on a ring.

- solid accent link   = you (TCP control connection), labelled with UDP ping
- thin link           = other users
- dashed amber link   = user currently typing (UDP TYPING_EVT), with a rotating ring
- dashed grey ghost   = other servers seen via broadcast, not connected
- travelling dots     = a message going sender -> server -> room members
"""
from __future__ import annotations

import math
import time
from typing import Callable, Iterable

from PyQt6.QtCore import (QEasingCurve, QPointF, QPropertyAnimation, QRectF, Qt, QTimer,
                          QVariantAnimation, pyqtSignal)
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import (QFrame, QGraphicsEllipseItem, QGraphicsItem, QGraphicsObject,
                             QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QProgressBar,
                             QVBoxLayout, QWidget)

from .theme import MONO_FONT, UI_FONT, Palette
from .util import TYPING_TTL, initials

R_INNER = 80
R_USERS = 190
R_GHOSTS = 290
SCENE_RECT = QRectF(-400, -330, 800, 660)
SELF_ANGLE = 135  # degrees, Qt y-down: bottom-left


def _font(family: str, pt: float, bold: bool = False) -> QFont:
    f = QFont(family)
    f.setPointSizeF(pt)
    if bold:
        f.setWeight(QFont.Weight.DemiBold)
    return f


class MapScene(QGraphicsScene):
    def __init__(self, pal: Palette):
        super().__init__()
        self.pal = pal
        self.setSceneRect(SCENE_RECT)


class Node(QGraphicsObject):
    SERVER, SELF, USER, GHOST = "server", "self", "user", "ghost"
    RADII = {SERVER: 44, SELF: 26, USER: 24, GHOST: 20}

    def __init__(self, kind: str, title: str, subtitle: str = ""):
        super().__init__()
        self.kind, self.title, self.subtitle = kind, title, subtitle
        self.typing_label: str | None = None
        self.phase = 0.0
        self.dim = False
        self._anim: QPropertyAnimation | None = None
        self.setZValue(2)

    @property
    def radius(self) -> int:
        return self.RADII[self.kind]

    def boundingRect(self) -> QRectF:
        r = self.radius
        return QRectF(-90, -r - 10, 180, 2 * r + 52)

    def paint(self, p: QPainter, option, widget=None) -> None:
        pal: Palette = self.scene().pal
        r = self.radius
        c = QColor
        center = QPointF(0, 0)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        center_flag = Qt.AlignmentFlag.AlignCenter

        if self.kind == self.SERVER:
            ink = pal.muted if self.dim else pal.text
            p.setPen(QPen(c(ink), 2))
            p.setBrush(c(pal.window))
            p.drawEllipse(center, r, r)
            p.setPen(c(ink))
            p.setFont(_font(UI_FONT, 9.5, True))
            fm = QFontMetricsF(p.font())
            p.drawText(QRectF(-r + 4, -17, 2 * r - 8, 18), center_flag,
                       fm.elidedText(self.title, Qt.TextElideMode.ElideRight, 2 * r - 10))
            p.setPen(c(pal.muted))
            p.setFont(_font(MONO_FONT, 7))
            p.drawText(QRectF(-r, 2, 2 * r, 14), center_flag, self.subtitle)
            return

        typing = self.typing_label is not None
        if typing:
            pen = QPen(c(pal.udp), 1.6)
            pen.setStyle(Qt.PenStyle.CustomDashLine)
            pen.setDashPattern([3.0, 3.0])
            pen.setDashOffset(self.phase)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(center, r + 7, r + 7)

        if self.kind == self.GHOST:
            pen = QPen(c(pal.muted), 1.2)
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(c(pal.canvas))
            p.drawEllipse(center, r, r)
            p.setPen(c(pal.muted))
            p.setFont(_font(UI_FONT, 11))
            p.drawText(QRectF(-r, -r, 2 * r, 2 * r), center_flag, "?")
        else:
            if self.kind == self.SELF:
                fill, fg = pal.accent, pal.on_accent
            elif typing:
                fill, fg = pal.udp_soft, pal.udp
            else:
                fill, fg = pal.selected, pal.text
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c(fill))
            p.drawEllipse(center, r, r)
            p.setPen(c(fg))
            p.setFont(_font(UI_FONT, 10, True))
            p.drawText(QRectF(-r, -r, 2 * r, 2 * r), center_flag, initials(self.title))

        y = r + (11 if typing else 6)
        p.setFont(_font(MONO_FONT if self.kind == self.GHOST else UI_FONT, 8))
        fm = QFontMetricsF(p.font())
        label = f"{self.title} · you" if self.kind == self.SELF else self.title
        p.setPen(c(pal.text if self.kind == self.USER else pal.muted))
        top = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
        p.drawText(QRectF(-88, y, 176, 14), top, fm.elidedText(label, Qt.TextElideMode.ElideRight, 172))
        if typing:
            p.setPen(c(pal.udp))
            p.drawText(QRectF(-88, y + 14, 176, 14), top, self.typing_label)


class Edge(QGraphicsItem):
    def __init__(self, a: Node, b: Node, ghost: bool = False):
        super().__init__()
        self.a, self.b, self.ghost = a, b, ghost
        self.label: str | None = None
        self.setZValue(0)
        for n in (a, b):
            n.xChanged.connect(self._moved)
            n.yChanged.connect(self._moved)

    def detach(self) -> None:
        for n in (self.a, self.b):
            try:
                n.xChanged.disconnect(self._moved)
                n.yChanged.disconnect(self._moved)
            except (TypeError, RuntimeError):
                pass

    def _moved(self) -> None:
        self.prepareGeometryChange()
        self.update()

    def _ends(self, typing: bool) -> tuple[QPointF, QPointF]:
        pa, pb = self.a.pos(), self.b.pos()
        dx, dy = pb.x() - pa.x(), pb.y() - pa.y()
        length = math.hypot(dx, dy)
        if length < 1:
            return pa, pb
        ux, uy = dx / length, dy / length
        ra = self.a.radius + 2
        rb = self.b.radius + (9 if typing else 2)
        return QPointF(pa.x() + ux * ra, pa.y() + uy * ra), QPointF(pb.x() - ux * rb, pb.y() - uy * rb)

    def boundingRect(self) -> QRectF:
        return QRectF(self.a.pos(), self.b.pos()).normalized().adjusted(-50, -20, 50, 20)

    def paint(self, p: QPainter, option, widget=None) -> None:
        pal: Palette = self.scene().pal
        c = QColor
        typing = self.b.typing_label is not None
        pa, pb = self._ends(typing)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self.ghost:
            pen = QPen(c(pal.muted), 1)
            pen.setStyle(Qt.PenStyle.CustomDashLine)
            pen.setDashPattern([2.0, 5.0])
        elif self.a.dim:
            pen = QPen(c(pal.border), 1)
        elif typing:
            pen = QPen(c(pal.udp), 1.6)
            pen.setStyle(Qt.PenStyle.CustomDashLine)
            pen.setDashPattern([4.0, 4.0])
        elif self.b.kind == Node.SELF:
            pen = QPen(c(pal.accent), 2.2)
        else:
            pen = QPen(c(pal.muted), 1)
        p.setPen(pen)
        p.drawLine(pa, pb)

        label = "TYPING" if typing else (self.label if self.b.kind == Node.SELF and not self.a.dim else None)
        if not label:
            return
        mid = QPointF((pa.x() + pb.x()) / 2, (pa.y() + pb.y()) / 2)
        p.setFont(_font(MONO_FONT, 7))
        fm = QFontMetricsF(p.font())
        w, h = fm.horizontalAdvance(label) + 12, fm.height() + 4
        rect = QRectF(mid.x() - w / 2, mid.y() - h / 2, w, h)
        p.setPen(QPen(c(pal.border), 1))
        p.setBrush(c(pal.window))
        p.drawRoundedRect(rect, 4, 4)
        p.setPen(c(pal.udp if typing else pal.text))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)


class Legend(QWidget):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.pal: Palette | None = None
        self.setFixedSize(140, 18)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, e) -> None:
        if not self.pal:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(_font(MONO_FONT, 8))
        y = 9
        left = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        p.setPen(QPen(QColor(self.pal.accent), 2))
        p.drawLine(QPointF(0, y), QPointF(16, y))
        p.setPen(QColor(self.pal.muted))
        p.drawText(QRectF(22, 0, 40, 18), left, "tcp")
        pen = QPen(QColor(self.pal.udp), 2)
        pen.setStyle(Qt.PenStyle.CustomDashLine)
        pen.setDashPattern([2.0, 2.0])
        p.setPen(pen)
        p.drawLine(QPointF(66, y), QPointF(82, y))
        p.setPen(QColor(self.pal.muted))
        p.drawText(QRectF(88, 0, 40, 18), left, "udp")


class TransfersOverlay(QFrame):
    """Bottom-right card listing active uploads/downloads."""
    changed = pyqtSignal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("Overlay")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 8, 12, 8)
        self._layout.setSpacing(6)
        self.rows: dict[str, tuple[QWidget, QLabel, QProgressBar, QLabel]] = {}
        self.hide()

    def update_transfer(self, key: str, filename: str, direction: str, done: int, total: int) -> None:
        if key not in self.rows:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(10)
            name = QLabel(f"{'↑' if direction == 'up' else '↓'} {filename}")
            name.setMaximumWidth(200)
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setTextVisible(False)
            bar.setFixedWidth(90)
            pct = QLabel("0%")
            pct.setProperty("mono", True)
            pct.setProperty("muted", True)
            pct.setFixedWidth(42)
            h.addWidget(name, 1)
            h.addWidget(bar)
            h.addWidget(pct)
            self._layout.addWidget(row)
            self.rows[key] = (row, name, bar, pct)
        _, _, bar, pct = self.rows[key]
        frac = 1.0 if total <= 0 else min(done / total, 1.0)
        bar.setValue(int(frac * 1000))
        pct.setText(f"{frac * 100:.0f}%")
        if frac >= 1.0:
            QTimer.singleShot(2500, lambda k=key: self.remove(k))
        self._refresh()

    def fail(self, key: str, reason: str = "failed") -> None:
        if key in self.rows:
            self.rows[key][3].setText("failed")
            self.rows[key][0].setToolTip(reason)
            QTimer.singleShot(4000, lambda k=key: self.remove(k))

    def remove(self, key: str) -> None:
        row = self.rows.pop(key, None)
        if row:
            row[0].deleteLater()
        self._refresh()

    def _refresh(self) -> None:
        self.setVisible(bool(self.rows))
        self.adjustSize()
        self.changed.emit()


class NetworkMapView(QGraphicsView):
    def __init__(self, pal: Palette, parent: QWidget | None = None):
        self._scene = MapScene(pal)
        super().__init__(self._scene, parent)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMinimumWidth(360)

        self.server = Node(Node.SERVER, "server", "")
        self._scene.addItem(self.server)
        self.self_nick: str | None = None
        self.nodes: dict[str, Node] = {}
        self.edges: dict[str, Edge] = {}
        self.ghosts: dict[str, tuple[Node, Edge]] = {}
        self._typing: dict[str, float] = {}
        self._anims: set = set()

        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)

        self.legend = Legend(self)
        self.legend.pal = pal
        self.transfers = TransfersOverlay(self)
        self.transfers.changed.connect(self._place_overlays)

    # ---- public API ----
    @property
    def pal(self) -> Palette:
        return self._scene.pal

    def set_palette(self, pal: Palette) -> None:
        self._scene.pal = pal
        self.legend.pal = pal
        self.legend.update()
        self._scene.update()
        self.viewport().update()

    def set_server(self, name: str, host: str, port: int) -> None:
        self.server.title, self.server.subtitle = name, host
        self.server.setToolTip(f"{name}\n{host}:{port}\nTCP {port} (control + file data), UDP 5051")
        self.server.update()

    def set_self(self, nick: str) -> None:
        self.self_nick = nick
        self.set_users([], {})

    def set_users(self, nicks: Iterable[str], rooms_by_nick: dict[str, list[str]]) -> None:
        wanted = set(nicks)
        if self.self_nick:
            wanted.add(self.self_nick)
        for nick in list(self.nodes):
            if nick not in wanted:
                self._remove_node(nick)
        for nick in wanted:
            if nick not in self.nodes:
                node = Node(Node.SELF if nick == self.self_nick else Node.USER, nick)
                node.setPos(0, 0)
                edge = Edge(self.server, node)
                self._scene.addItem(edge)
                self._scene.addItem(node)
                self.nodes[nick], self.edges[nick] = node, edge
        for nick, node in self.nodes.items():
            rooms = ", ".join(f"#{r}" for r in rooms_by_nick.get(nick, []))
            node.setToolTip(f"{nick}{' (you)' if nick == self.self_nick else ''}" + (f"\nin {rooms}" if rooms else ""))
        self._relayout()

    def set_ghosts(self, servers: Iterable[tuple[str, str, int]]) -> None:
        for _, (node, edge) in self.ghosts.items():
            edge.detach()
            self._scene.removeItem(edge)
            self._scene.removeItem(node)
        self.ghosts.clear()
        for i, (name, host, port) in enumerate(servers):
            node = Node(Node.GHOST, name)
            node.setToolTip(f"{name}\n{host}:{port}\nfound via broadcast · not connected")
            ang = math.radians(225 + i * 26)
            node.setPos(R_GHOSTS * math.cos(ang), R_GHOSTS * math.sin(ang))
            edge = Edge(self.server, node, ghost=True)
            self._scene.addItem(edge)
            self._scene.addItem(node)
            self.ghosts[f"{host}:{port}"] = (node, edge)

    def set_typing(self, nick: str, room_name: str) -> None:
        node = self.nodes.get(nick)
        if not node or nick == self.self_nick:
            return
        node.typing_label = f"typing in #{room_name}"
        self._typing[nick] = time.monotonic() + TYPING_TTL
        node.update()
        self.edges[nick].update()
        if not self._timer.isActive():
            self._timer.start()

    def clear_typing(self, nick: str) -> None:
        self._typing.pop(nick, None)
        node = self.nodes.get(nick)
        if node and node.typing_label is not None:
            node.typing_label = None
            node.update()
            self.edges[nick].update()

    def set_ping(self, rtt_ms: float | None) -> None:
        if self.self_nick in self.edges:
            edge = self.edges[self.self_nick]
            edge.label = None if rtt_ms is None else f"{rtt_ms:.0f} ms"
            edge.update()

    def set_connected(self, ok: bool) -> None:
        self.server.dim = not ok
        self._scene.update()

    def pulse_message(self, sender: str, recipients: Iterable[str], udp: bool = False) -> None:
        """Animate sender -> server, then server -> each recipient (the server relays)."""
        src = self.nodes.get(sender)
        if not src:
            return
        color = self.pal.udp if udp else self.pal.accent
        targets = [self.nodes[n] for n in recipients if n in self.nodes and n != sender]

        def fan_out() -> None:
            for t in targets:
                self._pulse(self.server, t, color)

        self._pulse(src, self.server, color, on_done=fan_out)

    def clear(self) -> None:
        for nick in list(self.nodes):
            self._remove_node(nick, fade=False)
        self.set_ghosts([])
        self.self_nick = None
        self.server.dim = False

    # ---- internals ----
    def _remove_node(self, nick: str, fade: bool = True) -> None:
        node, edge = self.nodes.pop(nick), self.edges.pop(nick)
        self._typing.pop(nick, None)
        edge.detach()
        self._scene.removeItem(edge)
        if not fade:
            self._scene.removeItem(node)
            return
        anim = QPropertyAnimation(node, b"opacity", self)
        anim.setDuration(220)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)

        def done() -> None:
            if node.scene():
                self._scene.removeItem(node)
            self._anims.discard(anim)

        anim.finished.connect(done)
        self._anims.add(anim)
        anim.start()

    def _relayout(self) -> None:
        others = sorted(n for n in self.nodes if n != self.self_nick)
        order = ([self.self_nick] if self.self_nick in self.nodes else []) + others
        count = max(len(order), 1)
        for i, nick in enumerate(order):
            ang = math.radians(SELF_ANGLE + i * 360 / count)
            target = QPointF(R_USERS * math.cos(ang), R_USERS * math.sin(ang))
            node = self.nodes[nick]
            if node._anim:
                node._anim.stop()
            anim = QPropertyAnimation(node, b"pos", self)
            anim.setDuration(520)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.setEndValue(target)
            node._anim = anim
            anim.start()

    def _pulse(self, src: Node, dst: Node, color: str, on_done: Callable[[], None] | None = None) -> None:
        dot = QGraphicsEllipseItem(-4, -4, 8, 8)
        dot.setBrush(QColor(color))
        dot.setPen(QPen(Qt.PenStyle.NoPen))
        dot.setZValue(1)
        a, b = src.pos(), dst.pos()
        dot.setPos(a)
        self._scene.addItem(dot)
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(420)
        anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        anim.valueChanged.connect(
            lambda t: dot.setPos(QPointF(a.x() + (b.x() - a.x()) * t, a.y() + (b.y() - a.y()) * t)))

        def fin() -> None:
            self._scene.removeItem(dot)
            self._anims.discard(anim)
            if on_done:
                on_done()

        anim.finished.connect(fin)
        self._anims.add(anim)
        anim.start()

    def _tick(self) -> None:
        now = time.monotonic()
        for nick, deadline in list(self._typing.items()):
            if now > deadline:
                self.clear_typing(nick)
        for nick in self._typing:
            node = self.nodes[nick]
            node.phase = (node.phase - 0.5) % 6.0
            node.update()
        if not self._typing:
            self._timer.stop()

    def drawBackground(self, p: QPainter, rect: QRectF) -> None:
        pal = self.pal
        p.fillRect(rect, QColor(pal.canvas))
        step = 18
        x0 = math.floor(rect.left() / step) * step
        y0 = math.floor(rect.top() / step) * step
        pts = QPolygonF()
        y = y0
        while y < rect.bottom():
            x = x0
            while x < rect.right():
                pts.append(QPointF(x, y))
                x += step
            y += step
        pen = QPen(QColor(pal.grid_dot), 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawPoints(pts)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(pal.border), 1))
        p.drawEllipse(QPointF(0, 0), R_INNER, R_INNER)
        ring = QPen(QColor(pal.border), 1)
        ring.setStyle(Qt.PenStyle.CustomDashLine)
        ring.setDashPattern([2.0, 4.0])
        p.setPen(ring)
        p.drawEllipse(QPointF(0, 0), R_USERS, R_USERS)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self.fitInView(SCENE_RECT, Qt.AspectRatioMode.KeepAspectRatio)
        self._place_overlays()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.fitInView(SCENE_RECT, Qt.AspectRatioMode.KeepAspectRatio)
        self._place_overlays()

    def _place_overlays(self) -> None:
        w, h = self.width(), self.height()
        self.legend.move(14, h - self.legend.height() - 12)
        if self.transfers.isVisible():
            self.transfers.adjustSize()
            self.transfers.move(w - self.transfers.width() - 12, h - self.transfers.height() - 10)
            self.transfers.raise_()
