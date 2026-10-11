"""Tkinter interface for xlop launcher (used on Windows; ships with python.org Python)."""
import base64
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import APP_NAME, VERSION, skins, urls
from . import update as updater
from .accounts import (ELYBY, MICROSOFT, OFFLINE, TYPE_LABELS, AccountStore, AuthError, MicrosoftAuth,
                       NotConfigured, TwoFactorRequired, ely_login, microsoft_client_id,
                       microsoft_ensure_fresh, new_offline_account)
from .game import GameError, launch, list_versions, load_manifest
from .net import NetError
from .paths import IS_WIN, Config, app_dir

ERRORS = (GameError, AuthError, NetError, skins.SkinError, OSError)
MS_NOT_READY = ("تسجيل الدخول بحساب Microsoft يتطلب معرّف تطبيق معتمد من Mojang، وهذا الإصدار من "
                "اللانشر لا يحتوي واحدًا بعد.\n\nمطوّر اللانشر يضعه مرة واحدة (التفاصيل في README)، "
                "وبعدها يعمل تسجيل الدخول للجميع بدون أي خطوة إضافية.\n\n"
                "يمكنك الآن استخدام الحساب الأوفلاين أو حساب Ely.by.")
MODELS = {"تلقائي": "auto", "كلاسيك (Steve)": "classic", "نحيف (Alex)": "slim"}
GREEN, BLUE = "#2e7d32", "#1c6ed5"

_Q = queue.Queue()


def ui(fn, *args, **kwargs):
    """Run fn(*args, **kwargs) on the Tk main thread (safe to call from worker threads)."""
    _Q.put((fn, args, kwargs))


class XlopApp:
    def __init__(self, root):
        self.root = root
        self.cfg = Config()
        self.store = AccountStore()
        self.manifest = None
        self.mode = "idle"
        self.proc = None
        self.pending_skin = None
        self.preview_img = None
        self.update_info = None
        self.stopped = False
        self.ms_top = None
        self.ms_cancel = False
        self.ms_deadline = None
        self.acc_ids, self.ver_ids = [], []
        self._loading = True
        root.title(APP_NAME)
        root.geometry("980x680")
        root.minsize(860, 600)
        for base in (getattr(sys, "_MEIPASS", ""), app_dir()):          # _MEIPASS: PyInstaller one-file
            icon = os.path.join(base, "xlop-launcher.png")
            if base and os.path.isfile(icon):
                try:
                    root.iconphoto(True, tk.PhotoImage(file=icon))
                except tk.TclError:
                    pass
                break
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True)
        self.pages = {}
        for key, title, build in (("play", "اللعب", self._page_play), ("accounts", "الحسابات", self._page_accounts),
                                  ("skins", "السكنات", self._page_skins), ("settings", "الإعدادات", self._page_settings),
                                  ("log", "السجل", self._page_log)):
            frame = ttk.Frame(self.nb, padding=24)
            self.pages[key] = frame
            self.nb.add(frame, text="  %s  " % title)
            build(frame)
        self._loading = False
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._pump()
        self._refresh_accounts()
        self._fill_versions()
        threading.Thread(target=self._load_manifest, daemon=True).start()
        self._maybe_check_updates()

    # ------------------------------------------------------------------ plumbing
    def _pump(self):
        try:
            while True:
                fn, args, kwargs = _Q.get_nowait()
                try:
                    fn(*args, **kwargs)
                except Exception:
                    traceback.print_exc()
        except queue.Empty:
            pass
        self.root.after(60, self._pump)

    def show_page(self, key):
        self.nb.select(self.pages[key])

    def error(self, text, title="حدث خطأ"):
        messagebox.showerror(title, str(text), parent=self.root)

    def info(self, text):
        messagebox.showinfo(APP_NAME, str(text), parent=self.root)

    def ask(self, title, fields, text=None):
        """fields: [(label, secret, default)] -> list of strings or None."""
        top = tk.Toplevel(self.root)
        top.title(title)
        top.transient(self.root)
        top.resizable(False, False)
        result = {"v": None}
        frm = ttk.Frame(top, padding=18)
        frm.pack()
        row = 0
        if text:
            tk.Label(frm, text=text, wraplength=420, justify="right", anchor="e").grid(
                row=row, column=0, columnspan=2, sticky="e", pady=(0, 10))
            row += 1
        entries = []
        for label, secret, default in fields:
            ttk.Label(frm, text=label).grid(row=row, column=1, sticky="e", padx=8, pady=5)
            entry = ttk.Entry(frm, width=34, show="*" if secret else "")
            entry.insert(0, default)
            entry.grid(row=row, column=0, pady=5)
            entries.append(entry)
            row += 1

        def ok(_e=None):
            result["v"] = [e.get() for e in entries]
            top.destroy()
        btns = ttk.Frame(frm)
        btns.grid(row=row, column=0, columnspan=2, pady=(14, 0))
        ttk.Button(btns, text="موافق", command=ok).pack(side="left", padx=4)
        ttk.Button(btns, text="إلغاء", command=top.destroy).pack(side="left", padx=4)
        top.bind("<Return>", ok)
        top.bind("<Escape>", lambda _e: top.destroy())
        if entries:
            entries[0].focus_set()
        top.grab_set()
        self.root.wait_window(top)
        return result["v"]

    # ------------------------------------------------------------------ play page
    def _page_play(self, page):
        self.update_bar = tk.Frame(page, bg="#dbe8fb", padx=10, pady=8)
        self.update_label = tk.Label(self.update_bar, text="", bg="#dbe8fb", anchor="e")
        self.update_label.pack(side="right", fill="x", expand=True)
        tk.Button(self.update_bar, text="لاحقًا", command=self.update_bar.pack_forget).pack(side="left", padx=4)
        self.update_btn = tk.Button(self.update_bar, text="تحديث الآن", bg=BLUE, fg="white", command=self.on_update_now)
        self.update_btn.pack(side="left", padx=4)

        self.title_label = tk.Label(page, text="xlop launcher", font=("Segoe UI", 24, "bold"), anchor="e")
        self.title_label.pack(fill="x")
        tk.Label(page, text="ماين كرافت جافا - حسابات أوفلاين، Microsoft، و Ely.by", fg="#666", anchor="e").pack(fill="x", pady=(0, 18))
        grid = ttk.Frame(page)
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=1)
        self.acc_var, self.ver_var = tk.StringVar(), tk.StringVar()
        ttk.Label(grid, text="الحساب").grid(row=0, column=1, sticky="e", padx=10, pady=6)
        self.acc_combo = ttk.Combobox(grid, textvariable=self.acc_var, state="readonly")
        self.acc_combo.grid(row=0, column=0, sticky="we")
        self.acc_combo.bind("<<ComboboxSelected>>", self.on_account_changed)
        ttk.Label(grid, text="النسخة").grid(row=1, column=1, sticky="e", padx=10, pady=6)
        self.ver_combo = ttk.Combobox(grid, textvariable=self.ver_var, state="readonly")
        self.ver_combo.grid(row=1, column=0, sticky="we")
        self.ver_combo.bind("<<ComboboxSelected>>", self.on_version_changed)
        self.snap_var = tk.BooleanVar(value=bool(self.cfg["show_snapshots"]))
        self.old_var = tk.BooleanVar(value=bool(self.cfg["show_old"]))
        flt = ttk.Frame(page)
        flt.pack(fill="x", pady=8)
        ttk.Checkbutton(flt, text="إظهار نسخ الـ Snapshot", variable=self.snap_var, command=self.on_filter_toggled).pack(side="right", padx=8)
        ttk.Checkbutton(flt, text="إظهار نسخ Alpha / Beta القديمة", variable=self.old_var, command=self.on_filter_toggled).pack(side="right", padx=8)
        self.play_btn = tk.Button(page, text="ابدأ اللعب", font=("Segoe UI", 15, "bold"), bg=GREEN, fg="white",
                                  activebackground="#256628", activeforeground="white", padx=30, pady=8, command=self.on_play)
        self.play_btn.pack(anchor="e", pady=16)
        self.progress = ttk.Progressbar(page, mode="determinate", maximum=100)
        self.progress.pack(fill="x")
        self.progress_text = tk.Label(page, text="", anchor="e", fg="#555")
        self.progress_text.pack(fill="x")
        self.status = tk.Label(page, text="", anchor="e", justify="right", wraplength=860)
        self.status.pack(fill="x", pady=6)

    def _load_manifest(self):
        try:
            ui(self._set_manifest, load_manifest())
        except GameError as exc:
            ui(self._set_status, str(exc))

    def _set_manifest(self, manifest):
        self.manifest = manifest
        self._fill_versions()

    def _fill_versions(self):
        versions = list_versions(self.manifest, self.cfg.root, self.snap_var.get(), self.old_var.get())
        notes = {"snapshot": "  (Snapshot)", "old_beta": "  (Beta)", "old_alpha": "  (Alpha)", "local": "  (محلية / معدّلة)"}
        self.ver_ids = [v for v, _ in versions]
        self.ver_combo["values"] = [v + notes.get(t, "") for v, t in versions]
        latest = ((self.manifest or {}).get("latest") or {}).get("release")
        wanted = self.cfg["selected_version"]
        pick = wanted if wanted in self.ver_ids else (latest if latest in self.ver_ids else (self.ver_ids[0] if self.ver_ids else None))
        if pick is not None:
            self.ver_combo.current(self.ver_ids.index(pick))
        else:
            self.ver_var.set("")

    def selected_version(self):
        i = self.ver_combo.current()
        return self.ver_ids[i] if 0 <= i < len(self.ver_ids) else None

    def selected_account_id(self):
        i = self.acc_combo.current()
        return self.acc_ids[i] if 0 <= i < len(self.acc_ids) else None

    def on_filter_toggled(self):
        self.cfg["show_snapshots"], self.cfg["show_old"] = self.snap_var.get(), self.old_var.get()
        self.cfg.save()
        self._fill_versions()

    def on_version_changed(self, _e=None):
        vid = self.selected_version()
        if vid:
            self.cfg["selected_version"] = vid
            self.cfg.save()

    def on_account_changed(self, _e=None):
        acc_id = self.selected_account_id()
        if acc_id and not self._loading:
            self.cfg["selected_account"] = acc_id
            self.cfg.save()
            self.acc_list.selection_clear(0, "end")
            self.acc_list.selection_set(self.acc_ids.index(acc_id))
        self._update_skin_page()

    def on_list_selected(self, _e=None):
        sel = self.acc_list.curselection()
        if sel:
            self.acc_combo.current(sel[0])
            self.on_account_changed()

    def _set_status(self, text):
        self.status.config(text=text)

    def _progress(self, text, frac):
        self.progress["value"] = max(0.0, min(1.0, frac)) * 100
        self.progress_text.config(text=text)

    def set_mode(self, mode):
        self.mode = mode
        self.play_btn.config(text={"idle": "ابدأ اللعب", "installing": "جارٍ التجهيز…", "running": "إيقاف اللعبة"}[mode],
                             state="disabled" if mode == "installing" else "normal")

    def on_play(self):
        if self.mode == "running":
            if self.proc:
                self.stopped = True
                self.proc.terminate()
            return
        if self.mode != "idle":
            return
        acct = self.store.get(self.selected_account_id() or "")
        vid = self.selected_version()
        if not acct:
            self.show_page("accounts")
            self.info("أضف حسابًا أولًا (أوفلاين أو Microsoft أو Ely.by)")
            return
        if not vid:
            self.error("لا توجد نسخة محددة. تحقق من الاتصال بالإنترنت ثم أعد فتح اللانشر")
            return
        self.stopped = False
        self.set_mode("installing")
        threading.Thread(target=self._launch_thread, args=(acct, vid), daemon=True).start()

    def _launch_thread(self, acct, vid):
        try:
            def on_start(proc):
                self.proc = proc
                ui(self.set_mode, "running")
                ui(self._set_status, "اللعبة تعمل الآن… أبقِ اللانشر مفتوحًا")
            code = launch(self.cfg, self.store, acct, vid, self.manifest,
                          progress=lambda t, f: ui(self._progress, t, f),
                          on_line=lambda ln: ui(self.log_append, ln), on_start=on_start)
            ui(self._set_status, "انتهت اللعبة (رمز الخروج %s)" % code)
            if code != 0 and not self.stopped:
                ui(self.show_page, "log")
        except ERRORS as exc:
            ui(self._set_status, "فشل: %s" % exc)
            ui(self.error, exc)
        except Exception as exc:
            ui(self.log_append, traceback.format_exc())
            ui(self._set_status, "خطأ غير متوقع: %s" % exc)
        finally:
            self.proc = None
            ui(self.set_mode, "idle")
            ui(self._progress, "", 0.0)

    # ------------------------------------------------------------------ accounts page
    def _page_accounts(self, page):
        tk.Label(page, text="الحسابات", font=("Segoe UI", 20, "bold"), anchor="e").pack(fill="x", pady=(0, 10))
        box = ttk.Frame(page)
        box.pack(fill="both", expand=True)
        self.acc_list = tk.Listbox(box, activestyle="none", font=("Segoe UI", 11), exportselection=False)
        sb = ttk.Scrollbar(box, command=self.acc_list.yview)
        self.acc_list.config(yscrollcommand=sb.set)
        sb.pack(side="left", fill="y")
        self.acc_list.pack(side="left", fill="both", expand=True)
        self.acc_list.bind("<<ListboxSelect>>", self.on_list_selected)
        row = ttk.Frame(page)
        row.pack(fill="x", pady=10)
        for label, cb in (("حذف المحدد", self.remove_account), ("+ Ely.by", self.add_ely),
                          ("+ Microsoft", self.add_microsoft), ("+ أوفلاين", self.add_offline)):
            ttk.Button(row, text=label, command=cb).pack(side="right", padx=4)
        tk.Label(page, text="الحساب الأوفلاين يصلح للعب الفردي وللسيرفرات التي تسمح بغير الأصلي (online-mode=false).",
                 fg="#666", anchor="e", wraplength=860, justify="right").pack(fill="x")

    def _refresh_accounts(self, select=None):
        self.acc_ids = [a["id"] for a in self.store.accounts]
        labels = ["%s   [%s]" % (a["name"], TYPE_LABELS[a["type"]]) for a in self.store.accounts]
        self.acc_list.delete(0, "end")
        for text in labels:
            self.acc_list.insert("end", text)
        self.acc_combo["values"] = labels
        want = select or self.cfg["selected_account"]
        if want in self.acc_ids:
            self.acc_combo.current(self.acc_ids.index(want))
        elif self.acc_ids:
            self.acc_combo.current(0)
        else:
            self.acc_var.set("")
        sel = self.selected_account_id()
        if sel:
            self.acc_list.selection_set(self.acc_ids.index(sel))
        self._update_skin_page()

    def _add_account(self, acc):
        self.store.add(acc)
        self.cfg["selected_account"] = acc["id"]
        self.cfg.save()
        self._refresh_accounts(acc["id"])

    def _selected_account(self):
        sel = self.acc_list.curselection()
        if sel:
            return self.store.get(self.acc_ids[sel[0]])
        return self.store.get(self.selected_account_id() or "")

    def add_offline(self):
        vals = self.ask("حساب أوفلاين", [("اسم اللاعب", False, "")], text="اكتب أي اسم (حتى 16 حرفًا إنجليزيًا/أرقام/_).")
        if vals:
            try:
                self._add_account(new_offline_account(vals[0]))
            except AuthError as exc:
                self.error(exc)

    def add_ely(self):
        user = pw = ""
        need_totp = False
        while True:
            fields = [("المستخدم / البريد", False, user), ("كلمة المرور", True, pw)]
            text = "سجّل الدخول بحساب Ely.by (اسم المستخدم أو البريد)."
            if need_totp:
                fields.append(("رمز المصادقة الثنائية", False, ""))
                text = "الحساب محمي بالمصادقة الثنائية. أدخل الرمز الحالي من تطبيق المصادقة."
            vals = self.ask("حساب Ely.by", fields, text=text)
            if not vals:
                return
            user, pw = vals[0].strip(), vals[1]
            try:
                self._add_account(ely_login(user, pw, vals[2] if need_totp else None))
                return
            except TwoFactorRequired:
                need_totp = True
            except (AuthError, NetError) as exc:
                self.error(exc)
                return

    def add_microsoft(self):
        if self.ms_top is not None:
            return
        client_id = microsoft_client_id(self.cfg)
        if not client_id:
            self.error(MS_NOT_READY, "حساب Microsoft غير مفعّل بعد")
            return
        self.ms_cancel = False
        threading.Thread(target=self._ms_thread, args=(client_id,), daemon=True).start()

    def _ms_thread(self, client_id):
        try:
            auth = MicrosoftAuth(client_id)
            info = auth.begin()
            ui(self._ms_show_code, info)
            tokens = auth.poll(info, cancelled=lambda: self.ms_cancel)
            ui(self._ms_waiting, "تم التحقق من الرمز… جارٍ إكمال تسجيل الدخول")
            acc = auth.finish(tokens)
            ui(self._ms_close)
            ui(self._add_account, acc)
            ui(self._set_status, "تمت إضافة حساب Microsoft: %s" % acc["name"])
            ui(self.show_page, "play")
        except NotConfigured:
            ui(self._ms_close)
            ui(self.error, MS_NOT_READY, "حساب Microsoft غير مفعّل بعد")
        except ERRORS as exc:
            ui(self._ms_close)
            if not self.ms_cancel:
                ui(self.error, exc, "فشل تسجيل الدخول بحساب Microsoft")

    def _ms_show_code(self, info):
        top = tk.Toplevel(self.root)
        top.title("تسجيل الدخول بحساب Microsoft")
        top.transient(self.root)
        top.resizable(False, False)
        frm = ttk.Frame(top, padding=24)
        frm.pack()
        tk.Label(frm, justify="right", anchor="e", text="١) افتح صفحة Microsoft أدناه\n٢) أدخل هذا الرمز في الصفحة\n"
                 "٣) سجّل الدخول بحسابك ووافق — وسيكتمل الدخول هنا تلقائيًا").pack(fill="x", pady=(0, 14))
        code = tk.Entry(frm, justify="center", font=("Consolas", 28, "bold"), width=12, relief="flat", bg="#f1f3f6", readonlybackground="#f1f3f6")
        code.insert(0, info["user_code"])
        code.config(state="readonly")
        code.pack(pady=6)
        tk.Label(frm, text=info["verification_uri"], fg=BLUE).pack()
        row = ttk.Frame(frm)
        row.pack(pady=14)
        ttk.Button(row, text="فتح صفحة Microsoft", command=lambda: webbrowser.open(info["verification_uri"])).pack(side="left", padx=4)
        ttk.Button(row, text="نسخ الرمز", command=lambda: self._copy(info["user_code"])).pack(side="left", padx=4)
        self.ms_wait = tk.Label(frm, text="", fg="#555")
        self.ms_wait.pack()
        ttk.Button(frm, text="إلغاء", command=self._ms_cancel_clicked).pack(pady=(12, 0))
        top.protocol("WM_DELETE_WINDOW", self._ms_cancel_clicked)
        self.ms_top = top
        self.ms_deadline = time.time() + int(info.get("expires_in", 900))
        self._ms_tick()
        webbrowser.open(info["verification_uri"])

    def _copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _ms_cancel_clicked(self):
        self.ms_cancel = True
        self._ms_close()

    def _ms_waiting(self, text):
        if self.ms_top is not None:
            self.ms_wait.config(text=text)
            self.ms_deadline = None

    def _ms_tick(self):
        if self.ms_top is None:
            return
        if self.ms_deadline:
            left = max(0, int(self.ms_deadline - time.time()))
            self.ms_wait.config(text="بانتظار إدخال الرمز… (تنتهي صلاحيته بعد %d:%02d)" % (left // 60, left % 60))
        self.root.after(1000, self._ms_tick)

    def _ms_close(self):
        if self.ms_top is not None:
            top, self.ms_top = self.ms_top, None
            top.destroy()

    def remove_account(self):
        acc = self._selected_account()
        if acc and messagebox.askyesno(APP_NAME, "حذف الحساب %s؟" % acc["name"], parent=self.root):
            self.store.remove(acc["id"])
            self._refresh_accounts()

    # ------------------------------------------------------------------ skins page
    def _page_skins(self, page):
        left = ttk.Frame(page)
        left.pack(side="left", anchor="n")
        self.canvas = tk.Canvas(left, width=160, height=320, bg="#eceff4", highlightthickness=1, highlightbackground="#bbb")
        self.canvas.pack()
        right = ttk.Frame(page)
        right.pack(side="right", fill="both", expand=True, padx=(0, 24))
        tk.Label(right, text="السكنات", font=("Segoe UI", 20, "bold"), anchor="e").pack(fill="x")
        self.skin_target = tk.Label(right, text="", anchor="e", justify="right", wraplength=560)
        self.skin_target.pack(fill="x", pady=6)
        ttk.Button(right, text="اختيار صورة سكن (PNG) من جهازك…", command=self.pick_skin).pack(anchor="e", pady=4)
        row = ttk.Frame(right)
        row.pack(fill="x", pady=6)
        ttk.Button(row, text="جلب", command=self.fetch_skin).pack(side="left")
        self.skin_name = ttk.Entry(row)
        self.skin_name.pack(side="left", fill="x", expand=True, padx=6)
        tk.Label(row, text="اسم لاعب أصلي:").pack(side="right")
        self.model_var = tk.StringVar(value="تلقائي")
        mrow = ttk.Frame(right)
        mrow.pack(fill="x", pady=6)
        ttk.Combobox(mrow, textvariable=self.model_var, values=list(MODELS), state="readonly", width=18).pack(side="right")
        tk.Label(mrow, text="الشكل:").pack(side="right", padx=6)
        self.model_var.trace_add("write", lambda *_: self._refresh_preview_model())
        brow = ttk.Frame(right)
        brow.pack(fill="x", pady=8)
        tk.Button(brow, text="تطبيق السكن على الحساب", bg=BLUE, fg="white", command=self.apply_skin).pack(side="right", padx=4)
        ttk.Button(brow, text="إزالة السكن", command=self.remove_skin).pack(side="right", padx=4)
        self.skin_note = tk.Label(right, text="", fg="#666", anchor="e", justify="right", wraplength=560)
        self.skin_note.pack(fill="x", pady=8)

    def _update_skin_page(self):
        if not hasattr(self, "skin_target"):
            return
        acc = self._selected_account()
        self.pending_skin = None
        self.canvas.delete("all")
        if not acc:
            self.skin_target.config(text="لا يوجد حساب محدد. أضف حسابًا أولًا.")
            return
        self.skin_target.config(text="الحساب: %s   [%s]" % (acc["name"], TYPE_LABELS[acc["type"]]))
        self.skin_note.config(text={
            OFFLINE: "للحساب الأوفلاين: يُحفظ السكن ويُعرض داخل اللعبة (النسخ 1.7.2 وما بعدها) عبر خادم محلي عند التشغيل.",
            MICROSOFT: "لحساب Microsoft: يُرفع السكن إلى حسابك الأصلي مباشرة ويظهر لكل اللاعبين.",
            ELYBY: "لحساب Ely.by: تُدار السكنات من موقع Ely.by (ارفع السكن هناك وسيظهر تلقائيًا)."}[acc["type"]])
        if acc["type"] == OFFLINE and acc.get("skin_path") and os.path.isfile(acc["skin_path"]):
            try:
                with open(acc["skin_path"], "rb") as fh:
                    self._set_pending(fh.read(), acc.get("skin_model", "auto"))
            except (skins.SkinError, OSError):
                pass

    def _model_key(self):
        return MODELS.get(self.model_var.get(), "auto")

    def _set_pending(self, data, model=None):
        self.pending_skin = skins.normalize_skin(data)
        if model:
            self.model_var.set(next((k for k, v in MODELS.items() if v == model), "تلقائي"))
        self._refresh_preview_model()

    def _refresh_preview_model(self):
        if not self.pending_skin:
            return
        png, w, h, rgba = self.pending_skin
        pw, ph, pix = skins.preview_rgba(w, h, rgba, skins.resolve_model(self._model_key(), w, h, rgba))
        data = base64.b64encode(skins.encode_png(pw, ph, pix)).decode()
        self.preview_img = tk.PhotoImage(data=data, format="png").zoom(10, 10)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.preview_img)

    def pick_skin(self):
        path = filedialog.askopenfilename(parent=self.root, title="اختر صورة السكن",
                                          filetypes=[("صور PNG", "*.png"), ("كل الملفات", "*.*")])
        if not path:
            return
        try:
            with open(path, "rb") as fh:
                self._set_pending(fh.read())
        except (skins.SkinError, OSError) as exc:
            self.error(exc, "تعذّر قراءة السكن")

    def fetch_skin(self):
        name = self.skin_name.get().strip()
        if not name:
            return
        self.skin_note.config(text="جارٍ جلب السكن…")

        def work():
            try:
                png, model = skins.fetch_skin_by_name(name)
                ui(self._fetched, png, model)
            except ERRORS as exc:
                ui(self.error, exc, "تعذّر جلب السكن")
        threading.Thread(target=work, daemon=True).start()

    def _fetched(self, png, model):
        try:
            self._set_pending(png, model)
            self.skin_note.config(text="تم جلب السكن. اضغط «تطبيق السكن على الحساب» لاستخدامه.")
        except skins.SkinError as exc:
            self.error(exc)

    def apply_skin(self):
        acc = self._selected_account()
        if not acc:
            return
        if acc["type"] == ELYBY:
            webbrowser.open(urls.ELY_SKINS_PAGE)
            self.info("تُدار سكنات Ely.by من الموقع. فتحتُ صفحة السكنات، ارفع السكن هناك وسيظهر في اللعبة.")
            return
        if not self.pending_skin:
            self.error("اختر صورة سكن أولًا")
            return
        png, w, h, rgba = self.pending_skin
        wanted = self._model_key()
        if acc["type"] == OFFLINE:
            acc["skin_path"] = skins.save_account_skin(acc["id"], png)
            acc["skin_model"] = wanted
            self.store.save()
            self.info("تم حفظ السكن. سيظهر عند تشغيل اللعبة (النسخ 1.7.2 وما بعدها).")
            return
        model = skins.resolve_model(wanted, w, h, rgba)
        self.skin_note.config(text="جارٍ رفع السكن إلى حساب Microsoft…")

        def work():
            try:
                microsoft_ensure_fresh(acc, self.cfg, self.store)
                skins.upload_skin(acc["access_token"], png, model)
                ui(self.skin_note.config, text="تم رفع السكن. قد يستغرق ظهوره في اللعبة دقيقة.")
            except ERRORS as exc:
                ui(self.error, exc, "فشل رفع السكن")
        threading.Thread(target=work, daemon=True).start()

    def remove_skin(self):
        acc = self._selected_account()
        if not acc:
            return
        if acc["type"] == OFFLINE:
            acc["skin_path"] = ""
            self.store.save()
            self._update_skin_page()
        elif acc["type"] == MICROSOFT:
            def work():
                try:
                    microsoft_ensure_fresh(acc, self.cfg, self.store)
                    skins.reset_skin(acc["access_token"])
                    ui(self.skin_note.config, text="أُعيد السكن الافتراضي.")
                except ERRORS as exc:
                    ui(self.error, exc)
            threading.Thread(target=work, daemon=True).start()
        else:
            webbrowser.open(urls.ELY_SKINS_PAGE)

    # ------------------------------------------------------------------ settings page
    def _page_settings(self, page):
        tk.Label(page, text="الإعدادات", font=("Segoe UI", 20, "bold"), anchor="e").pack(fill="x", pady=(0, 10))
        c = self.cfg
        grid = ttk.Frame(page)
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=1)
        self.v = {"memory_mb": tk.StringVar(value=str(c["memory_mb"])), "width": tk.StringVar(value=str(c["width"])),
                  "height": tk.StringVar(value=str(c["height"])), "java_path": tk.StringVar(value=c["java_path"]),
                  "jvm_args": tk.StringVar(value=c["jvm_args"]), "game_root": tk.StringVar(value=c["game_root"]),
                  "ms_client_id": tk.StringVar(value=c["ms_client_id"])}
        self.iso_var, self.auto_var = tk.BooleanVar(value=bool(c["isolate"])), tk.BooleanVar(value=bool(c["auto_update"]))
        rows = (("الذاكرة (MB)", "memory_mb", "spin", (512, 65536, 256)), ("عرض النافذة", "width", "spin", (320, 7680, 1)),
                ("ارتفاع النافذة", "height", "spin", (240, 4320, 1)), ("مسار Java (فارغ = تلقائي)", "java_path", "entry", None),
                ("خيارات JVM", "jvm_args", "entry", None), ("مجلد اللعبة (فارغ = .minecraft)", "game_root", "entry", None),
                ("Microsoft Client ID (متقدم)", "ms_client_id", "entry", None))
        for i, (label, key, kind, rng) in enumerate(rows):
            ttk.Label(grid, text=label).grid(row=i, column=1, sticky="e", padx=10, pady=5)
            if kind == "spin":
                w = ttk.Spinbox(grid, from_=rng[0], to=rng[1], increment=rng[2], textvariable=self.v[key], width=12, command=self._save_settings)
                w.grid(row=i, column=0, sticky="w")
            else:
                w = ttk.Entry(grid, textvariable=self.v[key])
                w.grid(row=i, column=0, sticky="we")
            w.bind("<FocusOut>", self._save_settings)
            w.bind("<Return>", self._save_settings)
        ttk.Checkbutton(page, text="مجلد لعبة مستقل لكل نسخة", variable=self.iso_var, command=self._save_settings).pack(anchor="e", pady=(10, 0))
        ttk.Checkbutton(page, text="فحص التحديثات تلقائيًا عند تشغيل اللانشر", variable=self.auto_var, command=self._save_settings).pack(anchor="e")
        row = ttk.Frame(page)
        row.pack(fill="x", pady=14)
        ttk.Button(row, text="افحص التحديثات الآن", command=self.check_updates_now).pack(side="right")
        tk.Label(row, text="إصدار اللانشر: %s" % VERSION, fg="#666").pack(side="right", padx=14)

    def _int(self, key, default, lo, hi):
        try:
            return max(lo, min(hi, int(float(self.v[key].get()))))
        except ValueError:
            return default

    def _save_settings(self, _e=None):
        if self._loading:
            return
        c = self.cfg
        old_root = c["game_root"]
        c["memory_mb"] = self._int("memory_mb", 2048, 512, 65536)
        c["width"] = self._int("width", 854, 320, 7680)
        c["height"] = self._int("height", 480, 240, 4320)
        c["java_path"] = self.v["java_path"].get().strip()
        c["jvm_args"] = self.v["jvm_args"].get().strip()
        c["game_root"] = os.path.expanduser(self.v["game_root"].get().strip())
        c["ms_client_id"] = self.v["ms_client_id"].get().strip()
        c["isolate"], c["auto_update"] = self.iso_var.get(), self.auto_var.get()
        c.save()
        if c["game_root"] != old_root:
            self._fill_versions()

    # ------------------------------------------------------------------ updates
    def _maybe_check_updates(self):
        due = time.time() - float(self.cfg["last_update_check"] or 0) > 6 * 3600
        if self.cfg["auto_update"] and updater.update_url() and due:
            threading.Thread(target=self._update_check_thread, args=(False,), daemon=True).start()

    def check_updates_now(self):
        if not updater.update_url():
            self.info("التحديثات غير مفعّلة في هذه النسخة (لا يوجد رابط تحديث).")
            return
        self._set_status("جارٍ فحص التحديثات…")
        threading.Thread(target=self._update_check_thread, args=(True,), daemon=True).start()

    def _update_check_thread(self, manual):
        try:
            info = updater.check()
            self.cfg["last_update_check"] = time.time()
            self.cfg.save()
            ui(self._update_found, info, manual)
        except updater.UpdateError as exc:
            if manual:
                ui(self.error, exc, "تعذّر فحص التحديثات")

    def _update_found(self, info, manual):
        self.update_info = info
        if info:
            self.update_label.config(text="إصدار جديد متوفر: %s  (الحالي %s)" % (info["version"], VERSION))
            self.update_btn.config(text="تنزيل التحديث")
            self.update_bar.pack(fill="x", pady=(0, 14), before=self.title_label)
            self.show_page("play")
        else:
            self.update_bar.pack_forget()
            if manual:
                self.info("أنت على آخر إصدار (%s)" % VERSION)

    def on_update_now(self):
        """Windows: open the download of the new .exe (it replaces the old one)."""
        info = self.update_info
        if not info:
            return
        if info.get("windows_url"):
            webbrowser.open(info["windows_url"])
            self.info("بدأ تنزيل الإصدار الجديد في المتصفح. أغلق اللانشر ثم استبدل الملف القديم بالجديد "
                      "(حساباتك وإعداداتك محفوظة ولا تتأثر).")
        else:
            self.info("إصدار جديد (%s) متوفر، لكن رابط تنزيل ويندوز غير مضبوط من الناشر." % info["version"])

    # ------------------------------------------------------------------ log page
    def _page_log(self, page):
        box = ttk.Frame(page)
        box.pack(fill="both", expand=True)
        self.log_view = tk.Text(box, state="disabled", wrap="word", font=("Consolas", 9))
        sb = ttk.Scrollbar(box, command=self.log_view.yview)
        self.log_view.config(yscrollcommand=sb.set)
        sb.pack(side="left", fill="y")
        self.log_view.pack(side="left", fill="both", expand=True)
        row = ttk.Frame(page)
        row.pack(fill="x", pady=8)
        ttk.Button(row, text="فتح مجلد اللعبة", command=self.open_folder).pack(side="right", padx=4)
        ttk.Button(row, text="مسح السجل", command=self.log_clear).pack(side="right", padx=4)

    def log_append(self, text):
        self.log_view.config(state="normal")
        self.log_view.insert("end", text)
        self.log_view.see("end")
        self.log_view.config(state="disabled")

    def log_clear(self):
        self.log_view.config(state="normal")
        self.log_view.delete("1.0", "end")
        self.log_view.config(state="disabled")

    def open_folder(self):
        path = self.cfg.root
        os.makedirs(path, exist_ok=True)
        try:
            if IS_WIN:
                os.startfile(path)                      # noqa: S606 (Windows only)
            else:
                subprocess.Popen(["xdg-open", path])
        except OSError:
            self.error("تعذّر فتح المجلد: %s" % path)

    def on_close(self):
        self.ms_cancel = True
        if self.proc:
            try:
                self.proc.terminate()
            except OSError:
                pass
        self.root.destroy()


def run():
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)     # crisp text on high-DPI screens
        except Exception:
            pass
    root = tk.Tk()
    XlopApp(root)
    root.mainloop()
    return 0
