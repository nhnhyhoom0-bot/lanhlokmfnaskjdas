"""Small HTTP helpers built on urllib (no third-party packages)."""
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import VERSION

UA = "xlop-launcher/%s" % VERSION


class NetError(Exception):
    def __init__(self, msg, status=None, body=b""):
        super().__init__(msg)
        self.status = status
        self.body = body


def http(url, data=None, headers=None, method=None, timeout=30, retries=3):
    """Return (status, body). HTTP error statuses are returned, not raised."""
    hdrs = {"User-Agent": UA}
    hdrs.update(headers or {})
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()
            if exc.code >= 500 and attempt < retries - 1:
                time.sleep(1 + attempt)
                continue
            return exc.code, body
        except OSError as exc:          # URLError, timeouts, resets
            last = exc
            time.sleep(1 + attempt)
    raise NetError("تعذّر الاتصال بـ %s (%s)" % (url, last))


def get_json(url, headers=None, timeout=30):
    status, body = http(url, headers=headers, timeout=timeout)
    if status >= 400:
        raise NetError("HTTP %d من %s" % (status, url), status, body)
    return json.loads(body.decode("utf-8"))


def request_json(url, obj=None, headers=None, method=None, form=None, timeout=30):
    """POST/GET and parse a JSON reply. Returns (status, dict)."""
    hdrs = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif obj is not None:
        data = json.dumps(obj).encode()
        hdrs.setdefault("Content-Type", "application/json")
    hdrs.setdefault("Accept", "application/json")
    status, body = http(url, data=data, headers=hdrs,
                        method=method or ("POST" if data is not None else "GET"),
                        timeout=timeout)
    try:
        parsed = json.loads(body.decode("utf-8")) if body.strip() else {}
    except ValueError:
        parsed = {"raw": body[:300].decode("utf-8", "replace")}
    return status, parsed


def hash_file(path, algo="sha1"):
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _already_ok(dest, sha1, sha256, size):
    if not os.path.isfile(dest):
        return False
    if size is not None:
        return os.path.getsize(dest) == size
    if sha1:
        return hash_file(dest, "sha1") == sha1
    if sha256:
        return hash_file(dest, "sha256") == sha256
    return True


def download(url, dest, sha1=None, sha256=None, size=None, retries=3, timeout=60):
    """Download url to dest atomically, verifying size/hash. Skips valid files."""
    if _already_ok(dest, sha1, sha256, size):
        return dest
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = "%s.part.%d" % (dest, os.getpid())
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            h1, h2 = hashlib.sha1(), hashlib.sha256()
            total = 0
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as out:
                for chunk in iter(lambda: resp.read(1 << 16), b""):
                    out.write(chunk)
                    h1.update(chunk)
                    h2.update(chunk)
                    total += len(chunk)
            if size is not None and total != size:
                raise NetError("حجم غير متطابق لـ %s (%d بدل %d)" % (url, total, size))
            if sha1 and h1.hexdigest() != sha1:
                raise NetError("فشل التحقق من SHA1 لـ %s" % url)
            if sha256 and h2.hexdigest() != sha256:
                raise NetError("فشل التحقق من SHA256 لـ %s" % url)
            os.replace(tmp, dest)
            return dest
        except (OSError, NetError) as exc:
            last = exc
            if os.path.exists(tmp):
                os.remove(tmp)
            time.sleep(1 + attempt)
    raise NetError("فشل تنزيل %s: %s" % (url, last))
