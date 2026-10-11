"""Skins: PNG handling, local skin server for offline accounts, Microsoft upload."""
import base64
import hashlib
import json
import os
import struct
import threading
import time
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import VERSION, urls
from .net import NetError, download, get_json, http, request_json
from .paths import data_dir

PNG_SIG = b"\x89PNG\r\n\x1a\n"


class SkinError(Exception):
    pass


# ----------------------------------------------------------------- PNG codec

def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else (b if pb <= pc else c)


def decode_png(data):
    """Decode a non-interlaced 8/16-bit PNG to (width, height, RGBA bytes)."""
    if data[:8] != PNG_SIG:
        raise SkinError("الملف ليس صورة PNG")
    pos, idat, plte, trns, ihdr = 8, [], b"", b"", None
    while pos + 8 <= len(data):
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", chunk)
        elif ctype == b"IDAT":
            idat.append(chunk)
        elif ctype == b"PLTE":
            plte = chunk
        elif ctype == b"tRNS":
            trns = chunk
        elif ctype == b"IEND":
            break
    if not ihdr or not idat:
        raise SkinError("ملف PNG تالف")
    w, h, depth, color, _, _, interlace = ihdr
    if interlace:
        raise SkinError("صور PNG المتداخلة (interlaced) غير مدعومة، أعد حفظ الصورة")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color)
    if channels is None or (color == 3 and depth != 8) or (color != 3 and depth not in (8, 16)):
        raise SkinError("نوع PNG غير مدعوم، احفظه بصيغة RGBA عادية")
    bpp = channels * (depth // 8)
    stride = w * bpp
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error:
        raise SkinError("ملف PNG تالف")
    if len(raw) < (stride + 1) * h:
        raise SkinError("ملف PNG ناقص")
    rows, prev = [], bytearray(stride)
    for y in range(h):
        ft = raw[y * (stride + 1)]
        cur = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if ft == 1:
            for i in range(bpp, stride):
                cur[i] = (cur[i] + cur[i - bpp]) & 255
        elif ft == 2:
            for i in range(stride):
                cur[i] = (cur[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                left = cur[i - bpp] if i >= bpp else 0
                cur[i] = (cur[i] + ((left + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = cur[i - bpp] if i >= bpp else 0
                c = prev[i - bpp] if i >= bpp else 0
                cur[i] = (cur[i] + _paeth(a, prev[i], c)) & 255
        elif ft != 0:
            raise SkinError("ملف PNG تالف")
        rows.append(cur)
        prev = cur
    step = depth // 8
    out = bytearray()
    for cur in rows:
        samples = cur if step == 1 else cur[::2]          # 16-bit -> keep high byte
        if color == 6:
            out += samples
        elif color == 2:
            for i in range(0, len(samples), 3):
                out += samples[i:i + 3] + b"\xff"
        elif color == 0:
            for v in samples:
                out += bytes((v, v, v, 255))
        elif color == 4:
            for i in range(0, len(samples), 2):
                v = samples[i]
                out += bytes((v, v, v, samples[i + 1]))
        else:   # palette
            for idx in samples:
                r, g, b = plte[idx * 3:idx * 3 + 3]
                out += bytes((r, g, b, trns[idx] if idx < len(trns) else 255))
    return w, h, bytes(out)


def encode_png(w, h, rgba):
    def chunk(tag, payload):
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))
    return (PNG_SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def normalize_skin(data):
    """Accept any PNG skin (64x64, 64x32, or HD multiples) -> (png64, w, h, rgba)."""
    w, h, rgba = decode_png(data)
    if w >= 64 and w % 64 == 0 and h * 2 in (w, w * 2) and h in (w, w // 2):
        k = w // 64
        if k > 1:                                     # HD skin: nearest-neighbour downscale
            nw, nh = 64, h // k
            out = bytearray()
            for y in range(nh):
                for x in range(nw):
                    i = ((y * k) * w + x * k) * 4
                    out += rgba[i:i + 4]
            w, h, rgba = nw, nh, bytes(out)
    if (w, h) not in ((64, 64), (64, 32)):
        raise SkinError("مقاس السكن غير صحيح (%d×%d). المقاسات المدعومة: 64×64 أو 64×32" % (w, h))
    return encode_png(w, h, rgba), w, h, rgba


def detect_slim(w, h, rgba):
    """Alex-style (slim) skins leave the last arm column transparent."""
    if h != 64:
        return False
    for y in range(20, 32):
        for x in (54, 55):
            if rgba[(y * w + x) * 4 + 3] != 0:
                return False
    return True


def resolve_model(model, w, h, rgba):
    if model in ("classic", "slim"):
        return model
    return "slim" if detect_slim(w, h, rgba) else "classic"


def preview_rgba(w, h, rgba, model="classic"):
    """Front view of the character as a 16x32 RGBA bitmap (base layer + overlay)."""
    arm = 3 if model == "slim" else 4
    canvas = bytearray(16 * 32 * 4)

    def px(x, y):
        i = (y * w + x) * 4
        return rgba[i:i + 4]

    def blit(sx, sy, sw, sh, dx, dy, mirror=False):
        for j in range(sh):
            for i in range(sw):
                sxx = sx + (sw - 1 - i if mirror else i)
                r, g, b, a = px(sxx, sy + j)
                if a == 0:
                    continue
                o = ((dy + j) * 16 + dx + i) * 4
                if a == 255:
                    canvas[o:o + 4] = bytes((r, g, b, 255))
                else:
                    br, bg, bb, ba = canvas[o:o + 4]
                    ia = a / 255.0
                    canvas[o:o + 4] = bytes((int(r * ia + br * (1 - ia)), int(g * ia + bg * (1 - ia)),
                                             int(b * ia + bb * (1 - ia)), max(a, ba)))

    legacy = h == 32
    # base layer
    blit(8, 8, 8, 8, 4, 0)                                   # head
    blit(20, 20, 8, 12, 4, 8)                                # body
    blit(44, 20, arm, 12, 4 - arm, 8)                        # right arm (viewer's left)
    blit(4, 20, 4, 12, 4, 20)                                # right leg
    if legacy:
        blit(44, 20, arm, 12, 12, 8, mirror=True)
        blit(4, 20, 4, 12, 8, 20, mirror=True)
    else:
        blit(36, 52, arm, 12, 12, 8)                         # left arm
        blit(20, 52, 4, 12, 8, 20)                           # left leg
        # overlay layer
        blit(20, 36, 8, 12, 4, 8)
        blit(44, 36, arm, 12, 4 - arm, 8)
        blit(52, 52, arm, 12, 12, 8)
        blit(4, 36, 4, 12, 4, 20)
        blit(4, 52, 4, 12, 8, 20)
    blit(40, 8, 8, 8, 4, 0)                                  # hat layer
    return 16, 32, bytes(canvas)


# ----------------------------------------------------------------- local skin server

class SkinServer:
    """Tiny Yggdrasil-style server so authlib-injector can show offline skins."""

    def __init__(self):
        self.profiles = {}      # uuid32 -> dict
        self.textures = {}      # hash -> png bytes
        self.httpd = None
        self.port = 0

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port

    def add(self, uuid32, name, png, model="classic"):
        digest = hashlib.sha256(png).hexdigest()[:40]
        self.textures[digest] = png
        self.profiles[uuid32] = {"name": name, "hash": digest, "model": model}

    def _profile_json(self, uuid32):
        p = self.profiles.get(uuid32)
        if p is None:
            return None
        skin = {"url": "%s/textures/%s" % (self.base, p["hash"])}
        if p["model"] == "slim":
            skin["metadata"] = {"model": "slim"}
        value = {"timestamp": int(time.time() * 1000), "profileId": uuid32,
                 "profileName": p["name"], "textures": {"SKIN": skin}}
        return {"id": uuid32, "name": p["name"], "properties": [{
            "name": "textures",
            "value": base64.b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()}]}

    def start(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, code, obj):
                self._send(code, json.dumps(obj, separators=(",", ":")).encode())

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path in ("/", ""):
                    return self._json(200, {
                        "meta": {"serverName": "xlop offline skins", "implementationName": "xlop-launcher",
                                 "implementationVersion": VERSION},
                        "skinDomains": ["127.0.0.1", "localhost"]})
                if path.startswith("/sessionserver/session/minecraft/profile/"):
                    prof = server._profile_json(path.rsplit("/", 1)[1].replace("-", ""))
                    return self._json(200, prof) if prof else self._send(204)
                if path.startswith("/textures/"):
                    png = server.textures.get(path.rsplit("/", 1)[1])
                    return self._send(200, png, "image/png") if png else self._json(404, {"error": "NotFound"})
                if path.startswith("/sessionserver/session/minecraft/hasJoined"):
                    return self._send(204)
                self._json(404, {"error": "NotFoundOperationException"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                path = self.path.split("?", 1)[0]
                if path == "/api/profiles/minecraft":
                    try:
                        names = json.loads(body.decode() or "[]")
                    except ValueError:
                        names = []
                    found = [{"id": u, "name": p["name"]} for u, p in server.profiles.items()
                             if p["name"] in names]
                    return self._json(200, found)
                if path.endswith("/session/minecraft/join"):
                    return self._send(204)
                self._json(404, {"error": "NotFoundOperationException"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self.base

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


# ----------------------------------------------------------------- authlib-injector

def ensure_authlib_injector(progress=None):
    dest = os.path.join(data_dir(), "tools", "authlib-injector.jar")
    if os.path.isfile(dest) and os.path.getsize(dest) > 10000:
        return dest
    last = None
    for meta_url in urls.AUTHLIB_LATEST:
        try:
            meta = get_json(meta_url, timeout=20)
            sha256 = (meta.get("checksums") or {}).get("sha256")
            if progress:
                progress("تنزيل authlib-injector %s" % meta.get("version", ""))
            download(meta["download_url"], dest, sha256=sha256)
            return dest
        except (NetError, KeyError, ValueError) as exc:
            last = exc
    raise SkinError("تعذّر تنزيل authlib-injector: %s" % last)


# ----------------------------------------------------------------- Microsoft skin API

def upload_skin(access_token, png, model):
    boundary = "----xlop" + uuid.uuid4().hex
    parts = []
    for name, value in (("variant", model),):
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                      % (boundary, name, value)).encode())
    parts.append(('--%s\r\nContent-Disposition: form-data; name="file"; filename="skin.png"\r\n'
                  'Content-Type: image/png\r\n\r\n' % boundary).encode() + png + b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode())
    status, body = http(urls.MC_SKINS, data=b"".join(parts), method="POST", headers={
        "Authorization": "Bearer " + access_token,
        "Content-Type": "multipart/form-data; boundary=" + boundary}, timeout=60)
    if status not in (200, 204):
        raise SkinError("رفضت Mojang السكن (HTTP %d): %s" % (status, body[:200].decode("utf-8", "replace")))


def reset_skin(access_token):
    status, _ = http(urls.MC_SKIN_ACTIVE, method="DELETE",
                     headers={"Authorization": "Bearer " + access_token})
    if status not in (200, 204):
        raise SkinError("تعذّر إعادة السكن الافتراضي (HTTP %d)" % status)


# ----------------------------------------------------------------- fetch a skin by player name

def fetch_skin_by_name(name):
    """Download the current public skin of a premium player. Returns (png, model)."""
    prof = None
    for url in (urls.MC_LOOKUP_NAME, urls.MOJANG_LOOKUP_NAME):
        status, obj = request_json(url.format(name=name))
        if status == 200 and obj.get("id"):
            prof = obj
            break
    if prof is None:
        raise SkinError("لا يوجد لاعب بهذا الاسم: %s" % name)
    status, obj = request_json(urls.SESSION_PROFILE.format(uuid=prof["id"]))
    if status != 200:
        raise SkinError("تعذّر جلب ملف اللاعب (HTTP %d)، حاول بعد قليل" % status)
    for prop in obj.get("properties", []):
        if prop.get("name") == "textures":
            tex = json.loads(base64.b64decode(prop["value"]).decode())
            skin = tex.get("textures", {}).get("SKIN")
            if not skin:
                raise SkinError("هذا اللاعب يستخدم السكن الافتراضي")
            status, png = http(skin["url"])
            if status != 200:
                raise SkinError("تعذّر تنزيل صورة السكن")
            model = (skin.get("metadata") or {}).get("model", "classic")
            return png, "slim" if model == "slim" else "classic"
    raise SkinError("لم يُعثر على سكن لهذا اللاعب")


def save_account_skin(acc_id, png):
    folder = os.path.join(data_dir(), "skins")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, acc_id.replace(":", "_") + ".png")
    with open(path, "wb") as fh:
        fh.write(png)
    return path
