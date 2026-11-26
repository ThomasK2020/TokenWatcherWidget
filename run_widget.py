#!/usr/bin/env python3
"""
Point d'entrée principal du Widget TokenWatcher (GTK4 + WebKitGTK 6.0).
Usage :
  - python3 run_widget.py            -> charge file://.../ui/index.html?mode=prod
  - python3 run_widget.py --preprod  -> charge file://.../ui/index.html?mode=preprod
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

WIDGET_ROOT = Path(__file__).resolve().parent

local_typelib = str(Path.home() / ".local" / "lib" / "girepository-1.0")
if Path(local_typelib).is_dir():
    existing_typelib = os.environ.get("GI_TYPELIB_PATH", "")
    if local_typelib not in existing_typelib.split(":"):
        os.environ["GI_TYPELIB_PATH"] = (
            f"{local_typelib}:{existing_typelib}" if existing_typelib else local_typelib
        )

try:
    import gi  # type: ignore[import-untyped]
except ModuleNotFoundError:
    sys_dist = "/usr/lib/python3/dist-packages"
    if sys_dist not in sys.path and Path(sys_dist).exists():
        sys.path.append(sys_dist)
    try:
        import gi  # type: ignore[import-untyped]
    except ModuleNotFoundError:
        if Path("/usr/bin/python3").exists() and Path(sys.executable).resolve() != Path("/usr/bin/python3").resolve():
            os.execv("/usr/bin/python3", ["/usr/bin/python3", *sys.argv])
        raise

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Gtk, WebKit  # type: ignore[import-untyped]


class TokenWatcherRunnerApp(Gtk.Application):
    def __init__(self, preprod: bool = False) -> None:
        app_id = "io.thomas.tokenwatcher.widget.preprod" if preprod else "io.thomas.tokenwatcher.widget"
        super().__init__(application_id=app_id)
        self.preprod = preprod

    def do_activate(self) -> None:
        title = "TokenWatcher Cockpit V4.2.5 [PREPROD]" if self.preprod else "TokenWatcher Cockpit V4.2.5"
        win = Gtk.ApplicationWindow(application=self, title=title)
        win.set_default_size(980, 960)

        webview = WebKit.WebView()
        settings = webview.get_settings()
        settings.set_allow_file_access_from_file_urls(True)
        settings.set_allow_universal_access_from_file_urls(True)

        ui_path = WIDGET_ROOT / "ui" / "index.html"
        mode_param = "preprod" if self.preprod else "prod"
        webview.load_uri(f"file://{ui_path}?mode={mode_param}")

        win.set_child(webview)
        win.present()


def main() -> int:
    parser = argparse.ArgumentParser(description="TokenWatcher GTK4 Cockpit Runner V4.2.5")
    parser.add_argument(
        "--preprod",
        action="store_true",
        help="Charge ui/index.html?mode=preprod avec les contrôles de simulation interactifs.",
    )
    args, gtk_args = parser.parse_known_args()
    app = TokenWatcherRunnerApp(preprod=args.preprod)
    return int(app.run([sys.argv[0], *gtk_args]))


if __name__ == "__main__":
    sys.exit(main())
