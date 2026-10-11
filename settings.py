# -*- coding: utf-8 -*-
"""设置界面:翻译引擎 / OCR 引擎 / 实时翻译 / 通用。

关于 API Key(需求 3):
  * 引擎列表里每个引擎都标注了「免费」或「需 Key」;
  * 选中「需 Key」的引擎时,下面会自动展开 Key 输入框,并在输入框**下方**
    给出可点击的「获取 Key」网址入口(点一下用浏览器打开);
  * 翻译时才发现缺 Key,会弹出 KeyPromptDialog 直接向用户索要。

每个选项卡都是可滚动的:窗口不够高时出现滚动条,鼠标滚轮也能上下翻。
"""
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import core
import engines
import theme
from theme import CARD, MUTED, OK_FG, ERR_FG, TEXT, WARN, WARN_BG, BORDER

FONT = theme.FONT


def link_label(master, text, url, bg=CARD, **kw):
    return theme.link_label(master, text, url, core.open_url, bg=bg, **kw)


def badge(master, is_free, bg=CARD):
    return theme.badge(master, is_free, bg=bg)


def section_title(master, text, bg=CARD):
    tk.Label(master, text=text, bg=bg, fg=TEXT, anchor="w",
             font=(FONT, 10, "bold")).pack(fill="x", pady=(14, 4))
    tk.Frame(master, bg=BORDER, height=1).pack(fill="x", pady=(0, 6))


def hint(master, text, bg=CARD, wraplength=600):
    tk.Label(master, text=text, bg=bg, fg=MUTED, anchor="w", justify="left",
             wraplength=wraplength, font=(FONT, 8)).pack(fill="x")


_OCR_LANGS = [
    ("自动(推荐)", "auto"),
    ("English", "en"),
    ("中文", "zh"),
    ("日本語", "ja"),
    ("한국어", "ko"),
    ("Français", "fr"),
    ("Deutsch", "de"),
]


class ParamForm(tk.Frame):
    """按 ParamSpec 列表渲染一组输入框(Key、接口地址、模型名……)。"""

    def __init__(self, master, params, cfg, bg=CARD):
        super().__init__(master, bg=bg)
        self.cfg = cfg
        self.bg = bg
        self.vars = {}
        self.entries = {}
        self._show = {}

        for p in params:
            row = tk.Frame(self, bg=bg)
            row.pack(fill="x", pady=(8, 0))

            title = p.label + ("  *必填" if p.required else "")
            tk.Label(row, text=title, bg=bg, fg=TEXT, anchor="w",
                     font=(FONT, 9, "bold")).pack(anchor="w")

            line = tk.Frame(row, bg=bg)
            line.pack(fill="x", pady=(3, 0))
            var = tk.StringVar(value=str(cfg.get(p.key, p.default) or ""))
            self.vars[p.key] = var

            if p.choices:
                ent = ttk.Combobox(line, textvariable=var, values=p.choices,
                                   state="readonly")
            else:
                ent = tk.Entry(line, textvariable=var, show="*" if p.secret else "",
                               relief="flat", bg=CARD, fg=TEXT,
                               highlightbackground=BORDER, highlightthickness=1,
                               insertbackground=TEXT, font=(FONT, 10))
            ent.pack(side="left", fill="x", expand=True, ipady=4)
            self.entries[p.key] = ent
            self._show[p.key] = False

            if p.secret:
                def toggle(key=p.key):
                    self._show[key] = not self._show[key]
                    self.entries[key].configure(
                        show="" if self._show[key] else "*")
                    show_btn.configure(text="隐藏" if self._show[key] else "显示")
                show_btn = theme.flat_button(line, "显示", toggle, padx=10, pady=4)
                show_btn.pack(side="left", padx=(5, 0))

            hint_text = " ".join(x for x in (
                p.help, f"例:{p.placeholder}" if p.placeholder else "") if x)
            if hint_text:
                tk.Label(row, text=hint_text, bg=bg, fg=MUTED, anchor="w",
                         wraplength=560, justify="left",
                         font=(FONT, 8)).pack(anchor="w", pady=(2, 0))
            if p.url:
                link_label(row, f"👉 点此获取 / 查看:{p.url}", p.url,
                           bg=bg).pack(anchor="w", pady=(1, 0))

    def commit(self):
        for key, var in self.vars.items():
            self.cfg[key] = str(var.get()).strip()


class SettingsWindow(tk.Toplevel):
    """主设置窗口。四个选项卡各自可滚动。"""

    def __init__(self, master, cfg, on_saved=None, focus_engine=None):
        super().__init__(master)
        self.cfg = dict(cfg)              # 先改副本,点保存才写回
        self.orig = cfg
        self.on_saved = on_saved
        self._areas = []

        self.title("设置")
        self.configure(bg=theme.BG)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = max(560, min(760, sw - 80)), max(520, min(820, sh - 120))
        self.geometry(f"{w}x{h}")
        self.minsize(520, 420)
        try:
            self.attributes("-topmost", True)
        except Exception:
            pass

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(12, 6))

        self._build_translate_tab(self.nb)
        self._build_ocr_tab(self.nb)
        self._build_realtime_tab(self.nb)
        self._build_general_tab(self.nb)

        # 窗口内所有滚动区都接上鼠标滚轮
        theme.enable_wheel_scroll(self, self._areas)

        bottom = tk.Frame(self, bg=theme.BG)
        bottom.pack(fill="x", padx=14, pady=(0, 12))
        self.hint = tk.Label(bottom, text="", bg=theme.BG, fg=MUTED,
                             font=(FONT, 8), anchor="w")
        self.hint.pack(side="left")
        theme.flat_button(bottom, "保存", self.save, kind="primary",
                          font=(FONT, 10, "bold"), padx=26, pady=6).pack(
            side="right", padx=(6, 0))
        theme.flat_button(bottom, "取消", self.destroy, padx=22, pady=6).pack(
            side="right")

        self.engine_var.set(self.cfg.get("engine", "mymemory"))
        self._on_engine_change()
        self._on_ocr_change()
        if focus_engine:
            self.engine_var.set(focus_engine)
            self._on_engine_change()
            self.nb.select(0)

    # ------------------------------------------------------ 工具

    def _scroll_tab(self, nb, title, bg=CARD):
        """建一个带滚动条的选项卡,返回可以往里放内容的 Frame。"""
        holder = tk.Frame(nb, bg=bg)
        nb.add(holder, text=f"  {title}  ")
        area = theme.ScrollArea(holder, bg=bg,
                                bar_style="Card.Vertical.TScrollbar")
        area.pack(fill="both", expand=True)
        self._areas.append(area)
        page = tk.Frame(area.inner, bg=bg)
        page.pack(fill="both", expand=True, padx=16, pady=14)
        return page

    # ------------------------------------------------------ 翻译引擎页

    def _build_translate_tab(self, nb):
        page = self._scroll_tab(nb, "翻译引擎")

        tk.Label(page, text="选择用哪个翻译引擎", bg=CARD, fg=TEXT,
                 anchor="w", font=(FONT, 12, "bold")).pack(fill="x")
        hint(page, "免费引擎开箱即用;标注「需 Key」的引擎要填自己的 API Key,"
                   "填的时候下面会直接给出申请网址。")

        self.engine_var = tk.StringVar(value=self.cfg.get("engine", "mymemory"))
        picker = tk.Frame(page, bg=CARD)
        picker.pack(fill="x", pady=(10, 0))
        for i, spec in enumerate(engines.all_engines()):
            cell = tk.Frame(picker, bg=CARD)
            cell.grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 24),
                      pady=4)
            ttk.Radiobutton(cell, variable=self.engine_var, value=spec.id,
                            style="Card.TRadiobutton", text=spec.name,
                            command=self._on_engine_change).pack(side="left")
            badge(cell, spec.free).pack(side="left", padx=(6, 0))

        section_title(page, "引擎配置")

        # 选中引擎的详情 + Key 表单
        self.engine_detail = tk.Frame(page, bg=CARD)
        self.engine_detail.pack(fill="both", expand=True)

        self.engine_key_form = None
        self.engine_test_lbl = None

    def _on_engine_change(self):
        for w in self.engine_detail.winfo_children():
            w.destroy()
        self.engine_key_form = None
        self.engine_test_lbl = None

        spec = engines.get_engine(self.engine_var.get())
        if spec is None:
            return
        box = self.engine_detail

        head = tk.Frame(box, bg=CARD)
        head.pack(fill="x")
        tk.Label(head, text=spec.name, bg=CARD, fg=TEXT,
                 font=(FONT, 13, "bold"), anchor="w").pack(side="left")
        badge(head, spec.free).pack(side="left", padx=(8, 0))
        if spec.note:
            tk.Label(box, text=spec.note, bg=CARD, fg=MUTED, font=(FONT, 9),
                     wraplength=600, justify="left", anchor="w").pack(
                fill="x", pady=(5, 0))

        if spec.needs_key:
            # —— 需要 Key:醒目地索要,并在下面给出获取 Key 的网址入口 ——
            warn = tk.Frame(box, bg=WARN_BG, highlightbackground="#f0d9b5",
                            highlightthickness=1)
            warn.pack(fill="x", pady=(12, 0))
            tk.Label(warn, text=f"⚠  「{spec.name}」需要 API Key 才能使用",
                     bg=WARN_BG, fg=WARN, font=(FONT, 10, "bold"),
                     anchor="w").pack(fill="x", padx=12, pady=(9, 3))
            if spec.key_help:
                tk.Label(warn, text=spec.key_help, bg=WARN_BG, fg="#7a5a2a",
                         font=(FONT, 9), wraplength=580, justify="left",
                         anchor="w").pack(fill="x", padx=12)
            if spec.key_url:
                link_label(warn, f"👉 点此获取 Key:{spec.key_url}",
                           spec.key_url, bg=WARN_BG,
                           font=(FONT, 10, "underline")).pack(
                    anchor="w", padx=12, pady=(6, 10))

            self.engine_key_form = ParamForm(box, spec.params, self.cfg)
            self.engine_key_form.pack(fill="x", pady=(10, 0))

            test_row = tk.Frame(box, bg=CARD)
            test_row.pack(fill="x", pady=(12, 0))
            theme.flat_button(test_row, "🔌 测试连接", self._test_engine,
                              padx=14, pady=6).pack(side="left")
            self.engine_test_lbl = tk.Label(test_row, text="", bg=CARD,
                                            fg=MUTED, font=(FONT, 9), anchor="w",
                                            wraplength=380, justify="left")
            self.engine_test_lbl.pack(side="left", padx=10)
        else:
            ok = tk.Frame(box, bg="#eefaf3", highlightbackground="#c8ebd9",
                          highlightthickness=1)
            ok.pack(fill="x", pady=(12, 0))
            tk.Label(ok, text="✓  这个引擎不需要 API Key,直接就能用。",
                     bg="#eefaf3", fg=OK_FG, font=(FONT, 10, "bold"),
                     anchor="w").pack(fill="x", padx=12, pady=9)
            if spec.params:
                self.engine_key_form = ParamForm(box, spec.params, self.cfg)
                self.engine_key_form.pack(fill="x", pady=(6, 0))
            if spec.key_url:
                link_label(box, f"👉 {spec.key_help or '相关页面'}:{spec.key_url}",
                           spec.key_url, bg=CARD).pack(anchor="w", pady=(8, 0))

    def _test_engine(self):
        if self.engine_key_form:
            self.engine_key_form.commit()
        eid = self.engine_var.get()
        lbl = self.engine_test_lbl
        if lbl is None:
            return
        lbl.configure(text="正在测试…", fg=MUTED)
        cfg = self.cfg

        def work():
            ok, msg = engines.test_engine(eid, cfg)

            def done():
                if lbl.winfo_exists():
                    lbl.configure(text=("✓ " if ok else "✗ ") + msg,
                                  fg=OK_FG if ok else ERR_FG)
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------ OCR 页

    def _build_ocr_tab(self, nb):
        page = self._scroll_tab(nb, "文字识别 OCR")

        tk.Label(page, text="用哪个引擎识别屏幕上的文字", bg=CARD, fg=TEXT,
                 anchor="w", font=(FONT, 12, "bold")).pack(fill="x")
        hint(page, "实时翻译对速度要求高,本地引擎最快;在线引擎更准但需要联网和 Key。")

        self.ocr_var = tk.StringVar(value=self.cfg.get("ocr_engine", "auto"))
        for eid, spec in core.ocr_engines():
            cell = tk.Frame(page, bg=CARD)
            cell.pack(fill="x", pady=3)
            ttk.Radiobutton(cell, variable=self.ocr_var, value=eid,
                            style="Card.TRadiobutton", text=spec["name"],
                            command=self._on_ocr_change).pack(side="left")
            badge(cell, not spec["needs_key"]).pack(side="left", padx=(6, 0))
            tk.Label(cell, text=spec["desc"], bg=CARD, fg=MUTED,
                     font=(FONT, 8), anchor="w", justify="left",
                     wraplength=380).pack(side="left", padx=8)

        section_title(page, "识别语言")

        row = tk.Frame(page, bg=CARD)
        row.pack(fill="x")
        self.ocr_lang_var = tk.StringVar(
            value=dict((c, n) for n, c in _OCR_LANGS).get(
                self.cfg.get("ocr_lang", "auto"), "自动(推荐)"))
        ttk.Combobox(row, textvariable=self.ocr_lang_var, state="readonly",
                     width=20, values=[n for n, _ in _OCR_LANGS]).pack(
            side="left")
        hint(page, "「自动」= 英文优先,再中文、日文、韩文。识别日韩文字建议手动指定。")

        section_title(page, "引擎配置")
        self.ocr_detail = tk.Frame(page, bg=CARD)
        self.ocr_detail.pack(fill="x")
        self.ocr_key_form = None

        test_row = tk.Frame(page, bg=CARD)
        test_row.pack(fill="x", pady=(14, 0))
        theme.flat_button(test_row, "🔍 用测试图试一次识别", self._test_ocr,
                          padx=14, pady=6).pack(side="left")
        self.ocr_test_lbl = tk.Label(test_row, text="", bg=CARD, fg=MUTED,
                                     font=(FONT, 9), anchor="w",
                                     wraplength=380, justify="left")
        self.ocr_test_lbl.pack(side="left", padx=10)

    def _on_ocr_change(self):
        if not hasattr(self, "ocr_detail"):
            return
        for w in self.ocr_detail.winfo_children():
            w.destroy()
        self.ocr_key_form = None
        spec = core.OCR_SPECS.get(self.ocr_var.get())
        if not spec:
            return
        box = self.ocr_detail
        if spec["needs_key"]:
            warn = tk.Frame(box, bg=WARN_BG, highlightbackground="#f0d9b5",
                            highlightthickness=1)
            warn.pack(fill="x")
            tk.Label(warn, text=f"⚠  「{spec['name']}」需要百度智能云的 Key",
                     bg=WARN_BG, fg=WARN, font=(FONT, 10, "bold"),
                     anchor="w").pack(fill="x", padx=12, pady=(9, 3))
            if spec["key_help"]:
                tk.Label(warn, text=spec["key_help"], bg=WARN_BG, fg="#7a5a2a",
                         font=(FONT, 9), wraplength=580, justify="left",
                         anchor="w").pack(fill="x", padx=12)
            if spec["key_url"]:
                link_label(warn, f"👉 点此获取 Key:{spec['key_url']}",
                           spec["key_url"], bg=WARN_BG,
                           font=(FONT, 10, "underline")).pack(
                    anchor="w", padx=12, pady=(6, 10))
            self.ocr_key_form = ParamForm(box, spec["params"], self.cfg)
            self.ocr_key_form.pack(fill="x", pady=(8, 0))
        else:
            tk.Label(box, text="✓  这个 OCR 引擎不需要 Key。", bg=CARD,
                     fg=OK_FG, font=(FONT, 10, "bold"), anchor="w").pack(fill="x")

    def _test_ocr(self):
        if self.ocr_key_form:
            self.ocr_key_form.commit()
        eid = self.ocr_var.get()
        lbl = self.ocr_test_lbl
        lbl.configure(text="正在识别…", fg=MUTED)
        cfg = self.cfg

        def work():
            try:
                img = core.make_test_image("Hello 2025")
                text, used = core.ocr_image_ex(img, cfg.get("ocr_lang", "auto"),
                                               eid, cfg)
                ok, msg = bool(text.strip()), f"{used}:{text.strip()[:40]}"
            except Exception as e:
                ok, msg = False, f"{type(e).__name__}: {e}"

            def done():
                if lbl.winfo_exists():
                    lbl.configure(text=("✓ " if ok else "✗ ") + msg,
                                  fg=OK_FG if ok else ERR_FG)
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------ 实时翻译页

    def _build_realtime_tab(self, nb):
        page = self._scroll_tab(nb, "实时翻译")

        tk.Label(page, text="实时翻译悬浮窗的默认样式", bg=CARD, fg=TEXT,
                 anchor="w", font=(FONT, 12, "bold")).pack(fill="x")
        hint(page, "运行时也可以在悬浮窗里点「🎨 样式」随时改;位置直接拖动悬浮窗即可。")

        self.rt_vars = {}
        rows = [
            ("rt_interval", "刷新间隔(秒)", 0.3, 3.0,
             "越小越实时,越吃 CPU"),
            ("rt_opacity", "悬浮窗不透明度", 0.2, 1.0, ""),
            ("rt_font_size", "译文字号", 10, 48, ""),
            ("rt_overlay_w", "悬浮窗宽度", 260, 1200, "像素"),
            ("rt_dedupe", "重复判定阈值", 0.5, 1.0,
             "画面文字相似度超过它就沿用上次译文,不再请求接口"),
        ]
        for key, label, lo, hi, tip in rows:
            box = tk.Frame(page, bg=CARD)
            box.pack(fill="x", pady=(10, 0))
            tk.Label(box, text=label, bg=CARD, fg=TEXT, anchor="w",
                     font=(FONT, 9, "bold")).pack(fill="x")
            line = tk.Frame(box, bg=CARD)
            line.pack(fill="x", pady=(2, 0))
            try:
                initial = float(self.cfg.get(key, lo))
            except (TypeError, ValueError):
                initial = float(lo)
            var = tk.DoubleVar(value=initial)
            self.rt_vars[key] = var
            val = tk.Label(line, text=f"{initial:.2f}", bg=CARD, fg=TEXT,
                           width=7, anchor="w", font=(FONT, 9, "bold"))
            ttk.Scale(line, from_=lo, to=hi, variable=var, length=260,
                      command=lambda _v, v=var, l=val: l.configure(
                          text=f"{v.get():.2f}")).pack(side="left")
            val.pack(side="left", padx=8)
            if tip:
                hint(page, tip, wraplength=560)

        section_title(page, "上下文与速度")
        self.rt_context_var = tk.BooleanVar(
            value=bool(self.cfg.get("rt_context", True)))
        ttk.Checkbutton(page, text="联系上下文翻译(看对话/长文时更连贯)",
                        variable=self.rt_context_var,
                        style="Card.TCheckbutton").pack(anchor="w")
        self.rt_stream_var = tk.BooleanVar(
            value=bool(self.cfg.get("rt_stream", True)))
        ttk.Checkbutton(page, text="逐字显示(译文边生成边出现,感觉更快)",
                        variable=self.rt_stream_var,
                        style="Card.TCheckbutton").pack(anchor="w", pady=(6, 0))
        _spec = engines.get_engine(self.cfg.get("engine", ""))
        if _spec and _spec.supports_context:
            _tip, _color = (f"{_spec.name} 支持上下文,这两个开关直接生效。",
                            MUTED)
        else:
            _tip = (f"{(_spec.name if _spec else '当前引擎')} 读不懂前文,"
                    f"这两个开关对它无效 —— 想用请把引擎换成 DeepSeek "
                    f"或 OpenAI 兼容接口。")
            _color = WARN
        tk.Label(page, text=_tip, bg=CARD, fg=_color, anchor="w",
                 font=(FONT, 8), wraplength=560, justify="left").pack(
            fill="x", pady=(2, 0))

        section_title(page, "显示选项")
        self.rt_region_var = tk.BooleanVar(
            value=bool(self.cfg.get("rt_show_region", True)))
        ttk.Checkbutton(page, text="在屏幕上显示监视范围框(取消勾选即隐藏)",
                        variable=self.rt_region_var,
                        style="Card.TCheckbutton").pack(anchor="w")
        hint(page, "范围框画在选区外面,不会挡住识别,也不影响点击。")

        self.rt_source_var = tk.BooleanVar(
            value=bool(self.cfg.get("rt_show_source")))
        ttk.Checkbutton(page, text="悬浮窗里同时显示原文",
                        variable=self.rt_source_var,
                        style="Card.TCheckbutton").pack(anchor="w", pady=(6, 0))
        self.rt_topmost_var = tk.BooleanVar(
            value=bool(self.cfg.get("rt_topmost", True)))
        ttk.Checkbutton(page, text="悬浮窗始终置顶", variable=self.rt_topmost_var,
                        style="Card.TCheckbutton").pack(anchor="w")
        self.rt_merge_var = tk.BooleanVar(
            value=bool(self.cfg.get("rt_merge_lines", True)))
        ttk.Checkbutton(page, text="折行合并(把折成多行的句子拼回一句再翻)",
                        variable=self.rt_merge_var,
                        style="Card.TCheckbutton").pack(anchor="w")

        hint(page, f"上次框选的范围:{self.cfg.get('rt_region') or '还没有'}",
             wraplength=560)

    # ------------------------------------------------------ 通用页

    def _build_general_tab(self, nb):
        page = self._scroll_tab(nb, "通用")

        tk.Label(page, text="语言", bg=CARD, fg=TEXT, anchor="w",
                 font=(FONT, 12, "bold")).pack(fill="x")

        row = tk.Frame(page, bg=CARD)
        row.pack(fill="x", pady=(8, 0))
        tk.Label(row, text="源语言", bg=CARD, fg=TEXT, width=7, anchor="w",
                 font=(FONT, 9, "bold")).pack(side="left")
        self.source_var = tk.StringVar(
            value=dict((c, n) for n, c in engines.SOURCE_LANGS).get(
                self.cfg.get("source_lang", "auto"), "自动检测"))
        ttk.Combobox(row, textvariable=self.source_var, state="readonly",
                     width=18, values=[n for n, _ in engines.SOURCE_LANGS]).pack(
            side="left")
        hint(page, "选「自动检测」时,免费接口遇到很短的句子(字幕单词)可能认不出语言"
                   "而原样返回;这时把源语言设成具体语言会更准。")

        row2 = tk.Frame(page, bg=CARD)
        row2.pack(fill="x", pady=(10, 0))
        tk.Label(row2, text="目标语言", bg=CARD, fg=TEXT, width=7, anchor="w",
                 font=(FONT, 9, "bold")).pack(side="left")
        self.target_var = tk.StringVar(
            value=core.target_name(self.cfg.get("target_lang", "zh-CN")))
        ttk.Combobox(row2, textvariable=self.target_var, state="readonly",
                     width=18, values=[n for n, _ in engines.TARGET_LANGS]).pack(
            side="left")

        section_title(page, "窗口与行为")
        self.auto_copy_var = tk.BooleanVar(value=bool(self.cfg.get("auto_copy")))
        ttk.Checkbutton(page, text="截屏翻译完成后自动复制译文",
                        variable=self.auto_copy_var,
                        style="Card.TCheckbutton").pack(anchor="w")
        self.topmost_var = tk.BooleanVar(value=bool(self.cfg.get("topmost", True)))
        ttk.Checkbutton(page, text="主窗口置顶", variable=self.topmost_var,
                        style="Card.TCheckbutton").pack(anchor="w", pady=(6, 0))

        op = tk.Frame(page, bg=CARD)
        op.pack(fill="x", pady=(10, 0))
        tk.Label(op, text="主窗口不透明度", bg=CARD, fg=TEXT, anchor="w",
                 font=(FONT, 9, "bold")).pack(fill="x")
        line = tk.Frame(op, bg=CARD)
        line.pack(fill="x", pady=(2, 0))
        self.opacity_var = tk.DoubleVar(value=float(self.cfg.get("opacity", 0.97)))
        op_val = tk.Label(line, text=f"{self.opacity_var.get():.0%}", bg=CARD,
                          fg=TEXT, width=7, anchor="w", font=(FONT, 9, "bold"))
        ttk.Scale(line, from_=0.4, to=1.0, variable=self.opacity_var, length=260,
                  command=lambda _v: op_val.configure(
                      text=f"{self.opacity_var.get():.0%}")).pack(side="left")
        op_val.pack(side="left", padx=8)

        section_title(page, "全局快捷键(留空表示禁用)")
        self.hotkey_vars = {}
        for key, label in (("hotkey", "截屏翻译"),
                           ("hotkey_realtime", "开始 / 停止实时翻译"),
                           ("hotkey_clickthrough", "开关实时翻译鼠标穿透")):
            line = tk.Frame(page, bg=CARD)
            line.pack(fill="x", pady=3)
            tk.Label(line, text=label, bg=CARD, fg=TEXT, width=22, anchor="w",
                     font=(FONT, 9)).pack(side="left")
            var = tk.StringVar(value=self.cfg.get(key, ""))
            self.hotkey_vars[key] = var
            tk.Entry(line, textvariable=var, relief="flat", bg=CARD, fg=TEXT,
                     highlightbackground=BORDER, highlightthickness=1,
                     insertbackground=TEXT, font=(FONT, 10)).pack(
                side="left", fill="x", expand=True, ipady=3)
        hint(page, "修改快捷键后需要重启程序才会生效。")

        if core.has_secret(self.cfg):
            warn = tk.Frame(page, bg=WARN_BG, highlightbackground="#f0d9b5",
                            highlightthickness=1)
            warn.pack(fill="x", pady=(16, 0))
            tk.Label(warn, text="⚠  API Key 以明文保存在 config.json,"
                                "请勿把这个文件分享给别人。",
                     bg=WARN_BG, fg=WARN, font=(FONT, 9), anchor="w",
                     justify="left", wraplength=580).pack(fill="x", padx=12,
                                                          pady=9)

    # ------------------------------------------------------ 保存

    def save(self):
        if self.engine_key_form:
            self.engine_key_form.commit()
        if self.ocr_key_form:
            self.ocr_key_form.commit()
        self.cfg["engine"] = self.engine_var.get()
        self.cfg["ocr_engine"] = self.ocr_var.get()
        self.cfg["ocr_lang"] = dict((n, c) for n, c in _OCR_LANGS).get(
            self.ocr_lang_var.get(), "auto")
        self.cfg["target_lang"] = dict(
            (n, c) for n, c in engines.TARGET_LANGS).get(
            self.target_var.get(), "zh-CN")
        self.cfg["target_name"] = self.target_var.get()
        self.cfg["source_lang"] = dict(
            (n, c) for n, c in engines.SOURCE_LANGS).get(
            self.source_var.get(), "auto")
        self.cfg["auto_copy"] = bool(self.auto_copy_var.get())
        self.cfg["topmost"] = bool(self.topmost_var.get())
        self.cfg["opacity"] = round(float(self.opacity_var.get()), 2)
        for key, var in self.hotkey_vars.items():
            self.cfg[key] = var.get().strip()
        for key, var in self.rt_vars.items():
            self.cfg[key] = (int(var.get()) if key in
                             ("rt_font_size", "rt_overlay_w")
                             else round(float(var.get()), 3))
        self.cfg["rt_show_region"] = bool(self.rt_region_var.get())
        self.cfg["rt_context"] = bool(self.rt_context_var.get())
        self.cfg["rt_stream"] = bool(self.rt_stream_var.get())
        self.cfg["rt_show_source"] = bool(self.rt_source_var.get())
        self.cfg["rt_topmost"] = bool(self.rt_topmost_var.get())
        self.cfg["rt_merge_lines"] = bool(self.rt_merge_var.get())

        # 校验:选了需 Key 的引擎却没填 Key,提醒但不阻止保存
        missing = engines.missing_params(self.cfg["engine"], self.cfg)
        if missing:
            names = "、".join(p.label for p in missing)
            spec = engines.get_engine(self.cfg["engine"])
            detail = f"\n\n获取 Key:{spec.key_url}" if spec.key_url else ""
            if not messagebox.askyesno(
                    "还缺 API Key",
                    f"「{spec.name}」还需要填写:{names}{detail}\n\n"
                    "现在保存吗?(保存后翻译时会再提醒你)",
                    parent=self):
                return
        ocr_missing = core.ocr_missing_params(self.cfg["ocr_engine"], self.cfg)
        if ocr_missing:
            names = "、".join(p.label for p in ocr_missing)
            spec = core.OCR_SPECS[self.cfg["ocr_engine"]]
            detail = f"\n\n获取 Key:{spec['key_url']}" if spec["key_url"] else ""
            if not messagebox.askyesno(
                    "OCR 还缺 Key",
                    f"「{spec['name']}」还需要填写:{names}{detail}\n\n现在保存吗?",
                    parent=self):
                return

        self.orig.clear()
        self.orig.update(self.cfg)
        try:
            core.save_config(self.orig)
        except Exception as e:
            messagebox.showerror("保存失败", str(e), parent=self)
            return
        if callable(self.on_saved):
            self.on_saved()
        self.destroy()


class KeyPromptDialog(tk.Toplevel):
    """翻译时才发现缺 Key —— 直接向用户索要,并给出获取 Key 的网址入口。"""

    def __init__(self, master, cfg, engine_id, extra_msg=""):
        super().__init__(master)
        self.cfg = cfg
        self.engine_id = engine_id
        self.saved = False
        spec = engines.get_engine(engine_id)
        self.spec = spec

        self.title("需要 API Key")
        self.configure(bg=CARD)
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.transient(master)

        pad = tk.Frame(self, bg=CARD, padx=18, pady=16)
        pad.pack(fill="both", expand=True)

        tk.Label(pad, text=f"「{spec.name}」需要 API Key", bg=CARD, fg=TEXT,
                 font=(FONT, 13, "bold"), anchor="w").pack(fill="x")
        if extra_msg:
            tk.Label(pad, text=extra_msg, bg=CARD, fg=ERR_FG, font=(FONT, 9),
                     anchor="w", wraplength=460, justify="left").pack(
                fill="x", pady=(5, 0))
        if spec.key_help:
            tk.Label(pad, text=spec.key_help, bg=CARD, fg=MUTED, font=(FONT, 9),
                     wraplength=460, justify="left", anchor="w").pack(
                fill="x", pady=(7, 0))

        self.form = ParamForm(pad, spec.params, cfg)
        self.form.pack(fill="x", pady=(10, 0))

        if spec.key_url:
            box = tk.Frame(pad, bg="#f2f7ff", highlightbackground="#cfe0f7",
                           highlightthickness=1)
            box.pack(fill="x", pady=(14, 0))
            tk.Label(box, text="还没有 Key?点下面的链接去申请:",
                     bg="#f2f7ff", fg="#3b5a80", font=(FONT, 9),
                     anchor="w").pack(fill="x", padx=12, pady=(9, 3))
            link_label(box, f"👉 {spec.key_url}", spec.key_url, bg="#f2f7ff",
                       font=(FONT, 10, "underline")).pack(anchor="w", padx=12,
                                                          pady=(0, 10))

        row = tk.Frame(pad, bg=CARD)
        row.pack(fill="x", pady=(18, 0))
        self.test_lbl = tk.Label(row, text="", bg=CARD, fg=MUTED,
                                 font=(FONT, 9), anchor="w", wraplength=260,
                                 justify="left")
        self.test_lbl.pack(side="left")
        theme.flat_button(row, "测试并保存", self._test_and_save, kind="primary",
                          padx=14, pady=6).pack(side="right")
        theme.flat_button(row, "仅保存", self._save, padx=14, pady=6).pack(
            side="right", padx=6)
        theme.flat_button(row, "取消", self.destroy, padx=14, pady=6).pack(
            side="right")

    def _collect(self):
        self.form.commit()

    def _save(self):
        self._collect()
        missing = engines.missing_params(self.engine_id, self.cfg)
        if missing:
            messagebox.showwarning(
                "还没填完", "请填写:" + "、".join(p.label for p in missing),
                parent=self)
            return
        try:
            core.save_config(self.cfg)
        except Exception as e:
            messagebox.showerror("保存失败", str(e), parent=self)
            return
        self.saved = True
        self.destroy()

    def _test_and_save(self):
        self._collect()
        self.test_lbl.configure(text="正在测试…", fg=MUTED)
        cfg = dict(self.cfg)
        eid = self.engine_id

        def work():
            ok, msg = engines.test_engine(eid, cfg)
            self.after(0, lambda: self._after_test(ok, msg))
        threading.Thread(target=work, daemon=True).start()

    def _after_test(self, ok, msg):
        if not self.winfo_exists():
            return
        self.test_lbl.configure(text=("✓ " if ok else "✗ ") + msg[:70],
                                fg=OK_FG if ok else ERR_FG)
        if ok:
            self._save()


def ask_for_key(master, cfg, engine_id, extra_msg=""):
    """弹出索要 Key 的对话框,返回是否成功保存。"""
    if not engine_id or engines.get_engine(engine_id) is None:
        return False
    dlg = KeyPromptDialog(master, cfg, engine_id, extra_msg)
    master.wait_window(dlg)
    return dlg.saved


def open_settings(master, cfg, on_saved=None, focus_engine=None):
    win = SettingsWindow(master, cfg, on_saved=on_saved,
                         focus_engine=focus_engine)
    master.wait_window(win)
    return win
