#!/usr/bin/python3
"""
TokenWatcher Desktop Runner (GTK4 + WebKitGTK 6.0).
Charge l'interface ui/index.html (V4.0.2 MULTI-ENGINE).
"""
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
    def __init__(self) -> None:
        super().__init__(application_id='io.thomas.tokenwatcher.widget')

    def do_activate(self) -> None:
        win = Gtk.ApplicationWindow(
            application=self,
            title="TokenWatcher Cockpit V4.0.2"
        )
        win.set_default_size(980, 920)

        webview = WebKit.WebView()
        settings = webview.get_settings()
        settings.set_allow_file_access_from_file_urls(True)
        settings.set_allow_universal_access_from_file_urls(True)

        ui_path = Path(__file__).resolve().parent.parent / "ui" / "index.html"
        webview.load_uri(f"file://{ui_path}")

        win.set_child(webview)
        win.present()


if __name__ == "__main__":
    app = TokenWatcherApp()
    sys.exit(app.run(sys.argv))
