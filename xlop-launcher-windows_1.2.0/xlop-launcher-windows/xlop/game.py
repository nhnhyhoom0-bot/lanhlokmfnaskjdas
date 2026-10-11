"""Versions, installation (jar, libraries, assets, Java) and launch command."""
import glob
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import subprocess
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import VERSION, urls
from .accounts import ELYBY, MICROSOFT, OFFLINE, ely_ensure_fresh, microsoft_ensure_fresh
from .net import NetError, download, get_json
from .paths import IS_WIN, data_dir, json_load, json_save


class GameError(Exception):
    pass


OS_NAME = "windows" if IS_WIN else "linux"


# ----------------------------------------------------------------- platform / rules

def machine_arch():
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "x86_64"
    if m in ("i386", "i486", "i586", "i686", "x86"):
        return "x86"
    if m in ("aarch64", "arm64"):
        return "arm64"
    return m


def _os_version():
    return platform.version() if IS_WIN else platform.release()


def rule_allows(rules, features=None):
    """Mojang rule semantics: no rules -> allowed; last matching rule wins."""
    if not rules:
        return True
    features = features or {}
    allowed = False
    for rule in rules:
        ok = True
        os_rule = rule.get("os")
        if os_rule:
            if os_rule.get("name") not in (None, OS_NAME):
                ok = False
            if "version" in os_rule and not re.search(os_rule["version"], _os_version()):
                ok = False
            if "arch" in os_rule and os_rule["arch"] != machine_arch():
                ok = False
        for key, want in (rule.get("features") or {}).items():
            if bool(features.get(key, False)) != bool(want):
                ok = False
        if ok:
            allowed = rule.get("action") == "allow"
    return allowed


# ----------------------------------------------------------------- version list

def load_manifest():
    cache = os.path.join(data_dir(), "version_manifest_v2.json")
    for url in (urls.MANIFEST, urls.MANIFEST_FALLBACK):
        try:
            manifest = get_json(url, timeout=20)
            json_save(cache, manifest)
            return manifest
        except (NetError, ValueError):
            continue
    manifest = json_load(cache, None)
    if manifest:
        return manifest
    raise GameError("تعذّر جلب قائمة النسخ من Mojang ولا توجد نسخة محفوظة. تحقق من الإنترنت")


def local_versions(root):
    found = []
    for path in glob.glob(os.path.join(root, "versions", "*", "*.json")):
        vid = os.path.basename(os.path.dirname(path))
        if os.path.basename(path) == vid + ".json":
            found.append(vid)
    return sorted(found)


def list_versions(manifest, root, snapshots=False, old=False):
    """[(id, type)] newest first; local-only (modded) versions are included."""
    allowed = {"release"}
    if snapshots:
        allowed.add("snapshot")
    if old:
        allowed.update(("old_beta", "old_alpha"))
    out, seen = [], set()
    for v in (manifest or {}).get("versions", []):
        if v["type"] in allowed:
            out.append((v["id"], v["type"]))
            seen.add(v["id"])
    for vid in local_versions(root):
        if vid not in seen:
            out.insert(0, (vid, "local"))
    return out


# ----------------------------------------------------------------- version json

def ensure_version_json(root, manifest, vid):
    path = os.path.join(root, "versions", vid, vid + ".json")
    entry = next((v for v in (manifest or {}).get("versions", []) if v["id"] == vid), None)
    if entry:
        download(entry["url"], path, sha1=entry.get("sha1"))
    elif not os.path.isfile(path):
        raise GameError("النسخة %s غير موجودة" % vid)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def resolve_version(root, manifest, vid, _depth=0):
    vj = ensure_version_json(root, manifest, vid)
    parent_id = vj.get("inheritsFrom")
    if not parent_id:
        return vj
    if _depth > 5:
        raise GameError("سلسلة وراثة النسخ طويلة جدًا")
    parent = resolve_version(root, manifest, parent_id, _depth + 1)
    merged = dict(parent)
    merged.update({k: v for k, v in vj.items()
                   if k not in ("libraries", "arguments", "inheritsFrom")})
    merged["libraries"] = vj.get("libraries", []) + parent.get("libraries", [])
    if "arguments" in vj or "arguments" in parent:
        a, b = parent.get("arguments", {}), vj.get("arguments", {})
        merged["arguments"] = {k: a.get(k, []) + b.get(k, []) for k in ("game", "jvm")}
    if "minecraftArguments" in vj:
        merged.pop("arguments", None)
    merged["_jar_id"] = vj.get("jar") or parent_id
    return merged


# ----------------------------------------------------------------- libraries

def maven_path(name, classifier=None):
    ext = "jar"
    if "@" in name:
        name, ext = name.split("@", 1)
    parts = name.split(":")
    group, art, ver = parts[0], parts[1], parts[2]
    cls = classifier or (parts[3] if len(parts) > 3 else None)
    fname = "%s-%s%s.%s" % (art, ver, "-" + cls if cls else "", ext)
    return "%s/%s/%s/%s" % (group.replace(".", "/"), art, ver, fname)


def collect_libraries(vj, root, features):
    """-> list of dicts: path, url, sha1, size, native (bool), exclude [..]"""
    libs_dir = os.path.join(root, "libraries")
    arch_bits = "64" if machine_arch() != "x86" else "32"
    out = []
    for lib in vj.get("libraries", []):
        if not rule_allows(lib.get("rules"), features):
            continue
        name = lib["name"]
        dls = lib.get("downloads")
        native_map = lib.get("natives") or {}
        exclude = (lib.get("extract") or {}).get("exclude", ["META-INF/"])
        entries = []
        if dls:
            if dls.get("artifact"):
                entries.append((dls["artifact"], False))
            if OS_NAME in native_map:
                key = native_map[OS_NAME].replace("${arch}", arch_bits)
                art = (dls.get("classifiers") or {}).get(key)
                if art:
                    entries.append((art, True))
        else:                                    # old style: maven coordinates only
            base = (lib.get("url") or urls.LIBRARIES_BASE).rstrip("/") + "/"
            if OS_NAME in native_map:
                key = native_map[OS_NAME].replace("${arch}", arch_bits)
                p = maven_path(name, key)
                entries.append(({"path": p, "url": base + p}, True))
            else:
                p = maven_path(name)
                entries.append(({"path": p, "url": base + p}, False))
        parts = name.split(":")
        new_native = len(parts) > 3 and parts[3].startswith("natives-" + OS_NAME)
        for art, is_native in entries:
            path = art.get("path") or maven_path(name)
            out.append({"path": os.path.normpath(os.path.join(libs_dir, path)), "url": art.get("url", ""),
                        "sha1": art.get("sha1"), "size": art.get("size"),
                        "native": is_native, "extract": is_native or new_native,
                        "exclude": exclude})
    return out


def extract_natives(libs, dest):
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)
    for lib in libs:
        if not lib["extract"] or not os.path.isfile(lib["path"]):
            continue
        with zipfile.ZipFile(lib["path"]) as zf:
            for info in zf.infolist():
                n = info.filename
                if info.is_dir() or any(n.startswith(e) for e in lib["exclude"]):
                    continue
                if IS_WIN:
                    if not n.lower().endswith(".dll"):
                        continue
                elif not (n.endswith(".so") or ".so." in n):
                    continue
                target = os.path.join(dest, os.path.basename(n))     # flatten, no path tricks
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)


# ----------------------------------------------------------------- downloading

def _parallel(tasks, report, cancel=None, workers=16):
    if not tasks:
        return
    done = 0
    ex = ThreadPoolExecutor(workers)
    try:
        futs = [ex.submit(t) for t in tasks]
        for f in as_completed(futs):
            f.result()
            done += 1
            report(done, len(tasks))
            if cancel and cancel():
                raise GameError("أُلغي التنزيل")
    finally:
        try:
            ex.shutdown(wait=True, cancel_futures=True)
        except TypeError:                       # Python < 3.9
            ex.shutdown(wait=True)


def _stage(progress, text, lo, hi):
    def report(done, total):
        if progress:
            progress("%s (%d/%d)" % (text, done, total), lo + (hi - lo) * done / max(total, 1))
    return report


def install_assets(root, vj, game_dir, progress=None, cancel=None, lo=0.2, hi=0.55):
    idx = vj.get("assetIndex")
    if not idx:
        return "legacy"
    assets = os.path.join(root, "assets")
    idx_path = os.path.join(assets, "indexes", idx["id"] + ".json")
    download(idx["url"], idx_path, sha1=idx.get("sha1"), size=idx.get("size"))
    with open(idx_path, encoding="utf-8") as fh:
        index = json.load(fh)
    objects = index.get("objects", {})
    seen, tasks = set(), []
    for info in objects.values():
        h = info["hash"]
        if h in seen:
            continue
        seen.add(h)
        dest = os.path.join(assets, "objects", h[:2], h)
        url = "%s/%s/%s" % (urls.ASSETS_BASE, h[:2], h)
        tasks.append(lambda u=url, d=dest, s=info["size"], hh=h: download(u, d, sha1=hh, size=s))
    _parallel(tasks, _stage(progress, "تنزيل الأصوات والموارد", lo, hi), cancel)
    # legacy layouts: copy objects to their real names
    target_root = None
    if index.get("map_to_resources"):
        target_root = os.path.join(game_dir, "resources")
    elif index.get("virtual"):
        target_root = os.path.join(assets, "virtual", idx["id"])
    if target_root:
        for name, info in objects.items():
            dest = os.path.join(target_root, *name.split("/"))
            if os.path.isfile(dest) and os.path.getsize(dest) == info["size"]:
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(os.path.join(assets, "objects", info["hash"][:2], info["hash"]), dest)
    return idx["id"]


# ----------------------------------------------------------------- Java

def _java_major(java):
    try:
        out = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=15).stderr
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'version "(\d+)(?:\.(\d+))?', out)
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2) or 0) if major == 1 else major


def find_system_java(required):
    if IS_WIN:
        cands = [shutil.which("java")]
        for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            for vendor in ("Java", "Eclipse Adoptium", "Microsoft", "Zulu", "BellSoft", "Amazon Corretto"):
                cands += sorted(glob.glob(os.path.join(os.environ.get(env, "C:\\Program Files"), vendor, "*", "bin", "java.exe")))
    else:
        cands = [shutil.which("java")] + sorted(glob.glob("/usr/lib/jvm/*/bin/java"))
    if os.environ.get("JAVA_HOME"):
        cands.insert(0, os.path.join(os.environ["JAVA_HOME"], "bin", JAVA_EXE))
    for java in cands:
        if java and os.path.isfile(java):
            major = _java_major(java)
            if major and ((required <= 8 and major == 8) or (required > 8 and major >= required)):
                return java
    return None


JAVA_EXE = "java.exe" if IS_WIN else "java"
RUNTIME_KEYS = ({"x86_64": "windows-x64", "x86": "windows-x86", "arm64": "windows-arm64"} if IS_WIN
                else {"x86_64": "linux", "x86": "linux-i386"})


def _install_runtime(root, component, entry, progress, cancel, lo, hi):
    home = os.path.join(root, "runtime-xlop", component)
    marker = os.path.join(home, ".xlop-installed")
    sha = entry["manifest"]["sha1"]
    if os.path.isfile(marker) and open(marker).read().strip() == sha and os.path.isfile(os.path.join(home, "bin", JAVA_EXE)):
        return home + "/bin/java"
    manifest = get_json(entry["manifest"]["url"], timeout=60)
    files = manifest.get("files", {})
    os.makedirs(home, exist_ok=True)
    tasks, links = [], []
    for rel, info in files.items():
        dest = os.path.join(home, *rel.split("/"))
        if info["type"] == "directory":
            os.makedirs(dest, exist_ok=True)
        elif info["type"] == "file":
            raw = info["downloads"]["raw"]

            def task(d=dest, r=raw, ex=info.get("executable")):
                download(r["url"], d, sha1=r["sha1"], size=r["size"])
                if ex and not IS_WIN:
                    os.chmod(d, 0o755)
            tasks.append(task)
        elif info["type"] == "link":
            links.append((dest, info["target"]))
    _parallel(tasks, _stage(progress, "تنزيل Java (%s)" % component, lo, hi), cancel)
    for dest, target in links:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.lexists(dest):
            os.remove(dest)
        try:
            os.symlink(target, dest)
        except OSError:                       # Windows without symlink rights: copy instead
            src = os.path.normpath(os.path.join(os.path.dirname(dest), target))
            if os.path.isfile(src):
                shutil.copyfile(src, dest)
    java = os.path.join(home, "bin", JAVA_EXE)
    if not os.path.isfile(java):
        raise GameError("حزمة Java المنزّلة لا تحتوي bin/java")
    if not IS_WIN:
        os.chmod(java, 0o755)
    with open(marker, "w") as fh:
        fh.write(sha)
    return java


def ensure_java(cfg, vj, progress=None, cancel=None, lo=0.56, hi=1.0):
    if cfg["java_path"]:
        if not os.path.isfile(cfg["java_path"]):
            raise GameError("مسار Java المحدد غير موجود: %s" % cfg["java_path"])
        return cfg["java_path"]
    jv = vj.get("javaVersion") or {}
    component = jv.get("component") or "jre-legacy"
    major = int(jv.get("majorVersion") or 8)
    plat = RUNTIME_KEYS.get(machine_arch())
    cached = os.path.join(cfg.root, "runtime-xlop", component, "bin", JAVA_EXE)
    if plat:
        try:
            allj = get_json(urls.JAVA_RUNTIMES, timeout=30)
            entries = allj.get(plat, {}).get(component) or []
            if entries:
                return _install_runtime(cfg.root, component, entries[0], progress, cancel, lo, hi)
        except NetError:
            if os.path.isfile(cached):
                return cached
    system = find_system_java(major)
    if system:
        return system
    raise GameError("لم أجد Java %d. %s" % (major, "نزّلها من adoptium.net ثم أعد المحاولة" if IS_WIN
                                                   else "ثبّتها بالأمر: sudo apt install openjdk-%d-jre" % major))


# ----------------------------------------------------------------- install

def install_version(cfg, vid, manifest=None, progress=None, cancel=None):
    """Download everything needed to run `vid`. Returns (resolved json, java path)."""
    root = cfg.root
    if progress:
        progress("قراءة معلومات النسخة", 0.0)
    vj = resolve_version(root, manifest, vid)
    features = {"has_custom_resolution": True}
    # client jar
    jar_id = vj.get("_jar_id", vid)
    jar_json = vj if jar_id == vid else resolve_version(root, manifest, jar_id)
    client = (jar_json.get("downloads") or {}).get("client")
    jar_path = os.path.join(root, "versions", jar_id, jar_id + ".jar")
    if client:
        download(client["url"], jar_path, sha1=client.get("sha1"), size=client.get("size"))
    elif not os.path.isfile(jar_path):
        raise GameError("لا يوجد ملف jar للنسخة %s" % jar_id)
    if progress:
        progress("تنزيل ملف اللعبة", 0.08)
    # libraries
    libs = collect_libraries(vj, root, features)
    tasks = [lambda l=l: download(l["url"], l["path"], sha1=l["sha1"], size=l["size"])
             for l in libs if l["url"]]
    _parallel(tasks, _stage(progress, "تنزيل المكتبات", 0.08, 0.2), cancel)
    # logging config
    log = ((vj.get("logging") or {}).get("client") or {})
    if log.get("file"):
        f = log["file"]
        download(f["url"], os.path.join(root, "assets", "log_configs", f["id"]),
                 sha1=f.get("sha1"), size=f.get("size"))
    install_assets(root, vj, cfg.game_dir(vid), progress, cancel, 0.2, 0.55)
    java = ensure_java(cfg, vj, progress, cancel, 0.56, 1.0)
    if progress:
        progress("جاهز", 1.0)
    return vj, java


# ----------------------------------------------------------------- launch command

_PLACEHOLDER = re.compile(r"\$\{(\w+)\}")


def _expand(args, features, subs):
    out = []
    for a in args:
        if isinstance(a, dict):
            if not rule_allows(a.get("rules"), features):
                continue
            vals = a["value"] if isinstance(a["value"], list) else [a["value"]]
        else:
            vals = [a]
        out.extend(_PLACEHOLDER.sub(lambda m: str(subs.get(m.group(1), "")), v) for v in vals)
    return out


class LaunchPlan:
    def __init__(self, cmd, cwd, env, cleanup=None):
        self.cmd, self.cwd, self.env, self.cleanup = cmd, cwd, env, cleanup

    def start(self):
        os.makedirs(self.cwd, exist_ok=True)
        flags = subprocess.CREATE_NO_WINDOW if IS_WIN else 0     # no black console on Windows
        return subprocess.Popen(self.cmd, cwd=self.cwd, env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace", creationflags=flags)


def build_plan(cfg, acct, vid, vj, java, store=None, progress=None):
    from . import skins                                # local import: keeps game.py light
    root = cfg.root
    game_dir = cfg.game_dir(vid)
    features = {"has_custom_resolution": True}
    libs = collect_libraries(vj, root, features)
    natives = os.path.join(root, "versions", vid, "natives")
    extract_natives(libs, natives)
    cp = [l["path"] for l in libs if not l["native"] and os.path.isfile(l["path"])]
    jar_id = vj.get("_jar_id", vid)
    jar_path = os.path.join(root, "versions", jar_id, jar_id + ".jar")
    cp.append(jar_path)

    # --- authentication
    extra_jvm, cleanup = [], None
    kind = acct["type"]
    if kind == OFFLINE:
        token, user_type = secrets.token_hex(16), "legacy"
        if acct.get("skin_path") and os.path.isfile(acct["skin_path"]):
            try:
                png, w, h, rgba = skins.normalize_skin(open(acct["skin_path"], "rb").read())
                model = skins.resolve_model(acct.get("skin_model", "auto"), w, h, rgba)
                server = skins.SkinServer()
                base = server.start()
                server.add(acct["uuid"], acct["name"], png, model)
                jar = skins.ensure_authlib_injector(lambda t: progress and progress(t, 1.0))
                extra_jvm += ["-javaagent:%s=%s" % (jar, base), "-Dauthlibinjector.noShowServerName"]
                cleanup = server.stop
            except (skins.SkinError, OSError) as exc:
                if progress:
                    progress("تعذّر تفعيل السكن: %s" % exc, 1.0)
    elif kind == ELYBY:
        ely_ensure_fresh(acct, store)
        token, user_type = acct["access_token"], "mojang"
        jar = skins.ensure_authlib_injector(lambda t: progress and progress(t, 1.0))
        extra_jvm += ["-javaagent:%s=%s" % (jar, urls.ELY_INJECTOR)]
    elif kind == MICROSOFT:
        microsoft_ensure_fresh(acct, cfg, store)
        token, user_type = acct["access_token"], "msa"
    else:
        raise GameError("نوع حساب غير معروف")

    assets_id = (vj.get("assetIndex") or {}).get("id", "legacy")
    assets_root = os.path.join(root, "assets")
    idx_path = os.path.join(assets_root, "indexes", assets_id + ".json")
    index = json_load(idx_path, {})
    if index.get("map_to_resources"):
        game_assets = os.path.join(game_dir, "resources")
    elif index.get("virtual"):
        game_assets = os.path.join(assets_root, "virtual", assets_id)
    else:
        game_assets = assets_root

    subs = {
        "auth_player_name": acct["name"], "version_name": vid, "game_directory": game_dir,
        "assets_root": assets_root, "assets_index_name": assets_id,
        "auth_uuid": acct["uuid"], "auth_access_token": token, "clientid": "0", "auth_xuid": "0",
        "user_type": user_type, "version_type": vj.get("type", "release"), "user_properties": "{}",
        "auth_session": "token:%s:%s" % (token, acct["uuid"]), "game_assets": game_assets,
        "resolution_width": cfg["width"], "resolution_height": cfg["height"],
        "natives_directory": natives, "launcher_name": "xlop-launcher", "launcher_version": VERSION,
        "classpath": os.pathsep.join(cp), "classpath_separator": os.pathsep,
        "library_directory": os.path.join(root, "libraries"),
        "primary_jar": jar_path, "primary_jar_name": os.path.basename(jar_path),
    }
    if "arguments" in vj:
        jvm = _expand(vj["arguments"].get("jvm", []), features, subs)
        game = _expand(vj["arguments"].get("game", []), features, subs)
    else:
        jvm = _expand(["-Djava.library.path=${natives_directory}", "-cp", "${classpath}"], features, subs)
        game = _expand((vj.get("minecraftArguments") or "").split(), features, subs)
    if "-cp" not in jvm and "-classpath" not in jvm:
        jvm += ["-cp", subs["classpath"]]

    log_arg = []
    log = ((vj.get("logging") or {}).get("client") or {})
    if log.get("argument") and log.get("file"):
        path = os.path.join(root, "assets", "log_configs", log["file"]["id"])
        log_arg = [log["argument"].replace("${path}", path)]

    mem = int(cfg["memory_mb"])
    cmd = ([java, "-Xmx%dM" % mem, "-Xms%dM" % min(512, mem)] + extra_jvm + log_arg
           + shlex.split(cfg["jvm_args"], posix=not IS_WIN) + jvm + [vj["mainClass"]] + game)
    return LaunchPlan(cmd, game_dir, os.environ.copy(), cleanup)


def launch(cfg, store, acct, vid, manifest=None, progress=None, cancel=None,
           on_line=None, on_start=None, dry_run=False):
    """Install if needed, then run the game. Returns the exit code (or the plan if dry_run)."""
    vj, java = install_version(cfg, vid, manifest, progress, cancel)
    if progress:
        progress("تجهيز الحساب والتشغيل", 1.0)
    plan = build_plan(cfg, acct, vid, vj, java, store, progress)
    if dry_run:
        if plan.cleanup:
            plan.cleanup()
        return plan
    try:
        proc = plan.start()
        if on_start:
            on_start(proc)
        for line in proc.stdout:
            if on_line:
                on_line(line)
        return proc.wait()
    finally:
        if plan.cleanup:
            plan.cleanup()
