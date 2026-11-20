#!/usr/bin/python3
"""
TokenWatcher Desktop Runner (GTK4 + WebKitGTK 6.0).
Charge l'interface ui/index.html (V4.1.0 MULTI-ENGINE).
Modes CLI :
  - défaut (sans argument) : mode production (sans contrôles de test/topologie)
  - --preprod              : affiche les pastilles de prévisualisation des topologies
"""
import argparse
import os
import sys
from pathlib import Path

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
    # Si lancé depuis un venv isolé (ex: venv Hermes ou uv), rattacher dist-packages système
    sys_dist = "/usr/lib/python3/dist-packages"
    if sys_dist not in sys.path and Path(sys_dist).exists():
        sys.path.append(sys_dist)
    try:
        import gi  # type: ignore[import-untyped]
    except ModuleNotFoundError:
        if Path("/usr/bin/python3").exists() and Path(sys.executable).resolve() != Path("/usr/bin/python3").resolve():
            os.execv("/usr/bin/python3", ["/usr/bin/python3", *sys.argv])
        raise

gi.require_version('Gtk', '4.0')
gi.require_version('WebKit', '6.0')
from gi.repository import Gtk, WebKit  # type: ignore[import-untyped]


class TokenWatcherApp(Gtk.Application):
    def __init__(self, preprod: bool = False) -> None:
        app_id = 'io.thomas.tokenwatcher.widget.preprod' if preprod else 'io.thomas.tokenwatcher.widget'
        super().__init__(application_id=app_id)
        self.preprod = preprod

    def do_activate(self) -> None:
        title = "TokenWatcher Cockpit V4.1.0 [PREPROD]" if self.preprod else "TokenWatcher Cockpit V4.1.0"
        win = Gtk.ApplicationWindow(
            application=self,
            title=title
        )
        win.set_default_size(980, 920)

        webview = WebKit.WebView()
        settings = webview.get_settings()
        settings.set_allow_file_access_from_file_urls(True)
        settings.set_allow_universal_access_from_file_urls(True)

        ui_path = Path(__file__).resolve().parent.parent / "ui" / "index.html"
        uri = f"file://{ui_path}?mode=preprod" if self.preprod else f"file://{ui_path}"
        webview.load_uri(uri)

        win.set_child(webview)
        win.present()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TokenWatcher GTK4 Cockpit V4.1.0")
    parser.add_argument(
        "--preprod",
        action="store_true",
        help="Active le mode préproduction (affiche les pastilles de sélection de topologie).",
    )
    args, gtk_args = parser.parse_known_args()
    app = TokenWatcherApp(preprod=args.preprod)
    sys.exit(app.run([sys.argv[0], *gtk_args]))
