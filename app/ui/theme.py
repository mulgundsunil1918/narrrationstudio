"""Design system: tokens, typography and stylesheet for both appearances.

One palette object drives every colour in the app, so light and dark are the
same layout with different tokens rather than two stylesheets kept in sync by
hand. The accent is a restrained clinical blue — trustworthy and technical, not
a children's palette.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from PySide6.QtGui import QColor, QFont, QFontDatabase
from PySide6.QtWidgets import QApplication


class Appearance(str, Enum):
    DARK = "dark"
    LIGHT = "light"


@dataclass(frozen=True)
class Palette:
    name: str
    bg: str
    surface: str
    surface_alt: str
    elevated: str
    hover: str
    border: str
    border_strong: str
    text: str
    text_dim: str
    text_faint: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_soft: str
    accent_text: str
    success: str
    success_soft: str
    warning: str
    warning_soft: str
    danger: str
    danger_soft: str
    neutral: str
    neutral_soft: str
    selection: str
    shadow: str


DARK = Palette(
    name="dark",
    bg="#0D0E12",
    surface="#14161B",
    surface_alt="#191C22",
    elevated="#20242C",
    hover="#272C36",
    border="#242832",
    border_strong="#333944",
    text="#EDEFF3",
    text_dim="#9BA3B0",
    text_faint="#646C7A",
    accent="#4C7DFF",
    accent_hover="#6892FF",
    accent_pressed="#3A68E0",
    accent_soft="#1A2440",
    accent_text="#FFFFFF",
    success="#35C48D",
    success_soft="#0F2A21",
    warning="#E0A03A",
    warning_soft="#2C2211",
    danger="#F0575D",
    danger_soft="#331519",
    neutral="#7C8593",
    neutral_soft="#1D2029",
    selection="#22314F",
    shadow="rgba(0,0,0,0.45)",
)

LIGHT = Palette(
    name="light",
    bg="#F6F7F9",
    surface="#FFFFFF",
    surface_alt="#F1F3F6",
    elevated="#FFFFFF",
    hover="#EDEFF3",
    border="#E3E6EB",
    border_strong="#CDD3DB",
    text="#14171C",
    text_dim="#5B636F",
    text_faint="#8B939F",
    accent="#2F62E8",
    accent_hover="#4275F5",
    accent_pressed="#2551C9",
    accent_soft="#E8EEFD",
    accent_text="#FFFFFF",
    success="#12875C",
    success_soft="#E4F5ED",
    warning="#9A6410",
    warning_soft="#FBF0DC",
    danger="#C6363C",
    danger_soft="#FCE9EA",
    neutral="#6B7280",
    neutral_soft="#EFF1F4",
    selection="#DCE6FD",
    shadow="rgba(15,20,30,0.10)",
)

_current: Palette = DARK


def palette() -> Palette:
    return _current


def set_appearance(appearance: Appearance) -> Palette:
    global _current
    _current = DARK if appearance is Appearance.DARK else LIGHT
    return _current


# -- typography ----------------------------------------------------------

DISPLAY = 28
TITLE = 20
HEADING = 15
BODY = 13
SMALL = 12
CAPTION = 11


def ui_font(size: int = BODY, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    font.setPointSize(size)
    font.setWeight(weight)
    return font


def mono_font(size: int = SMALL, bold: bool = False) -> QFont:
    """A fixed-width font that exists on the platform in use."""
    from app.utils.platform import monospace_families

    families = monospace_families()
    font = QFont(families[0], size)
    for family in families:
        candidate = QFont(family, size)
        if candidate.exactMatch():
            font = candidate
            break
    font.setFamilies(families)
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setBold(bold)
    return font


def qcolor(value: str, alpha: float = 1.0) -> QColor:
    colour = QColor(value)
    if alpha < 1.0:
        colour.setAlphaF(alpha)
    return colour


STATUS_TONES = {
    "pending": ("neutral", "neutral_soft"),
    "queued": ("accent", "accent_soft"),
    "generating": ("accent", "accent_soft"),
    "generated": ("success", "success_soft"),
    "needs_regen": ("warning", "warning_soft"),
    "failed": ("danger", "danger_soft"),
    "skipped": ("text_faint", "neutral_soft"),
}


def tone(kind: str) -> tuple[str, str]:
    """Return (foreground, background) hex for a semantic tone name."""
    current = palette()
    mapping = {
        "info": (current.accent, current.accent_soft),
        "success": (current.success, current.success_soft),
        "warning": (current.warning, current.warning_soft),
        "error": (current.danger, current.danger_soft),
        "neutral": (current.neutral, current.neutral_soft),
    }
    return mapping.get(kind, mapping["neutral"])


def apply_theme(application: QApplication, appearance: Appearance = Appearance.DARK) -> None:
    """Apply tokens and stylesheet to the whole application."""
    current = set_appearance(appearance)
    application.setStyle("Fusion")
    application.setFont(ui_font())
    application.setStyleSheet(stylesheet(current))


def stylesheet(p: Palette) -> str:
    return f"""
QWidget {{
    background-color: transparent;
    color: {p.text};
    selection-background-color: {p.selection};
    selection-color: {p.text};
}}
QMainWindow, QDialog {{ background-color: {p.bg}; }}

QToolTip {{
    background-color: {p.elevated};
    color: {p.text};
    border: 1px solid {p.border_strong};
    padding: 7px 10px;
    border-radius: 7px;
    font-size: {SMALL}px;
}}

/* ---------- structure ---------- */

#Sidebar     {{ background-color: {p.surface}; border-right: 1px solid {p.border}; }}
#TopBar      {{ background-color: {p.surface}; border-bottom: 1px solid {p.border}; }}
#Workspace   {{ background-color: {p.bg}; }}
#RightPanel  {{ background-color: {p.surface}; border-left: 1px solid {p.border}; }}
#BottomBar   {{ background-color: {p.surface}; border-top: 1px solid {p.border}; }}

#Card {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    border-radius: 12px;
}}
#CardQuiet {{
    background-color: {p.surface_alt};
    border: 1px solid transparent;
    border-radius: 12px;
}}
#Divider {{ background-color: {p.border}; border: none; }}

/* ---------- typography ---------- */

#Display        {{ font-size: {DISPLAY}px; font-weight: 700; color: {p.text}; }}
#Title          {{ font-size: {TITLE}px;   font-weight: 600; color: {p.text}; }}
#Heading        {{ font-size: {HEADING}px; font-weight: 600; color: {p.text}; }}
#Body           {{ font-size: {BODY}px;    color: {p.text}; }}
#Muted          {{ font-size: {SMALL}px;   color: {p.text_dim}; }}
#Caption        {{ font-size: {CAPTION}px; color: {p.text_faint}; }}
#SectionLabel   {{
    font-size: {CAPTION}px; font-weight: 700; color: {p.text_faint};
    letter-spacing: 1.2px;
}}
#Metric         {{ font-size: 24px; font-weight: 700; color: {p.text}; }}
#MetricLabel    {{ font-size: {CAPTION}px; color: {p.text_faint}; }}

/* ---------- buttons ---------- */

QPushButton {{
    background-color: {p.elevated};
    color: {p.text};
    border: 1px solid {p.border_strong};
    border-radius: 9px;
    padding: 9px 16px;
    font-size: {SMALL}px;
    font-weight: 500;
}}
QPushButton:hover:!disabled   {{ background-color: {p.hover}; }}
QPushButton:pressed:!disabled {{ background-color: {p.surface_alt}; }}
QPushButton:disabled {{
    color: {p.text_faint};
    background-color: {p.surface_alt};
    border-color: {p.border};
}}
QPushButton:focus {{ border-color: {p.accent}; }}

QPushButton#Primary {{
    background-color: {p.accent};
    border-color: {p.accent};
    color: {p.accent_text};
    font-weight: 600;
    padding: 10px 20px;
}}
QPushButton#Primary:hover:!disabled   {{ background-color: {p.accent_hover}; }}
QPushButton#Primary:pressed:!disabled {{ background-color: {p.accent_pressed}; }}
QPushButton#Primary:disabled {{
    background-color: {p.surface_alt}; border-color: {p.border}; color: {p.text_faint};
}}

QPushButton#Ghost {{
    background-color: transparent;
    border: 1px solid transparent;
    color: {p.text_dim};
    padding: 7px 11px;
}}
QPushButton#Ghost:hover:!disabled {{ background-color: {p.hover}; color: {p.text}; }}
QPushButton#Ghost:disabled {{ color: {p.text_faint}; background: transparent; }}

QPushButton#Danger {{
    background-color: transparent;
    border: 1px solid {p.danger};
    color: {p.danger};
}}
QPushButton#Danger:hover:!disabled {{ background-color: {p.danger_soft}; }}

QPushButton#Nav {{
    background-color: transparent;
    border: none;
    border-radius: 9px;
    padding: 10px 13px;
    text-align: left;
    font-size: {BODY}px;
    color: {p.text_dim};
}}
QPushButton#Nav:hover:!checked {{ background-color: {p.surface_alt}; color: {p.text}; }}
QPushButton#Nav:checked {{
    background-color: {p.accent_soft}; color: {p.accent}; font-weight: 600;
}}
QPushButton#Nav:disabled {{ color: {p.text_faint}; }}

QPushButton#Chip {{
    background-color: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: 999px;
    padding: 5px 13px;
    font-size: {CAPTION}px;
    color: {p.text_dim};
}}
QPushButton#Chip:checked {{
    background-color: {p.accent_soft}; border-color: {p.accent}; color: {p.accent};
    font-weight: 600;
}}

/* ---------- inputs ---------- */

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox {{
    background-color: {p.bg};
    border: 1px solid {p.border_strong};
    border-radius: 9px;
    padding: 9px 11px;
    color: {p.text};
    font-size: {BODY}px;
    selection-background-color: {p.accent};
    selection-color: {p.accent_text};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {p.accent}; }}
QLineEdit:disabled, QPlainTextEdit:disabled {{
    color: {p.text_faint}; background-color: {p.surface_alt};
}}

QComboBox {{
    background-color: {p.elevated};
    border: 1px solid {p.border_strong};
    border-radius: 9px;
    padding: 8px 11px;
    font-size: {BODY}px;
    min-height: 18px;
}}
QComboBox:hover:!disabled {{ background-color: {p.hover}; }}
QComboBox:focus {{ border-color: {p.accent}; }}
QComboBox:disabled {{ color: {p.text_faint}; background-color: {p.surface_alt}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {p.text_dim};
    margin-right: 9px;
}}
QComboBox QAbstractItemView {{
    background-color: {p.elevated};
    border: 1px solid {p.border_strong};
    border-radius: 10px;
    padding: 5px;
    outline: none;
}}
QComboBox QAbstractItemView::item {{ padding: 8px 11px; border-radius: 6px; min-height: 20px; }}
QComboBox QAbstractItemView::item:selected {{ background-color: {p.accent_soft}; color: {p.accent}; }}

QCheckBox, QRadioButton {{ spacing: 9px; font-size: {SMALL}px; color: {p.text}; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {p.border_strong};
    background-color: {p.bg};
}}
QCheckBox::indicator {{ border-radius: 5px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background-color: {p.accent}; border-color: {p.accent};
}}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {p.accent}; }}

/* ---------- sliders ---------- */

QSlider::groove:horizontal {{
    height: 4px; background: {p.border_strong}; border-radius: 2px;
}}
QSlider::sub-page:horizontal {{ background: {p.accent}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: #FFFFFF;
    border: 1px solid {p.border_strong};
    width: 15px; height: 15px; margin: -6px 0; border-radius: 8px;
}}
QSlider::handle:horizontal:hover {{ border-color: {p.accent}; }}
QSlider::handle:horizontal:disabled {{ background: {p.text_faint}; }}

/* ---------- progress ---------- */

QProgressBar {{
    background-color: {p.surface_alt};
    border: none; border-radius: 4px; height: 7px; text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{ background-color: {p.accent}; border-radius: 4px; }}

/* ---------- scroll ---------- */

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
QScrollBar::handle:vertical {{
    background: {p.border_strong}; border-radius: 5px; min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.text_faint}; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
QScrollBar::handle:horizontal {{
    background: {p.border_strong}; border-radius: 5px; min-width: 32px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

/* ---------- menus ---------- */

QMenuBar {{ background-color: {p.surface}; }}
QMenuBar::item {{ padding: 6px 12px; background: transparent; }}
QMenuBar::item:selected {{ background-color: {p.hover}; border-radius: 6px; }}
QMenu {{
    background-color: {p.elevated};
    border: 1px solid {p.border_strong};
    border-radius: 10px; padding: 6px;
}}
QMenu::item {{ padding: 7px 28px 7px 22px; border-radius: 6px; }}
QMenu::item:selected {{ background-color: {p.accent_soft}; color: {p.accent}; }}
QMenu::item:disabled {{ color: {p.text_faint}; }}
QMenu::separator {{ height: 1px; background: {p.border}; margin: 6px 10px; }}

/* ---------- misc ---------- */

QSplitter::handle {{ background-color: {p.border}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QListWidget {{ background: transparent; border: none; outline: none; }}
QListWidget::item {{ border-radius: 9px; padding: 4px; }}
QListWidget::item:selected {{ background-color: {p.accent_soft}; }}
"""
