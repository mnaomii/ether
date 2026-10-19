import argparse
import asyncio
import logging
import sys

import qasync
from PyQt6.QtWidgets import QApplication

from .app import run


def main() -> None:
    ap = argparse.ArgumentParser(prog="ether_gui", description="Ether PyQt6 client")
    ap.add_argument("--demo", action="store_true", help="use the built-in mock client (no server needed)")
    ap.add_argument("--theme", choices=["light", "dark"], help="override the saved theme")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    app = QApplication(sys.argv)
    app.setApplicationName("Ether")
    app.setOrganizationName("Ether")
    app.setQuitOnLastWindowClosed(False)  # run() decides when we're done
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    with loop:
        loop.run_until_complete(run(args))


if __name__ == "__main__":
    main()
