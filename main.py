"""LANchat entry point:  python main.py [--profile NAME] [--name NAME] [--port N]"""
from __future__ import annotations

import argparse
import logging
import sys
import tkinter as tk
from tkinter import messagebox

from lanchat import APP_NAME, __version__
from lanchat.engine import Engine
from lanchat.naming import sanitize_name
from lanchat.settings import load_settings, save_settings, settings_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="LANchat", description="Offline nearby-PC messaging")
    ap.add_argument("--profile", help="separate settings file - lets you run several LANchats on one PC")
    ap.add_argument("--name", help="display name for this run (overrides the saved one)")
    ap.add_argument("--port", type=int, help="preferred TCP port (default 50500; falls back if busy)")
    ap.add_argument("--debug", action="store_true", help="verbose logging")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    path = settings_path(args.profile)
    settings = load_settings(path)
    first_run = not path.exists()
    if args.name:
        settings.username = sanitize_name(args.name)
    elif args.profile and first_run:
        settings.username = sanitize_name(args.profile)
    if args.port:
        settings.tcp_port = args.port
    settings = settings.validated()
    if first_run and not args.name:
        save_settings(path, settings)  # remember the default name for next time

    from lanchat.ui.app import MainWindow  # imported late so --help works without a display

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Cannot open a window: {exc}", file=sys.stderr)
        return 1
    engine = Engine(settings)
    try:
        engine.start()
    except OSError as exc:
        messagebox.showerror(APP_NAME, f"LANchat could not start its network listener:\n\n{exc}")
        return 1
    MainWindow(root, engine, path)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
