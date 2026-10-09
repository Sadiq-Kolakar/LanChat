"""Colors, fonts and ttk theming. Uses the optional ``sv-ttk`` package when installed."""
from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk

IS_WINDOWS = sys.platform.startswith("win")
FONT_FAMILY = "Segoe UI" if IS_WINDOWS else "TkDefaultFont"

LIGHT = dict(
    bg="#f6f7f9", panel="#ffffff", sidebar="#eef0f4", text="#1f2430", muted="#6b7280",
    accent="#2563eb", on_accent="#ffffff", border="#d9dce3",
    online="#16a34a", connecting="#d97706", connected="#2563eb", wants="#9333ea", offline="#9ca3af",
    error="#dc2626", banner="#fff7e0", me="#2563eb", other="#0f766e",
)
DARK = dict(
    bg="#1c1e24", panel="#23262e", sidebar="#191b20", text="#e6e8ee", muted="#9aa1b2",
    accent="#60a5fa", on_accent="#0b1220", border="#343845",
    online="#4ade80", connecting="#fbbf24", connected="#60a5fa", wants="#c084fc", offline="#6b7280",
    error="#f87171", banner="#3a3320", me="#60a5fa", other="#5eead4",
)


ACCENT_STYLE = "TButton"  # becomes "Accent.TButton" when sv-ttk provides it


def font(size: int = 10, weight: str = "normal") -> tuple:
    return (FONT_FAMILY, size, weight)


def apply_theme(root: tk.Misc, dark: bool) -> dict[str, str]:
    """Apply light/dark theme; returns the palette for the plain-tk widgets."""
    style = ttk.Style(root)
    try:
        import sv_ttk  # type: ignore

        try:
            sv_ttk.set_theme("dark" if dark else "light", root)  # newer sv-ttk: explicit root
        except TypeError:
            sv_ttk.set_theme("dark" if dark else "light")
    except Exception:  # package missing or incompatible: plain ttk still works
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
    global ACCENT_STYLE
    ACCENT_STYLE = "Accent.TButton" if style.layout("Accent.TButton") else "TButton"
    palette = DARK if dark else LIGHT
    style.configure("Treeview", rowheight=30, borderwidth=0)
    style.configure("Title.TLabel", font=font(17, "bold"))
    style.configure("Sub.TLabel", font=font(9))
    style.configure("Section.TLabel", font=font(9, "bold"))
    style.configure("Header.TLabel", font=font(13, "bold"))
    return palette
