# -*- coding: utf-8 -*-
"""屏幕翻译:截屏翻译 + 实时翻译。

- 「📷 截屏翻译」:框选一块区域 → OCR → 翻译;原文可编辑后「重新翻译」;
- 「🎯 实时翻译」:框选一块区域 → 持续 OCR + 翻译,译文显示在可调悬浮窗里,
  悬浮窗有自己的「停止翻译」和「退出实时翻译」按钮;
- 引擎可在主界面或设置里切换;选到需要 Key 的引擎会直接向你要 Key,
  并给出获取 Key 的网址入口;
- 全局快捷键:截屏翻译 / 开始停止实时翻译 / 开关鼠标穿透。
"""
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import core
import engines
import realtime
import selector
import settings as settings_ui
import theme

# 必须在创建任何窗口之前设置 DPI 感知,否则高分屏截图坐标会错位
core.enable_dpi_awareness()

APP_DIR = core.APP_DIR
LOG_PATH = os.path.join(APP_DIR, "fanyi.log")

try:
    import keyboard as _keyboard
except Exception:
    _keyboard = None

try:
    import pyperclip
except Exception:
    pyperclip = None

FONT = "Microsoft YaHei UI"


def setup_logging():
    handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=512 * 1024, backupCount=1, encoding="utf-8")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[handler])


def engine_labels():
    """主界面下拉框用的「显示名 → 引擎 id」映射。"""
    return {spec.label(): spec.id for spec in engines.all_engines()}


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = core.load_config()
        self.events = queue.Queue()
        self.busy = False
        self.capturing = False
        self.collapsed = False
        self.normal_geometry = ""
        self.last_source = ""
        self.last_result = ""
        self.last_engine = ""
        self.history = []
        self._engine_syncing = False

        theme.apply_theme(root)
        self._build_ui()
        self._apply_window_attrs()
        self._setup_hotkeys()

        self.rt = realtime.RealtimeSession(
            self.root, self.cfg,
            on_exit=self._on_realtime_exit,
            on_status=lambda m: self.events.put(("status", m)),
            on_need_key=self._on_realtime_need_key)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._poll_events)
        self._start_startup_prep()

    # ------------------------------------------------------------ 界面

    def _build_ui(self):
        root = self.root
        root.title("屏幕翻译")
        root.configure(bg=theme.BG)
        root.minsize(430, 360)

        # ---- 固定标题栏(不随内容滚动)----
        header = tk.Frame(root, bg=theme.BG)
        header.pack(fill="x", padx=14, pady=(10, 4))
        tk.Label(header, text="🔤 屏幕翻译", bg=theme.BG, fg=theme.TEXT,
                 font=(FONT, 13, "bold")).pack(side="left")
        for text, cmd in (("✕", self.on_close),
                          ("—", self.toggle_collapse),
                          ("⚙", self.open_settings)):
            theme.flat_button(header, text, cmd, padx=9, pady=2).pack(
                side="right", padx=2)

        # ---- 可滚动主体:窗口不够高时出现滚动条,滚轮可上下翻 ----
        self.scroll = theme.ScrollArea(root, bg=theme.BG)
        self.scroll.pack(fill="both", expand=True)
        theme.enable_wheel_scroll(root, [self.scroll])
        self.body = tk.Frame(self.scroll.inner, bg=theme.BG)
        self.body.pack(fill="both", expand=True, padx=14, pady=(4, 8))

        # ---- 两个主要功能入口 ----
        big = tk.Frame(self.body, bg=theme.BG)
        big.pack(fill="x", pady=(2, 10))
        big.columnconfigure(0, weight=1)
        big.columnconfigure(1, weight=1)

        self.capture_btn = theme.flat_button(
            big, "📷 截屏翻译", self.trigger_capture, kind="primary",
            font=(FONT, 11, "bold"), padx=10, pady=9, justify="center")
        self.capture_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))

        self.realtime_btn = theme.flat_button(
            big, "🎯 实时翻译", self.start_realtime, kind="success",
            font=(FONT, 11, "bold"), padx=10, pady=9, justify="center")
        self.realtime_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self._refresh_hotkey_labels()

        # ---- 引擎 / 语言快切(卡片)----
        card = tk.Frame(self.body, bg=theme.CARD,
                        highlightbackground=theme.BORDER,
                        highlightthickness=1)
        card.pack(fill="x", pady=(0, 10))
        inner = tk.Frame(card, bg=theme.CARD)
        inner.pack(fill="x", padx=11, pady=9)

        # 第一行:引擎(单独一行,名字带 [需 Key] 会比较长,挤在一起会被截断)
        line1 = tk.Frame(inner, bg=theme.CARD)
        line1.pack(fill="x")
        tk.Label(line1, text="翻译引擎", bg=theme.CARD, fg=theme.MUTED,
                 font=(FONT, 9), width=6, anchor="w").pack(side="left")
        self.engine_box = ttk.Combobox(
            line1, state="readonly", values=[s.label() for s in engines.all_engines()])
        self.engine_box.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.engine_box.bind("<<ComboboxSelected>>", self._on_engine_pick)

        # 第二行:源语言 → 目标语言
        line2 = tk.Frame(inner, bg=theme.CARD)
        line2.pack(fill="x", pady=(7, 0))
        tk.Label(line2, text="语言", bg=theme.CARD, fg=theme.MUTED,
                 font=(FONT, 9), width=6, anchor="w").pack(side="left")
        tk.Label(line2, text="从", bg=theme.CARD, fg=theme.MUTED,
                 font=(FONT, 9)).pack(side="left", padx=(6, 4))
        self.source_box = ttk.Combobox(
            line2, state="readonly", width=11,
            values=[n for n, _ in engines.SOURCE_LANGS])
        self.source_box.pack(side="left")
        self.source_box.bind("<<ComboboxSelected>>", self._on_source_pick)
        tk.Label(line2, text="→", bg=theme.CARD, fg=theme.MUTED,
                 font=(FONT, 10)).pack(side="left", padx=8)
        self.lang_box = ttk.Combobox(
            line2, state="readonly", width=12,
            values=[n for n, _ in engines.TARGET_LANGS])
        self.lang_box.pack(side="left")
        self.lang_box.bind("<<ComboboxSelected>>", self._on_lang_pick)

        self.engine_hint = tk.Label(inner, text="", bg=theme.CARD,
                                    fg=theme.WARN, font=(FONT, 8), anchor="w",
                                    justify="left", wraplength=480)
        self.engine_hint.pack(fill="x")
        self._sync_engine_widgets()

        # ---- 次级操作 ----
        row = tk.Frame(self.body, bg=theme.BG)
        row.pack(fill="x", pady=(0, 8))
        for text, cmd in (("📋 剪贴板翻译", self.translate_clipboard),
                          ("🔁 重新翻译", self.retranslate),
                          ("🕘 历史", self.open_history),
                          ("🗑 清空", self.clear_all)):
            theme.flat_button(row, text, cmd, padx=11, pady=5).pack(
                side="left", padx=(0, 5))

        # ---- 原文(可编辑) ----
        tk.Label(self.body, text="原文", bg=theme.BG, fg=theme.TEXT,
                 anchor="w", font=(FONT, 9, "bold")).pack(fill="x")
        tk.Label(self.body, text="识别结果可以直接改,改完点「🔁 重新翻译」",
                 bg=theme.BG, fg=theme.MUTED, anchor="w",
                 font=(FONT, 8)).pack(fill="x", pady=(1, 3))
        src_frame = tk.Frame(self.body, bg=theme.CARD,
                             highlightbackground=theme.BORDER,
                             highlightthickness=1)
        src_frame.pack(fill="x", pady=(0, 10))
        self.src_text = tk.Text(src_frame, wrap="word", height=4, relief="flat",
                                borderwidth=0, highlightthickness=0,
                                font=(FONT, 10), background=theme.CARD,
                                foreground=theme.TEXT, padx=8, pady=6,
                                insertbackground=theme.TEXT)
        src_scroll = ttk.Scrollbar(src_frame, command=self.src_text.yview,
                                   style="Card.Vertical.TScrollbar")
        self.src_text.configure(yscrollcommand=src_scroll.set)
        src_scroll.pack(side="right", fill="y")
        self.src_text.pack(side="left", fill="both", expand=True)

        # ---- 译文 ----
        tk.Label(self.body, text="译文", bg=theme.BG, fg=theme.TEXT,
                 anchor="w", font=(FONT, 9, "bold")).pack(fill="x")
        dst_frame = tk.Frame(self.body, bg=theme.CARD,
                             highlightbackground=theme.BORDER,
                             highlightthickness=1)
        dst_frame.pack(fill="x", pady=(3, 10))
        self.dst_text = tk.Text(dst_frame, wrap="word", height=8, relief="flat",
                                borderwidth=0, highlightthickness=0,
                                font=(FONT, 11), background=theme.CARD,
                                foreground=theme.TEXT, padx=8, pady=6)
        dst_scroll = ttk.Scrollbar(dst_frame, command=self.dst_text.yview,
                                   style="Card.Vertical.TScrollbar")
        self.dst_text.configure(yscrollcommand=dst_scroll.set)
        dst_scroll.pack(side="right", fill="y")
        self.dst_text.pack(side="left", fill="both", expand=True)
        self.dst_text.tag_configure("err", foreground=theme.ERR_FG)
        self.dst_text.configure(state="disabled")

        copy_row = tk.Frame(self.body, bg=theme.BG)
        copy_row.pack(fill="x")
        for text, cmd in (("📄 复制译文", self.copy_result),
                          ("🗒 复制原文", self.copy_source)):
            theme.flat_button(copy_row, text, cmd, padx=11, pady=5).pack(
                side="left", padx=(0, 5))

        # ---- 固定状态栏 ----
        tk.Frame(root, bg=theme.BORDER, height=1).pack(fill="x")
        bar = tk.Frame(root, bg=theme.BG)
        bar.pack(fill="x")
        self.status = tk.Label(bar, text="就绪", anchor="w", fg=theme.MUTED,
                               bg=theme.BG, font=(FONT, 9))
        self.status.pack(fill="x", padx=14, pady=5)

        # 初始窗口给大一点,信息尽量一屏放下;真放不下时靠滚轮 / 滚动条
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = max(500, min(640, sw - 80))
        h = max(560, min(1080, sh - 90))
        self.root.geometry(
            f"{w}x{h}+{max(sw - w - 40, 0)}+{max((sh - h) // 2 - 30, 10)}")
        self.normal_geometry = self.root.geometry()

    def _refresh_hotkey_labels(self):
        """按钮分两行写,快捷键放第二行,免得长名字被按钮宽度截断。"""
        hk = (self.cfg.get("hotkey") or "").strip()
        self.capture_btn.configure(
            text="📷 截屏翻译" + (f"\n{hk}" if hk else ""))
        hk2 = (self.cfg.get("hotkey_realtime") or "").strip()
        self.realtime_btn.configure(
            text="🎯 实时翻译" + (f"\n{hk2}" if hk2 else ""))

    def _sync_engine_widgets(self):
        """让下拉框显示当前配置的引擎/语言,并在缺 Key 时给出提示。"""
        self._engine_syncing = True
        spec = engines.get_engine(self.cfg.get("engine", "mymemory"))
        if spec:
            self.engine_box.set(spec.label())
        self.source_box.set(dict((c, n) for n, c in engines.SOURCE_LANGS).get(
            self.cfg.get("source_lang", "auto"), "自动检测"))
        self.lang_box.set(core.target_name(self.cfg.get("target_lang", "zh-CN")))
        missing = engines.missing_params(self.cfg.get("engine", ""), self.cfg)
        if missing:
            self.engine_hint.configure(
                text=f"⚠ 还缺 {missing[0].label},点⚙填写")
        else:
            self.engine_hint.configure(text="")
        self._engine_syncing = False

    def _apply_window_attrs(self):
        try:
            self.root.attributes("-topmost", bool(self.cfg.get("topmost", True)))
            self.root.attributes("-alpha", float(self.cfg.get("opacity", 0.97)))
        except Exception as e:
            logging.getLogger("fanyi").warning("窗口属性设置失败: %s", e)

    def toggle_collapse(self):
        if self.collapsed:
            self.scroll.pack(fill="both", expand=True)
            self.root.geometry(self.normal_geometry)
            self.collapsed = False
        else:
            self.normal_geometry = self.root.geometry()
            self.scroll.pack_forget()
            self.root.update_idletasks()
            self.root.geometry(
                f"{self.root.winfo_reqwidth()}x{self.root.winfo_reqheight()}")
            self.collapsed = True

    # ------------------------------------------------------------ 引擎 / 语言

    def _on_engine_pick(self, _e=None):
        if self._engine_syncing:
            return
        label = self.engine_box.get()
        eid = engine_labels().get(label)
        if not eid:
            return
        self.cfg["engine"] = eid
        spec = engines.get_engine(eid)
        missing = engines.missing_params(eid, self.cfg)
        if missing:
            # 需要 Key —— 立刻向用户索要,并给出获取 Key 的网址入口
            self._set_status(f"「{spec.name}」需要 API Key,请填写…")
            if settings_ui.ask_for_key(
                    self.root, self.cfg, eid,
                    extra_msg="这个引擎必须填写 API Key 才能使用。"):
                self._set_status(f"已保存 {spec.name} 的 Key")
            else:
                self._set_status("未填写 Key,该引擎暂时不可用")
        else:
            self._set_status(f"已切换到 {spec.name}")
        try:
            core.save_config(self.cfg)
        except Exception:
            pass
        self._sync_engine_widgets()

    def _on_lang_pick(self, _e=None):
        if self._engine_syncing:
            return
        name = self.lang_box.get()
        code = dict((n, c) for n, c in engines.TARGET_LANGS).get(name, "zh-CN")
        self.cfg["target_lang"] = code
        self.cfg["target_name"] = name
        try:
            core.save_config(self.cfg)
        except Exception:
            pass
        self._set_status(f"目标语言:{name}")

    def _on_source_pick(self, _e=None):
        if self._engine_syncing:
            return
        name = self.source_box.get()
        code = dict((n, c) for n, c in engines.SOURCE_LANGS).get(name, "auto")
        self.cfg["source_lang"] = code
        try:
            core.save_config(self.cfg)
        except Exception:
            pass
        self._set_status(f"源语言:{name}"
                         + ("(短句识别不准时可以指定具体语言)" if code == "auto"
                            else ""))

    # ------------------------------------------------------------ 启动准备

    def _start_startup_prep(self):
        threading.Thread(target=self._startup_prep, daemon=True).start()

    def _startup_prep(self):
        self.events.put(("status", "正在准备识别引擎…"))
        core.warmup_rapidocr()

        eid = self.cfg.get("engine", "mymemory")
        missing = engines.missing_params(eid, self.cfg)
        if missing:
            self.events.put(("probe", (None,
                                       f"「{engines.engine_name(eid)}」还缺 API Key")))
            return

        self.events.put(("status", "正在检测翻译引擎…"))
        if self._probe_engine(eid):
            self.events.put(("probe", (eid, None)))
            return

        # 首选引擎连不上:在其它「免费且不需要 Key」的引擎里找一个能顶上的,
        # 免得用户一打开就撞在连不通的引擎上(比如国内访问不了 Google)
        spec = engines.get_engine(eid)
        if spec is not None and spec.free:
            for other in engines.all_engines():
                if other.id == eid or not other.free:
                    continue
                if engines.missing_params(other.id, self.cfg):
                    continue
                if self._probe_engine(other.id):
                    self.cfg["engine"] = other.id
                    try:
                        core.save_config(self.cfg)
                    except Exception:
                        pass
                    self.events.put(("probe", (other.id, None)))
                    self.events.put((
                        "status",
                        f"就绪 · 「{spec.name}」连不上,已自动改用 {other.name}"))
                    self.events.put(("engine_changed", None))
                    return
        self.events.put(("probe", (None, None)))

    def _probe_engine(self, engine_id, timeout=4):
        """真连一次接口,看这个引擎通不通。超时设短一点,免得启动卡太久。

        探测失败就顺手把它标记成「冷却」。这样用户第一次点翻译时,不会再在
        已知不通的引擎上白等一轮超时 —— 国内 Google 连不上、免费接口限流是
        常态,一路试过去能拖到几十秒,而且全程界面都是"正在识别…"。
        """
        spec = engines.get_engine(engine_id)
        if spec is None:
            return False
        ok = False
        try:
            ok = bool(spec.runner("hello", self.cfg, "zh-CN",
                                  timeout=timeout).strip())
        except TypeError:
            # 万一某个引擎的 runner 不接受 timeout
            try:
                ok = bool(spec.runner("hello", self.cfg, "zh-CN").strip())
            except Exception as e:
                logging.getLogger("fanyi").info("引擎 %s 探测失败: %s",
                                                engine_id, e)
        except Exception as e:
            logging.getLogger("fanyi").info("引擎 %s 探测失败: %s", engine_id, e)
        if ok:
            engines.mark_good(engine_id)
        else:
            engines.mark_down(engine_id)
        return ok

    # ------------------------------------------------------------ 截图翻译

    def trigger_capture(self):
        if self.busy or self.capturing or self.rt.active:
            return
        self.capturing = True
        self._set_status("请框选要翻译的区域…")
        self.root.withdraw()      # 隐藏小窗,避免被截进去
        self.root.update()
        self.root.after(260, self._do_capture)

    def _do_capture(self):
        rect = None
        try:
            rect = selector.select_region(
                self.root, title="框选要翻译的区域",
                hint="按住左键拖动框选,松开即识别并翻译\n右键或 Esc 取消")
        except Exception:
            logging.getLogger("fanyi").exception("框选失败")
        self.root.deiconify()
        self.capturing = False
        if rect:
            self._start_worker(rect)
        else:
            self._set_status("已取消")

    def _start_worker(self, rect):
        if self.busy:
            return
        self.busy = True
        self._set_status("正在识别…")
        threading.Thread(target=self._worker_ocr, args=(rect,),
                         daemon=True).start()

    def _worker_ocr(self, rect):
        try:
            img = core.capture_region(rect)
        except Exception as e:
            self.events.put(("error", f"截图失败:{e}"))
            return
        try:
            raw, ocr_used = core.ocr_image_ex(
                img, self.cfg.get("ocr_lang", "auto"),
                self.cfg.get("ocr_engine", "auto"), self.cfg)
            if not raw:
                self.events.put(("error",
                                 "未识别到文字。换个区域重试,或到设置里切换 OCR 语言。"))
                return
            text = core.merge_ocr_lines(raw)
            self.events.put(("source", text))
            self.events.put(("status", f"正在翻译…({ocr_used})"))
            self._translate_text(text)
        except core.MissingKeyError as e:
            self.events.put(("needkey", e))
        except core.OcrError as e:
            self.events.put(("error", f"识别失败:{e}"))
        except Exception as e:
            logging.getLogger("fanyi").exception("后台任务出错")
            self.events.put(("error", f"出错了:{e}"))

    def _translate_text(self, text):
        try:
            result, used = engines.translate(
                text, self.cfg, self.cfg.get("engine"), allow_fallback=True)
            if not result:
                self.events.put(("error", "翻译结果为空,请重试"))
                return
            self.events.put(("result", (result, used)))
        except core.MissingKeyError as e:
            self.events.put(("needkey", e))
        except Exception as e:
            self.events.put(("error", f"翻译失败:{e}"))

    # ------------------------------------------------------------ 剪贴板/重译

    def translate_clipboard(self):
        if self.busy or self.rt.active:
            return
        if pyperclip is None:
            self._set_status("剪贴板组件不可用")
            return
        try:
            text = (pyperclip.paste() or "").strip()
        except Exception as e:
            self._set_status(f"读取剪贴板失败:{e}")
            return
        if not text:
            self._set_status("剪贴板为空")
            return
        self.busy = True
        self._set_source(text)
        self._set_status("正在翻译剪贴板…")
        threading.Thread(target=self._worker_text, args=(text,),
                         daemon=True).start()

    def retranslate(self):
        """用（可能被用户改过的）原文重新翻译一次。"""
        if self.busy or self.rt.active:
            return
        text = self.src_text.get("1.0", "end").strip()
        if not text:
            self._set_status("原文是空的")
            return
        self.busy = True
        self._set_status("正在翻译…")
        threading.Thread(target=self._worker_text, args=(text,),
                         daemon=True).start()

    def _worker_text(self, text):
        try:
            self._translate_text(text)
        except Exception as e:
            logging.getLogger("fanyi").exception("翻译出错")
            self.events.put(("error", f"出错了:{e}"))

    # ------------------------------------------------------------ 实时翻译

    def start_realtime(self):
        if self.rt.active:
            self.rt.toggle_translation()
            return
        if self.busy or self.capturing:
            return
        self.capturing = True
        self._set_status("请框选要实时翻译的区域…")
        self.root.withdraw()      # 实时翻译期间主窗口保持隐藏
        self.root.update()
        self.root.after(260, self._do_start_realtime)

    def _do_start_realtime(self):
        self.capturing = False
        rect = None
        try:
            rect = selector.select_region(
                self.root, initial=self.cfg.get("rt_region"),
                title="框选要实时翻译的区域",
                hint="框住字幕/文字所在的那一小块区域即可\n"
                     "松开鼠标即开始实时翻译 ·右键或 Esc 取消")
        except Exception:
            logging.getLogger("fanyi").exception("实时翻译框选失败")
        if not rect:
            self.root.deiconify()
            self._set_status("已取消实时翻译")
            return
        self.rt.start(rect)
        self._set_status(f"实时翻译进行中,监视范围 {self.rt._fmt_region()}")

    def _on_realtime_exit(self):
        """「退出实时翻译」后回到主界面。"""
        self.rt.save()
        try:
            self.root.deiconify()
        except Exception:
            pass
        self._set_status("已退出实时翻译")

    def _on_realtime_need_key(self, engine_id):
        """实时翻译中途发现缺 Key:立刻问用户要。"""
        if not engine_id:
            return
        # 实时翻译期间主窗口是隐藏的,而模态对话框要有个能显示的父窗口,
        # 所以先把主窗口临时亮出来,问完再藏回去。
        restored = False
        try:
            if not self.root.winfo_viewable():
                self.root.deiconify()
                self.root.update()
                restored = True
        except Exception:
            pass
        try:
            saved = settings_ui.ask_for_key(
                self.root, self.cfg, engine_id,
                extra_msg="实时翻译已暂停,填好 Key 后会自动继续。")
        finally:
            if restored and self.rt.active:
                try:
                    self.root.withdraw()
                except Exception:
                    pass
        if saved:
            self._set_status("Key 已保存,继续实时翻译")
            self.rt.resume_translation()
        else:
            self._set_status("实时翻译已暂停:缺少 API Key")

    def toggle_realtime_hotkey(self):
        if self.rt.active:
            self.rt.toggle_translation()
        else:
            self.start_realtime()

    # ------------------------------------------------------------ 历史

    def open_history(self):
        win = tk.Toplevel(self.root)
        win.title("翻译历史")
        win.configure(bg="#f4f6f9")
        win.geometry("560x420")
        win.attributes("-topmost", True)
        tk.Label(win, text=f"最近 {len(self.history)} 条(双击可载入到主窗口)",
                 bg="#f4f6f9", anchor="w", font=(FONT, 9)).pack(
            fill="x", padx=10, pady=(8, 4))
        box = tk.Listbox(win, font=(FONT, 9), activestyle="none")
        box.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        for i, item in enumerate(self.history):
            first = (item["source"] or "").replace("\n", " ")[:44]
            box.insert("end", f"{i + 1:>2}. [{item['time']}] {first}")

        def load(_e=None):
            sel = box.curselection()
            if not sel:
                return
            item = self.history[sel[0]]
            self._set_source(item["source"])
            self._set_result(item["result"], item.get("engine", ""))
            win.destroy()
        box.bind("<Double-Button-1>", load)

    def _push_history(self, source, result, used):
        self.history.insert(0, {"source": source, "result": result,
                                "engine": used,
                                "time": time.strftime("%H:%M:%S")})
        limit = int(self.cfg.get("history_limit", 30) or 30)
        del self.history[limit:]

    # ------------------------------------------------------------ 事件循环

    def _poll_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _handle_event(self, kind, payload):
        if kind == "status":
            self._set_status(payload)
        elif kind == "probe":
            used, warn = payload
            if warn:
                self._set_status(f"就绪 · {warn}")
            elif used:
                self._set_status(f"就绪(翻译引擎:{engines.engine_name(used)})")
            else:
                spec = engines.get_engine(self.cfg.get("engine", ""))
                name = spec.name if spec else "当前引擎"
                self._set_status(f"就绪 · {name} 暂时连不上,可到设置里换一个引擎")
        elif kind == "engine_changed":
            self._sync_engine_widgets()
        elif kind == "source":
            self.last_source = payload
            self._set_source(payload)
        elif kind == "result":
            result, used = payload
            self.last_result = result
            self.last_engine = used
            self.busy = False
            self._set_result(result, used)
            self._push_history(self.last_source, result, used)
            if self.cfg.get("auto_copy") and pyperclip is not None:
                try:
                    pyperclip.copy(result)
                except Exception:
                    pass
            extra = "(已自动复制)" if self.cfg.get("auto_copy") else ""
            self._set_status(f"完成 ✓ {extra}引擎:{engines.engine_name(used)}")
        elif kind == "needkey":
            self.busy = False
            eid = getattr(payload, "engine_id", None)
            spec = engines.get_engine(eid)
            if spec and settings_ui.ask_for_key(self.root, self.cfg, eid,
                                                extra_msg=str(payload)):
                self._sync_engine_widgets()
                self._set_status("Key 已保存,可以再点一次翻译了")
            else:
                self._render_error(str(payload))
                self._set_status("缺少 API Key")
        elif kind == "error":
            self.busy = False
            self._render_error(payload)
            self._set_status("出错")
        elif kind == "hotkey_capture":
            self.trigger_capture()
        elif kind == "hotkey_realtime":
            self.toggle_realtime_hotkey()
        elif kind == "hotkey_clickthrough":
            if self.rt.active:
                self.rt.toggle_click_through()
            else:
                self._set_status("实时翻译没在运行")

    # ------------------------------------------------------------ 文本渲染

    def _set_source(self, text):
        self.src_text.delete("1.0", "end")
        self.src_text.insert("1.0", text or "")

    def _set_result(self, text, used=""):
        self.dst_text.configure(state="normal")
        self.dst_text.delete("1.0", "end")
        self.dst_text.insert("1.0", text or "")
        self.dst_text.configure(state="disabled")
        if used:
            self.last_engine = used

    def _render_error(self, msg):
        self.dst_text.configure(state="normal")
        self.dst_text.delete("1.0", "end")
        self.dst_text.insert("end", msg, "err")
        self.dst_text.configure(state="disabled")

    def _set_status(self, msg):
        try:
            self.status.configure(text=msg)
        except Exception:
            pass

    def clear_all(self):
        self._set_source("")
        self._set_result("")
        self.last_source = self.last_result = ""
        self._set_status("已清空")

    # ------------------------------------------------------------ 复制

    def _copy(self, value, what):
        if not value:
            self._set_status(f"没有可复制的{what}")
            return
        if pyperclip is None:
            self._set_status("剪贴板组件不可用")
            return
        try:
            pyperclip.copy(value)
            self._set_status(f"已复制{what} ✓")
        except Exception as e:
            self._set_status(f"复制失败:{e}")

    def copy_result(self):
        self._copy(self.dst_text.get("1.0", "end").strip(), "译文")

    def copy_source(self):
        self._copy(self.src_text.get("1.0", "end").strip(), "原文")

    # ------------------------------------------------------------ 快捷键

    def _setup_hotkeys(self):
        if _keyboard is None:
            return
        try:
            _keyboard.unhook_all()
        except Exception:
            pass
        pairs = (("hotkey", "hotkey_capture"),
                 ("hotkey_realtime", "hotkey_realtime"),
                 ("hotkey_clickthrough", "hotkey_clickthrough"))
        for cfg_key, event in pairs:
            hk = (self.cfg.get(cfg_key) or "").strip()
            if not hk:
                continue
            try:
                _keyboard.add_hotkey(
                    hk, lambda e=event: self.events.put((e, None)))
                logging.getLogger("fanyi").info("快捷键已注册: %s -> %s",
                                                 hk, event)
            except Exception as e:
                logging.getLogger("fanyi").warning(
                    "快捷键注册失败 %s: %s", hk, e)

    # ------------------------------------------------------------ 设置

    def open_settings(self):
        settings_ui.open_settings(self.root, self.cfg,
                                  on_saved=self._on_settings_saved)

    def _on_settings_saved(self):
        self._apply_window_attrs()
        self._setup_hotkeys()
        self._refresh_hotkey_labels()
        self._sync_engine_widgets()
        if self.rt.overlay and self.rt.overlay.winfo_exists():
            self.rt.overlay.apply_style()
            self.rt.set_click_through(bool(self.cfg.get("rt_click_through")))
        self._set_status("设置已保存")

    # ------------------------------------------------------------ 退出

    def on_close(self):
        if self.rt.active:
            if not messagebox.askyesno(
                    "实时翻译还在运行",
                    "实时翻译还在运行,退出程序会一并结束它。确定退出吗?",
                    parent=self.root):
                return
            self.rt.exit_realtime()
        try:
            if _keyboard is not None:
                _keyboard.unhook_all()
        except Exception:
            pass
        try:
            core.save_config(self.cfg)
        except Exception:
            pass
        self.root.destroy()


def run_selftest(app):
    """自检:验证 OCR 和翻译到底能不能用,结果写进日志并弹窗显示。

    打包成 exe 之后没有控制台,别人用着不对时,让他在命令行加 --selftest
    跑一下(或直接双击「自检.bat」),把弹窗内容或 fanyi.log 发回来即可。
    """
    log = logging.getLogger("fanyi")
    lines = []
    ok_all = True

    lines.append("程序版本:屏幕翻译")
    lines.append(f"Python:{sys.version.split()[0]}"
                 f"  {'(打包 exe)' if core.FROZEN else '(源码运行)'}")
    lines.append(f"配置目录:{core.APP_DIR}")
    lines.append(f"当前引擎:{engines.engine_name(app.cfg.get('engine'))}"
                 f" / OCR:{core.ocr_engine_name(app.cfg.get('ocr_engine'))}")
    lines.append("")

    # ---- 1. OCR ----
    try:
        img = core.make_test_image("Hello 2025")
        text, used = core.ocr_image_ex(img, "auto",
                                       app.cfg.get("ocr_engine", "auto"),
                                       app.cfg)
        good = "Hello" in text and "2025" in text
        ok_all &= good
        lines.append(f"[{'通过' if good else '失败'}] OCR({used}):"
                     f"识别到 {text.strip()[:40]!r}")
    except Exception as e:
        ok_all = False
        lines.append(f"[失败] OCR:{type(e).__name__}: {e}")

    # ---- 2. 宽条按行识别(容易出问题的那条路径)----
    try:
        from PIL import Image, ImageDraw, ImageFont
        wide = Image.new("RGB", (1000, 110), "#101418")
        d = ImageDraw.Draw(wide)
        fnt = None
        for p in (r"C:\Windows\Fonts\arial.ttf",
                  r"C:\Windows\Fonts\segoeui.ttf"):
            try:
                fnt = ImageFont.truetype(p, 22)
                break
            except Exception:
                continue
        d.text((14, 20), "First line here", fill="white", font=fnt)
        d.text((14, 60), "Second line here", fill="white", font=fnt)
        wtext, _u = core.ocr_image_ex(wide, "auto", "rapidocr", app.cfg)
        good = "line" in wtext.lower()
        ok_all &= good
        lines.append(f"[{'通过' if good else '失败'}] 宽条逐行识别:"
                     f"{wtext.replace(chr(10), ' / ')[:40]!r}")
    except Exception as e:
        ok_all = False
        lines.append(f"[失败] 宽条逐行识别:{type(e).__name__}: {e}")

    # ---- 3. 翻译 ----
    try:
        out, used = engines.translate("Hello world", app.cfg,
                                      app.cfg.get("engine"))
        good = bool(out.strip()) and out.strip().lower() != "hello world"
        ok_all &= good
        lines.append(f"[{'通过' if good else '失败'}] 翻译({engines.engine_name(used)}):"
                     f"{out.strip()[:40]!r}")
    except Exception as e:
        ok_all = False
        lines.append(f"[失败] 翻译:{e}")
        lines.append("      → 到「设置 → 翻译引擎」换一个引擎,"
                     "或检查 API Key / 网络")

    lines.append("")
    lines.append("全部通过 ✓" if ok_all else "有项目没通过 ✗ —— 把这段内容发回来即可")
    text = "\n".join(lines)
    for ln in lines:
        log.info("[自检] %s", ln)
    try:
        from tkinter import messagebox
        (messagebox.showinfo if ok_all else messagebox.showwarning)(
            "屏幕翻译 · 自检结果", text)
    except Exception:
        pass
    return 0 if ok_all else 1


def main():
    setup_logging()
    log = logging.getLogger("fanyi")
    log.info("=== 启动 ===")
    root = tk.Tk()
    app = App(root)
    if "--selftest" in sys.argv:
        log.info("自检模式")
        root.after(600, lambda: (run_selftest(app), app.on_close()))
    elif "--smoke" in sys.argv:
        ms = 4000
        i = sys.argv.index("--smoke")
        if i + 1 < len(sys.argv):
            try:
                ms = int(sys.argv[i + 1])
            except ValueError:
                pass
        log.info("冒烟测试模式:%d 毫秒后自动退出", ms)
        root.after(ms, app.on_close)
    root.mainloop()
    log.info("=== 退出 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
