"""Self-update: read a small JSON manifest, download the new .deb, verify it, install it."""
import glob
import os
import re
import shutil
import subprocess
import sys
import urllib.parse

from . import VERSION
from .accounts import _first_line
from .net import NetError, download, get_json
from .paths import data_dir, sysconf_path

# The launcher OWNER sets this once, e.g.
#   https://github.com/OWNER/REPO/releases/latest/download/latest.json
# (the same link always points at the newest release). Empty = updater disabled.
UPDATE_URL = ""
SYSTEM_URL_FILE = sysconf_path("update_url")
PACKAGE = "xlop-launcher"


class UpdateError(Exception):
    pass


def update_url():
    for value in (os.environ.get("XLOP_UPDATE_URL", ""), _first_line(SYSTEM_URL_FILE), UPDATE_URL):
        if value and value.strip():
            return value.strip()
    return ""


def version_key(v):
    m = re.match(r"^v?(\d+(?:\.\d+)*)", str(v).strip())
    if not m:
        raise UpdateError("رقم إصدار غير صالح: %r" % (v,))
    return tuple(int(x) for x in m.group(1).split("."))


def is_newer(remote, local=VERSION):
    a, b = version_key(remote), version_key(local)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def _safe_url(url):
    p = urllib.parse.urlparse(url)
    return p.scheme == "https" or (p.scheme == "http" and p.hostname in ("127.0.0.1", "localhost"))


def check(timeout=15):
    """Return info about a newer version, or None. Raises UpdateError on problems."""
    url = update_url()
    if not url:
        return None
    if not _safe_url(url):
        raise UpdateError("رابط التحديث يجب أن يبدأ بـ https")
    try:
        data = get_json(url, timeout=timeout)
    except (NetError, ValueError) as exc:
        raise UpdateError("تعذّر فحص التحديثات: %s" % exc)
    try:
        version = str(data["version"])
        deb_url = urllib.parse.urljoin(url, str(data["url"]))
        sha256 = str(data["sha256"]).lower()
        size = int(data["size"]) if data.get("size") else None
    except (KeyError, TypeError, ValueError, AttributeError):
        raise UpdateError("ملف معلومات التحديث غير صالح")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256) or not _safe_url(deb_url):
        raise UpdateError("ملف معلومات التحديث غير صالح (الرابط أو البصمة)")
    if not is_newer(version):
        return None
    notes = data.get("notes", "")
    if isinstance(notes, list):
        notes = "\n".join(str(n) for n in notes)
    win = data.get("windows_url")
    return {"version": version, "url": deb_url, "sha256": sha256, "size": size, "notes": str(notes),
            "windows_url": urllib.parse.urljoin(url, str(win)) if win else ""}


def _deb_field(path, field):
    try:
        out = subprocess.run(["dpkg-deb", "-f", path, field], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        raise UpdateError("الأداة dpkg-deb غير متوفرة. هذا اللانشر يعمل على Debian/Ubuntu وما يشبههما")
    if out.returncode != 0:
        raise UpdateError("الملف المنزّل ليس حزمة deb صالحة")
    return out.stdout.strip()


def download_update(info):
    """Download + verify (sha256, size, package name/version). Returns the .deb path."""
    folder = os.path.join(data_dir(), "updates")
    os.makedirs(folder, exist_ok=True)
    for old in glob.glob(os.path.join(folder, "*.deb*")):
        try:
            os.remove(old)
        except OSError:
            pass
    safe_ver = re.sub(r"[^0-9A-Za-z.+~-]", "_", info["version"])
    dest = os.path.join(folder, "%s_%s_all.deb" % (PACKAGE, safe_ver))
    try:
        download(info["url"], dest, sha256=info["sha256"], size=info["size"])
    except NetError as exc:
        raise UpdateError("فشل تنزيل التحديث: %s" % exc)
    try:
        if _deb_field(dest, "Package") != PACKAGE:
            raise UpdateError("الحزمة المنزّلة ليست %s" % PACKAGE)
        if _deb_field(dest, "Version") != info["version"]:
            raise UpdateError("رقم إصدار الحزمة لا يطابق المعلن عنه")
    except UpdateError:
        os.remove(dest)
        raise
    return dest


def install_command(path):
    apt = ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y",
           "-o", "Dpkg::Options::=--force-confold", path]
    if os.geteuid() == 0:
        return apt
    if shutil.which("pkexec"):
        return ["pkexec"] + apt
    return None


def install(path):
    cmd = install_command(path)
    if cmd is None:
        raise UpdateError("لا توجد أداة pkexec. ثبّت يدويًا بالأمر:\nsudo apt install %s" % path)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        raise UpdateError("تعذّر تشغيل أداة التثبيت: %s" % exc)
    if r.returncode in (126, 127) and cmd[0] == "pkexec":
        raise UpdateError("لم يتم إدخال كلمة مرور النظام، أُلغي التحديث")
    if r.returncode != 0:
        raise UpdateError("فشل تثبيت التحديث:\n%s" % ((r.stderr or r.stdout).strip()[-700:]))


def restart():
    """Replace this process with the freshly installed launcher."""
    os.execv(sys.executable, [sys.executable, os.path.realpath(sys.argv[0])] + sys.argv[1:])
