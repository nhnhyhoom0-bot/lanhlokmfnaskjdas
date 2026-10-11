"""Locations and persistent settings."""
import json
import os
import sys

IS_WIN = sys.platform == "win32"

DEFAULT_CONFIG = {
    "selected_account": "",
    "selected_version": "",
    "show_snapshots": False,
    "show_old": False,
    "memory_mb": 2048,
    "java_path": "",
    "jvm_args": "",
    "game_root": "",            # empty -> ~/.minecraft
    "isolate": False,           # separate game folder per version
    "width": 854,
    "height": 480,
    "ms_client_id": "",
    "auto_update": True,
    "last_update_check": 0,
}


def app_dir():
    """Folder that holds the launcher (next to the .exe on Windows)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sysconf_path(name):
    """Owner-editable settings file: /etc/xlop-launcher/NAME on Linux, NAME.txt next to the app on Windows."""
    if IS_WIN:
        return os.path.join(app_dir(), name + ".txt")
    return "/etc/xlop-launcher/" + name


def _appdata():
    return os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")


def data_dir():
    if IS_WIN:
        base = _appdata()
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    path = os.path.join(base, "xlop-launcher")
    os.makedirs(path, exist_ok=True)
    return path


def json_load(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def json_save(path, obj, private=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class Config(dict):
    def __init__(self):
        super().__init__(DEFAULT_CONFIG)
        self.path = os.path.join(data_dir(), "config.json")
        self.update(json_load(self.path, {}))

    def save(self):
        json_save(self.path, dict(self))

    @property
    def root(self):
        if self["game_root"]:
            return self["game_root"]
        return os.path.join(_appdata() if IS_WIN else os.path.expanduser("~"), ".minecraft")

    def game_dir(self, version_id):
        if self["isolate"]:
            return os.path.join(data_dir(), "instances", version_id)
        return self.root
