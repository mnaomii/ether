"""Light/dark palettes and the app-wide Qt stylesheet.

All colours live here. Widgets that paint themselves (map, ping bars, chat
HTML) read the active Palette object; everything else is styled by QSS.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from string import Template

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

UI_FONT = "Segoe UI"        # Windows target; Qt falls back gracefully elsewhere
MONO_FONT = "Cascadia Mono"  # falls back to Consolas / monospace


@dataclass(frozen=True)
class Palette:
    name: str
    dark: bool
    window: str
    panel: str
    surface: str
    input: str
    selected: str
    text: str
    muted: str
    border: str
    accent: str        # TCP / primary
    accent_hover: str
    on_accent: str
    accent_soft: str
    udp: str           # UDP / typing
    udp_soft: str
    ok: str
    danger: str
    canvas: str        # network map background
    grid_dot: str


LIGHT = Palette(
    name="light", dark=False,
    window="#ffffff", panel="#f4f4f2", surface="#fafaf8", input="#ffffff",
    selected="#e8e9e6", text="#1d1d1b", muted="#6b6b66", border="#dcdcd7",
    accent="#0f6e7d", accent_hover="#0c5965", on_accent="#ffffff", accent_soft="#ddeff1",
    udp="#a8641a", udp_soft="#f7ecd9", ok="#2f8f4e", danger="#b5402f",
    canvas="#f8f8f6", grid_dot="#dedeD9",
)

DARK = Palette(
    name="dark", dark=True,
    window="#1b1c1e", panel="#151617", surface="#232528", input="#202224",
    selected="#2c2f33", text="#e7e7e4", muted="#9a9b97", border="#303236",
    accent="#6cc3d1", accent_hover="#85d0dc", on_accent="#101112", accent_soft="#23434a",
    udp="#e2b25c", udp_soft="#3d3322", ok="#5cc07a", danger="#e07a6a",
    canvas="#17181a", grid_dot="#2a2c30",
)


def by_name(name: str | None) -> Palette:
    return DARK if (name or "").lower() == "dark" else LIGHT


_QSS = Template("""
* { font-family: "$ui"; font-size: 10pt; color: $text; }
QMainWindow, QDialog { background: $window; }
QWidget#ChatPanel { background: $window; }
QFrame#TopBar { background: $window; border-bottom: 1px solid $border; }
QFrame#Rule { background: $border; border: none; }
QLabel { background: transparent; }
QLabel[muted="true"] { color: $muted; }
QLabel[mono="true"] { font-family: "$mono"; font-size: 9pt; }
QLabel#Brand { font-size: 12pt; font-weight: 600; }
QLabel#SectionTitle { font-weight: 600; }
QLabel#Chip { font-family: "$mono"; font-size: 9pt; color: $muted;
              border: 1px solid $border; border-radius: 10px; padding: 2px 9px; }
QLabel#Typing { color: $udp; font-size: 9pt; }
QLabel#Error { color: $danger; }

QLineEdit { background: $input; border: 1px solid $border; border-radius: 6px;
            padding: 7px 10px; selection-background-color: $accent; selection-color: $on_accent; }
QLineEdit:focus { border: 1px solid $accent; }
QLineEdit:disabled { color: $muted; }
QFrame#Composer { background: $input; border: 1px solid $border; border-radius: 8px; }
QFrame#Composer QLineEdit { border: none; background: transparent; padding: 6px 4px; }

QPushButton { background: $surface; border: 1px solid $border; border-radius: 6px; padding: 6px 14px; }
QPushButton:hover { background: $selected; }
QPushButton:disabled { color: $muted; }
QPushButton#Primary { background: $accent; color: $on_accent; border: 1px solid $accent; font-weight: 600; }
QPushButton#Primary:hover { background: $accent_hover; border-color: $accent_hover; }
QPushButton#Primary:disabled { background: $selected; border-color: $selected; color: $muted; }

QToolButton { background: transparent; border: 1px solid transparent; border-radius: 6px;
              padding: 4px 9px; color: $muted; }
QToolButton:hover { background: $selected; color: $text; }
QToolButton:disabled { color: $border; }
QToolButton::menu-indicator { image: none; width: 0; }

QTabBar { background: transparent; }
QTabBar::tab { background: transparent; color: $muted; padding: 6px 12px; margin-right: 2px;
               border: none; border-radius: 6px; }
QTabBar::tab:selected { background: $selected; color: $text; font-weight: 600; }
QTabBar::tab:hover:!selected { color: $text; }

QTextBrowser { background: $window; border: none; }
QListWidget { background: $input; border: 1px solid $border; border-radius: 6px; outline: 0; }
QListWidget::item { border-bottom: 1px solid $border; }
QListWidget::item:selected { background: $selected; }
QListWidget::item:hover:!selected { background: $surface; }

QProgressBar { background: $selected; border: none; border-radius: 2px; min-height: 4px; max-height: 4px; }
QProgressBar::chunk { background: $accent; border-radius: 2px; }
QFrame#Overlay { background: $window; border: 1px solid $border; border-radius: 6px; }

QStatusBar { background: $panel; border-top: 1px solid $border; }
QStatusBar::item { border: none; }
QStatusBar QLabel { color: $muted; font-family: "$mono"; font-size: 8.5pt; padding: 0 8px; }

QSplitter::handle { background: $border; }
QGraphicsView { border: none; background: $canvas; }

QMenu { background: $surface; border: 1px solid $border; padding: 4px; }
QMenu::item { padding: 6px 22px 6px 12px; border-radius: 4px; }
QMenu::item:selected { background: $selected; }
QMenu::item:disabled { color: $muted; }
QMenu::separator { height: 1px; background: $border; margin: 4px 6px; }
QToolTip { background: $surface; color: $text; border: 1px solid $border; padding: 4px 6px; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: $selected; border-radius: 4px; min-height: 30px; margin: 2px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
""")


def stylesheet(p: Palette) -> str:
    return _QSS.substitute(asdict(p), ui=UI_FONT, mono=MONO_FONT)


def apply(app: QApplication, p: Palette) -> None:
    """Apply palette + stylesheet app-wide. Safe to call repeatedly (theme toggle)."""
    app.setStyle("Fusion")
    qp = QPalette()
    roles = QPalette.ColorRole
    for role, value in (
        (roles.Window, p.window), (roles.WindowText, p.text),
        (roles.Base, p.input), (roles.AlternateBase, p.surface),
        (roles.Text, p.text), (roles.Button, p.surface), (roles.ButtonText, p.text),
        (roles.Highlight, p.accent), (roles.HighlightedText, p.on_accent),
        (roles.ToolTipBase, p.surface), (roles.ToolTipText, p.text),
        (roles.PlaceholderText, p.muted), (roles.Link, p.accent),
    ):
        qp.setColor(role, QColor(value))
    app.setPalette(qp)
    app.setStyleSheet(stylesheet(p))
