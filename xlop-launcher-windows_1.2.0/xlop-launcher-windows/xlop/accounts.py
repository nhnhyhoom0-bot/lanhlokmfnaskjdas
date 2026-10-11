"""Accounts: offline, Ely.by (Yggdrasil) and Microsoft (device-code flow)."""
import hashlib
import re
import time
import uuid

from . import urls
from .net import NetError, request_json
from .paths import data_dir, json_load, json_save, sysconf_path
import os

OFFLINE, MICROSOFT, ELYBY = "offline", "microsoft", "elyby"
TYPE_LABELS = {OFFLINE: "أوفلاين", MICROSOFT: "Microsoft", ELYBY: "Ely.by"}
NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")


class AuthError(Exception):
    pass


class TwoFactorRequired(AuthError):
    pass


class NotConfigured(AuthError):
    """No Azure client ID available (the launcher owner has not set one yet)."""


# ----------------------------------------------------------------- offline

def offline_uuid(name):
    """Same UUID the game/servers derive for an offline player name."""
    d = bytearray(hashlib.md5(("OfflinePlayer:" + name).encode("utf-8")).digest())
    d[6] = (d[6] & 0x0F) | 0x30      # version 3
    d[8] = (d[8] & 0x3F) | 0x80      # IETF variant
    return uuid.UUID(bytes=bytes(d)).hex


def new_offline_account(name):
    name = name.strip()
    if not NAME_RE.match(name):
        raise AuthError("الاسم يجب أن يكون من 1 إلى 16 حرفًا (إنجليزية وأرقام و _ فقط)")
    u = offline_uuid(name)
    return {"id": "%s:%s" % (OFFLINE, u), "type": OFFLINE, "name": name, "uuid": u,
            "skin_path": "", "skin_model": "auto"}


# ----------------------------------------------------------------- store

class AccountStore:
    def __init__(self, path=None):
        self.path = path or os.path.join(data_dir(), "accounts.json")
        self.accounts = json_load(self.path, {"accounts": []}).get("accounts", [])

    def save(self):
        json_save(self.path, {"accounts": self.accounts}, private=True)   # holds tokens

    def get(self, acc_id):
        for a in self.accounts:
            if a["id"] == acc_id:
                return a
        return None

    def add(self, acc):
        old = self.get(acc["id"])
        if old is not None:
            self.accounts[self.accounts.index(old)] = acc
        else:
            self.accounts.append(acc)
        self.save()
        return acc

    def remove(self, acc_id):
        self.accounts = [a for a in self.accounts if a["id"] != acc_id]
        self.save()


# ----------------------------------------------------------------- Ely.by

def _ely_account(obj, client_token):
    prof = obj.get("selectedProfile") or (obj.get("availableProfiles") or [{}])[0]
    if not prof.get("id"):
        raise AuthError("لا يوجد ملف لاعب في حساب Ely.by")
    return {"id": "%s:%s" % (ELYBY, prof["id"]), "type": ELYBY, "name": prof["name"],
            "uuid": prof["id"], "access_token": obj["accessToken"],
            "client_token": client_token}


def ely_login(username, password, totp=None):
    client_token = uuid.uuid4().hex
    pw = password + (":" + totp.strip() if totp and totp.strip() else "")
    status, obj = request_json(urls.ELY_AUTH + "/auth/authenticate", {
        "username": username, "password": pw, "clientToken": client_token,
        "requestUser": True})
    if status == 200:
        return _ely_account(obj, client_token)
    msg = obj.get("errorMessage") or obj.get("error") or "HTTP %d" % status
    if status == 401 and "two factor" in msg.lower():
        raise TwoFactorRequired("الحساب محمي بالمصادقة الثنائية، أدخل الرمز")
    if status in (401, 403):
        raise AuthError("اسم المستخدم أو كلمة المرور غير صحيحة (%s)" % msg)
    raise AuthError("فشل تسجيل الدخول إلى Ely.by: %s" % msg)


def ely_ensure_fresh(acc, store=None):
    """Validate the token; refresh it if expired. Raises AuthError if impossible."""
    status, _ = request_json(urls.ELY_AUTH + "/auth/validate",
                             {"accessToken": acc["access_token"]})
    if status == 204 or status == 200:
        return acc
    status, obj = request_json(urls.ELY_AUTH + "/auth/refresh", {
        "accessToken": acc["access_token"], "clientToken": acc["client_token"]})
    if status != 200:
        raise AuthError("انتهت صلاحية جلسة Ely.by، سجّل الدخول من جديد")
    acc["access_token"] = obj["accessToken"]
    if store:
        store.save()
    return acc


# ----------------------------------------------------------------- Microsoft

MS_SCOPE = "XboxLive.signin offline_access"

# The launcher OWNER puts the Azure "Application (client) ID" that Mojang approved here, once.
# It is a public identifier (safe to ship). End users never see or type it.
BUILTIN_CLIENT_ID = ""
SYSTEM_ID_FILE = sysconf_path("microsoft_client_id")


def _first_line(path):
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except OSError:
        pass
    return ""


def microsoft_client_id(cfg=None):
    """Priority: advanced setting > $XLOP_MS_CLIENT_ID > /etc file > built-in constant."""
    for value in ((cfg or {}).get("ms_client_id", ""), os.environ.get("XLOP_MS_CLIENT_ID", ""),
                  _first_line(SYSTEM_ID_FILE), BUILTIN_CLIENT_ID):
        if value and value.strip():
            return value.strip()
    return ""
XERR = {
    2148916233: "هذا الحساب لا يملك حساب Xbox. أنشئ واحدًا من xbox.com ثم أعد المحاولة",
    2148916235: "خدمة Xbox غير متاحة في بلدك",
    2148916236: "الحساب يحتاج إلى التحقق من العمر (البالغين)",
    2148916237: "الحساب يحتاج إلى التحقق من العمر (البالغين)",
    2148916238: "الحساب تابع لطفل ويجب إضافته إلى عائلة Microsoft أولًا",
}


class MicrosoftAuth:
    def __init__(self, client_id):
        if not client_id:
            raise NotConfigured("تسجيل الدخول بحساب Microsoft غير مفعّل بعد في هذا اللانشر")
        self.client_id = client_id

    def begin(self):
        status, obj = request_json(urls.MS_DEVICECODE,
                                   form={"client_id": self.client_id, "scope": MS_SCOPE})
        if status != 200:
            raise AuthError("تعذّر بدء تسجيل الدخول: %s" %
                            (obj.get("error_description") or obj.get("error") or status))
        return obj      # device_code, user_code, verification_uri, expires_in, interval

    def poll(self, info, cancelled=lambda: False):
        interval = int(info.get("interval", 5))
        deadline = time.time() + int(info.get("expires_in", 900))
        while time.time() < deadline:
            if cancelled():
                raise AuthError("أُلغي تسجيل الدخول")
            time.sleep(interval)
            status, obj = request_json(urls.MS_TOKEN, form={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": self.client_id, "device_code": info["device_code"]})
            if status == 200:
                return obj
            err = obj.get("error")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            if err == "authorization_declined":
                raise AuthError("تم رفض تسجيل الدخول من صفحة Microsoft")
            if err in ("expired_token", "bad_verification_code"):
                raise AuthError("انتهت صلاحية الرمز، ابدأ من جديد")
            raise AuthError("فشل تسجيل الدخول: %s" % (obj.get("error_description") or err))
        raise AuthError("انتهت مهلة الرمز، أعد المحاولة")

    def refresh(self, refresh_token):
        status, obj = request_json(urls.MS_TOKEN, form={
            "grant_type": "refresh_token", "client_id": self.client_id,
            "refresh_token": refresh_token, "scope": MS_SCOPE})
        if status != 200:
            raise AuthError("انتهت جلسة Microsoft، سجّل الدخول من جديد")
        return obj

    def finish(self, ms_tokens):
        """Microsoft token -> Xbox Live -> XSTS -> Minecraft token -> profile."""
        hdr = {"Accept": "application/json"}
        status, xbl = request_json(urls.XBL_AUTH, {
            "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com",
                           "RpsTicket": "d=" + ms_tokens["access_token"]},
            "RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT"}, headers=hdr)
        if status != 200:
            raise AuthError("فشل تسجيل الدخول إلى Xbox Live (%s)" % status)
        status, xsts = request_json(urls.XSTS_AUTH, {
            "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl["Token"]]},
            "RelyingParty": "rp://api.minecraftservices.com/", "TokenType": "JWT"}, headers=hdr)
        if status != 200:
            raise AuthError(XERR.get(xsts.get("XErr"), "رفضت Xbox الحساب (XErr=%s)" % xsts.get("XErr")))
        uhs = xsts["DisplayClaims"]["xui"][0]["uhs"]
        status, mc = request_json(urls.MC_LOGIN, {
            "identityToken": "XBL3.0 x=%s;%s" % (uhs, xsts["Token"])}, headers=hdr)
        if status != 200:
            if status == 403:
                raise AuthError("رفضت Mojang معرّف التطبيق (Invalid app registration). "
                                "يجب أن يكون الـ Client ID معتمدًا من Mojang للوصول إلى Minecraft API")
            raise AuthError("فشل تسجيل الدخول إلى Minecraft (%s)" % status)
        token = mc["access_token"]
        status, prof = request_json(urls.MC_PROFILE, headers={"Authorization": "Bearer " + token})
        if status == 404:
            raise AuthError("هذا الحساب لا يملك Minecraft: Java Edition")
        if status != 200:
            raise AuthError("تعذّر جلب ملف اللاعب (%s)" % status)
        return {"id": "%s:%s" % (MICROSOFT, prof["id"]), "type": MICROSOFT, "name": prof["name"],
                "uuid": prof["id"], "access_token": token,
                "refresh_token": ms_tokens.get("refresh_token", ""),
                "expires_at": int(time.time()) + int(mc.get("expires_in", 86400)) - 300}


def microsoft_ensure_fresh(acc, cfg, store=None):
    if acc.get("expires_at", 0) > time.time():
        return acc
    auth = MicrosoftAuth(microsoft_client_id(cfg))
    fresh = auth.finish(auth.refresh(acc["refresh_token"]))
    acc.update({k: fresh[k] for k in ("access_token", "refresh_token", "expires_at", "name")})
    if store:
        store.save()
    return acc
