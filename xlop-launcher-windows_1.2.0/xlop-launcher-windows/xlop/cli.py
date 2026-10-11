"""Command line entry point. With no arguments the GUI opens."""
import argparse
import getpass
import os
import shlex
import sys

from . import APP_NAME, VERSION
from .accounts import (ELYBY, OFFLINE, TYPE_LABELS, AccountStore, AuthError, MicrosoftAuth,
                       NotConfigured, TwoFactorRequired, ely_login, microsoft_client_id, new_offline_account)
from .game import GameError, launch, list_versions, load_manifest
from .paths import Config
from . import update as updater
from .net import NetError


def _pick_account(store, cfg, wanted):
    acct = store.get(wanted or cfg["selected_account"]) or (store.accounts[0] if store.accounts else None)
    if not acct:
        raise GameError("لا يوجد حساب. أضف واحدًا أولًا (add-offline / add-ely)")
    return acct


def main(argv=None):
    p = argparse.ArgumentParser(prog="xlop-launcher", description=APP_NAME)
    p.add_argument("--version", action="version", version="%s %s" % (APP_NAME, VERSION))
    sub = p.add_subparsers(dest="cmd")
    v = sub.add_parser("versions", help="list available Minecraft versions")
    v.add_argument("--snapshots", action="store_true")
    v.add_argument("--old", action="store_true", help="include old alpha/beta")
    sub.add_parser("accounts", help="list accounts")
    a = sub.add_parser("add-offline", help="add an offline account")
    a.add_argument("name")
    e = sub.add_parser("add-ely", help="add an Ely.by account")
    e.add_argument("username")
    e.add_argument("--totp", default=None)
    u = sub.add_parser("update", help="check for / install a new launcher version")
    u.add_argument("--check", action="store_true", help="only report, do not install")
    sub.add_parser("add-microsoft", help="add a Microsoft account (device code login)")
    r = sub.add_parser("remove", help="remove an account")
    r.add_argument("account_id")
    i = sub.add_parser("install", help="download a version without launching it")
    i.add_argument("version")
    l = sub.add_parser("launch", help="launch a version")
    l.add_argument("version")
    l.add_argument("--account")
    l.add_argument("--dry-run", action="store_true", help="print the command only")
    s = sub.add_parser("skin", help="set an offline account skin from a PNG file")
    s.add_argument("account_id")
    s.add_argument("file")
    s.add_argument("--model", choices=("auto", "classic", "slim"), default="auto")
    args = p.parse_args(argv)

    if not args.cmd:
        try:
            if sys.platform == "win32":
                from . import gui_tk as gui
            else:
                from . import gui
        except (ImportError, ValueError) as exc:
            sys.stderr.write("تعذّر تشغيل الواجهة: %s\n" % exc)
            if sys.platform != "win32":
                sys.stderr.write("sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0\n")
            return 1
        return gui.run()

    cfg, store = Config(), AccountStore()
    try:
        if args.cmd == "versions":
            for vid, typ in list_versions(load_manifest(), cfg.root, args.snapshots, args.old):
                print("%-22s %s" % (vid, typ))
        elif args.cmd == "accounts":
            for acc in store.accounts:
                print("%-48s %-9s %s" % (acc["id"], TYPE_LABELS[acc["type"]], acc["name"]))
        elif args.cmd == "add-offline":
            print("تمت الإضافة:", store.add(new_offline_account(args.name))["id"])
        elif args.cmd == "add-ely":
            password = os.environ.get("XLOP_ELY_PASSWORD") or getpass.getpass("كلمة مرور Ely.by: ")
            try:
                acc = ely_login(args.username, password, args.totp)
            except TwoFactorRequired:
                acc = ely_login(args.username, password, input("رمز المصادقة الثنائية: "))
            print("تمت الإضافة:", store.add(acc)["id"])
        elif args.cmd == "update":
            if not updater.update_url():
                print("التحديثات غير مفعّلة في هذه النسخة (لا يوجد رابط تحديث).")
                return 0
            info = updater.check()
            if not info:
                print("أنت على آخر إصدار (%s)." % VERSION)
                return 0
            print("إصدار جديد متوفر: %s (الحالي %s)" % (info["version"], VERSION))
            if info["notes"]:
                print(info["notes"])
            if args.check:
                return 0
            print("جارٍ التنزيل والتحقق…")
            path = updater.download_update(info)
            print("جارٍ التثبيت…")
            updater.install(path)
            print("تم التحديث إلى %s. شغّل اللانشر من جديد." % info["version"])
        elif args.cmd == "add-microsoft":
            auth = MicrosoftAuth(microsoft_client_id(cfg))
            info = auth.begin()
            print("1) Open:        %s" % info["verification_uri"])
            print("2) Enter code:  %s" % info["user_code"])
            print("3) Sign in with your Microsoft account...  (waiting)")
            sys.stdout.flush()
            acc = auth.finish(auth.poll(info))
            print("تمت الإضافة:", store.add(acc)["id"], "(%s)" % acc["name"])
        elif args.cmd == "remove":
            store.remove(args.account_id)
        elif args.cmd == "skin":
            from . import skins
            acc = store.get(args.account_id)
            if not acc or acc["type"] != OFFLINE:
                raise GameError("السكن عبر هذا الأمر متاح للحسابات الأوفلاين فقط")
            png, w, h, rgba = skins.normalize_skin(open(args.file, "rb").read())
            acc["skin_path"] = skins.save_account_skin(acc["id"], png)
            acc["skin_model"] = args.model
            store.save()
            print("تم تعيين السكن (%s)" % skins.resolve_model(args.model, w, h, rgba))
        elif args.cmd in ("install", "launch"):
            manifest = load_manifest()
            def progress(text, frac):
                sys.stderr.write("\r%-70s %3d%%" % (text[:70], frac * 100))
                sys.stderr.flush()
            if args.cmd == "install":
                from .game import install_version
                install_version(cfg, args.version, manifest, progress)
                sys.stderr.write("\n")
            else:
                acct = _pick_account(store, cfg, args.account)
                result = launch(cfg, store, acct, args.version, manifest, progress,
                                on_line=lambda ln: sys.stdout.write(ln), dry_run=args.dry_run)
                sys.stderr.write("\n")
                if args.dry_run:
                    safe = [("<token>" if t == acct.get("access_token") else t) for t in result.cmd]
                    print(" ".join(shlex.quote(t) for t in safe))
                    return 0
                return result
    except NotConfigured:
        sys.stderr.write("\nحساب Microsoft غير مفعّل بعد: لا يوجد معرّف تطبيق معتمد (راجع README).\n")
        return 1
    except (GameError, AuthError, NetError, updater.UpdateError) as exc:
        sys.stderr.write("\nخطأ: %s\n" % exc)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
