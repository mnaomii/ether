from __future__ import annotations

import asyncio
import logging

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from . import theme
from .discovery_dialog import DiscoveryDialog
from .main_window import MainWindow

log = logging.getLogger("ether_gui")


def load_backend(demo: bool):
    """Return (ClientClass, discover_coroutine_fn). Falls back to the mock if ether_core is missing."""
    if not demo:
        try:
            from ether_core.net import EtherClient, discover  # type: ignore
            return EtherClient, discover
        except ImportError:
            log.warning("ether_core not importable; starting in demo mode")
    from .mock_client import MockClient, mock_discover
    return MockClient, mock_discover


async def run(args) -> None:
    app = QApplication.instance()
    settings = QSettings("Ether", "ether_gui")
    pal = theme.by_name(args.theme or settings.value("theme", "light"))
    theme.apply(app, pal)

    Client, discover = load_backend(args.demo)
    window = MainWindow(settings, pal)
    closed = asyncio.Event()
    window.closed.connect(closed.set)

    error: str | None = None
    while True:
        dlg = DiscoveryDialog(discover, settings, window.pal, error=error)
        target = await dlg.run()
        if target is None:
            return
        client = Client(on_event=window.handle_event)
        window.attach(client, target)
        try:
            await client.connect(target.host, target.port, target.nick, target.password)
        except Exception as e:
            error = f"Could not connect to {target.host}:{target.port} — {e}"
            window.detach()
            continue
        window.map.set_ghosts(dlg.other_servers(target))
        break

    window.on_connected()
    window.show()
    await closed.wait()
    close = getattr(client, "close", None)
    if close:
        try:
            await asyncio.wait_for(close(), timeout=2.0)
        except Exception:
            pass
