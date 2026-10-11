# -*- coding: utf-8 -*-
"""实时翻译:框选一块屏幕范围,持续识别 + 翻译,译文显示在可调的悬浮窗里。

两个按钮语义不同,别混淆:
  * 「停止翻译」—— 只停掉轮询,悬浮窗和选区都保留,可以随时「继续翻译」;
  * 「退出实时翻译」—— 结束整个实时模式,关掉悬浮窗回到主界面。

悬浮窗可调:显示位置(直接拖动)、不透明度、字体颜色、背景颜色、字号、
是否同时显示原文、鼠标穿透。

线程模型:截图在 Tk 主线程的定时器里做(需要临时隐藏悬浮窗,避免把译文
自己截进去);OCR + 翻译在后台线程做,结果经 queue 回到界面。
"""
import ctypes
import logging
import queue
import threading
import time
import tkinter as tk
from tkinter import colorchooser, font as tkfont, ttk

import core
import engines
import selector
import theme

log = logging.getLogger("fanyi")
FONT = "Microsoft YaHei UI"

# Windows 扩展样式:用于实现「鼠标穿透」
_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000

# SetWindowPos 用的标志
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_SHOWWINDOW = 0x0040


def _hwnd_of(win):
    """取 Tk 窗口对应的真实 HWND。"""
    try:
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        return hwnd or win.winfo_id()
    except Exception:
        return None


def _apply_click_through(win, enabled):
    """让窗口忽略鼠标(穿透到下面的窗口)。"""
    hwnd = _hwnd_of(win)
    if not hwnd:
        return False
    try:
        u = ctypes.windll.user32
        ex = u.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        ex |= _WS_EX_LAYERED | _WS_EX_TOOLWINDOW
        if enabled:
            ex |= _WS_EX_TRANSPARENT | _WS_EX_NOACTIVATE
        else:
            ex &= ~(_WS_EX_TRANSPARENT | _WS_EX_NOACTIVATE)
        u.SetWindowLongW(hwnd, _GWL_EXSTYLE, ex)
        return True
    except Exception as e:
        log.warning("设置鼠标穿透失败: %s", e)
        return False


class RegionIndicator(tk.Toplevel):
    """把监视范围画成一个高亮边框,让你随时看得见正在翻屏幕上的哪一块。

    关键:边框画在选区**外面**的留白里(窗口比选区四周各大 PAD 像素),
    所以截图时不会把边框自己拍进去,不影响 OCR 识别。
    顶部还有一条标签,显示状态和范围尺寸 —— 它同样在选区之外。
    """

    MAGIC = "#ff00ff"     # 透明色键:该颜色的像素完全透明且鼠标穿透
    PAD = 4               # 选区四周的留白(边框就画在这里)

    def __init__(self, master, region, color="#2f6fed", text=""):
        super().__init__(master)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self._keyed = False
        try:
            self.attributes("-transparentcolor", self.MAGIC)
            self._keyed = True
        except Exception:
            # 不是 Windows 或不支持色键:退化成半透明色块
            self.attributes("-alpha", 0.35)
        self._color = color

        # 标签条高度按实际字体行高算,高分屏下才不会把字切掉
        try:
            f = tkfont.Font(font=(FONT, 8))
            self.LABEL_H = max(16, f.metrics("linespace") + 6)
        except Exception:
            self.LABEL_H = 19

        bg = self.MAGIC if self._keyed else color
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0,
                                takefocus=0)
        self.canvas.pack(fill="both", expand=True)
        self._text = text
        self._region = core.normalize_rect(region)
        self._label_below = False
        self.set_region(region)
        _apply_click_through(self, True)
        self.lift()

    def set_region(self, region, text=None, color=None):
        if text is not None:
            self._text = text
        if color:
            self._color = color
        x1, y1, x2, y2 = core.normalize_rect(region)
        self._region = (x1, y1, x2, y2)
        sx, sy, sw, sh = core.virtual_screen_rect()
        pad, lh = self.PAD, self.LABEL_H
        w = (x2 - x1) + pad * 2
        h = (y2 - y1) + pad * 2 + lh
        gx = x1 - pad
        # 选区太靠屏幕顶部时,把标签条挪到下面去,免得跑到屏幕外
        if y1 - pad - lh < sy:
            gy = y1 - pad
            self._label_below = True
        else:
            gy = y1 - pad - lh
            self._label_below = False

        self.geometry(f"{w}x{h}+{gx}+{gy}")
        self._force_place(gx, gy, w, h)
        self._redraw(w, h, pad, lh)
        try:
            self.lift()          # 保证盖在被监视窗口之上
        except Exception:
            pass

    def _force_place(self, x, y, w, h):
        """用 Win32 SetWindowPos 把窗口强制搬到位。

        只调 Tk 的 geometry() 有时只生效一半:实测「移动译文悬浮窗之后再重选
        监视范围」时,尺寸变了但位置还停在老地方(Windows 上 overrideredirect
        窗口的已知毛病)。所以这里再强制落一次位,保证移动一定生效。
        """
        hwnd = _hwnd_of(self)
        if not hwnd:
            return
        try:
            ctypes.windll.user32.SetWindowPos(
                hwnd, 0, int(x), int(y), int(w), int(h),
                _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_SHOWWINDOW)
        except Exception as e:
            log.warning("SetWindowPos 失败: %s", e)

    def _redraw(self, w, h, pad, lh):
        c = self.canvas
        c.delete("all")
        bg = self.MAGIC if self._keyed else self._color

        if self._label_below:
            bar_top, bar_bottom = h - lh, h
            border_top, border_bottom = 1, h - lh - 1
        else:
            bar_top, bar_bottom = 0, lh
            border_top, border_bottom = lh + 1, h - 2

        # 顶部(或底部)标签条 —— 位于选区之外,不会被截进去
        c.create_rectangle(0, bar_top, w, bar_bottom,
                           fill="#1c2431", outline="")
        c.create_text(7, (bar_top + bar_bottom) // 2, anchor="w",
                      text=self._text, fill="#8fb6ff", font=(FONT, 8))

        # 选区边框:只画四周留白里,像素不进入选区
        c.create_rectangle(1, border_top, w - 2, border_bottom,
                           outline=self._color, width=2)

    def update_text(self, text):
        self._text = text
        try:
            self.set_region(self._region, text=text)
        except Exception:
            pass


class TranslationCache:
    """小小的译文缓存:同一句话第二次出现时直接命中,不再请求接口。"""

    def __init__(self, limit=300):
        self.limit = limit
        self._data = {}
        self._order = []

    def get(self, key):
        return self._data.get(key)

    def put(self, key, value):
        if key in self._data:
            return
        self._data[key] = value
        self._order.append(key)
        while len(self._order) > self.limit:
            old = self._order.pop(0)
            self._data.pop(old, None)

    def clear(self):
        self._data.clear()
        self._order.clear()


class RealtimeOverlay(tk.Toplevel):
    """译文悬浮窗。"""

    def __init__(self, master, session):
        super().__init__(master)
        self.session = session
        cfg = session.cfg

        self.overrideredirect(True)
        self.attributes("-topmost", bool(cfg.get("rt_topmost", True)))
        self.attributes("-alpha", float(cfg.get("rt_opacity", 0.88)))
        self.configure(bg=str(cfg.get("rt_bg", "#0f1318")))

        self._drag = None
        self._resize = None
        self._lines = None
        self._click_through = False

        # ---------------- 顶部控制条(兼作拖动把手) ----------------
        self.header = tk.Frame(self, bg=theme.OVERLAY_HEAD, cursor="fleur")
        self.header.pack(fill="x")

        # 左侧一小条强调色,让窗口看起来更有层次
        self.accent = tk.Frame(self.header, bg="#2f6fed", width=3,
                               cursor="fleur")
        self.accent.pack(side="left", fill="y")

        self.title = tk.Label(self.header, text="🎯 实时翻译",
                              bg=theme.OVERLAY_HEAD, fg="#8fb6ff",
                              font=(FONT, 9, "bold"), cursor="fleur")
        self.title.pack(side="left", padx=(8, 8), pady=4)

        self.state_dot = tk.Label(self.header, text="● 运行中",
                                  bg=theme.OVERLAY_HEAD, fg="#3ddc84",
                                  font=(FONT, 9), cursor="fleur")
        self.state_dot.pack(side="left")

        def _btn(text, cmd, fg="#dbe6f3", bg="#26313f", hover="#33445a"):
            b = tk.Button(self.header, text=text, command=cmd, relief="flat",
                          bg=bg, fg=fg, activebackground=hover,
                          activeforeground="#ffffff", font=(FONT, 9),
                          cursor="hand2", padx=8, pady=2, borderwidth=0)
            b.pack(side="right", padx=3, pady=3)
            b.bind("<Enter>", lambda _e: b.configure(bg=hover))
            b.bind("<Leave>", lambda _e: b.configure(bg=bg))
            return b

        _btn("✕ 退出实时翻译", self.session.exit_realtime, fg="#ffb3ad",
             bg="#43272a", hover="#5c3437")
        _btn("🎨 样式", self.session.open_style_panel)
        self.region_btn = _btn("👁 范围", self.session.toggle_show_region)
        _btn("⛶ 重选范围", self.session.reselect_region)
        self.stop_btn = _btn("⏸ 停止翻译", self.session.toggle_translation,
                             bg="#1f3a33", hover="#285045")

        # 拖动:整个控制条都能拖
        for w in (self.header, self.title, self.state_dot):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", self._drag_end)

        # ---------------- 译文区 ----------------
        body = tk.Frame(self, bg=str(cfg.get("rt_bg", "#0f1318")))
        body.pack(fill="both", expand=True, padx=8, pady=(6, 4))

        self.text = tk.Text(
            body, wrap="word", height=3, relief="flat", borderwidth=0,
            highlightthickness=0, bg=str(cfg.get("rt_bg", "#0f1318")),
            fg=str(cfg.get("rt_fg", "#ffffff")),
            font=(str(cfg.get("rt_font", FONT)), int(cfg.get("rt_font_size", 20))),
            padx=6, pady=4, cursor="arrow")
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("src", foreground="#8fa6bd")
        self.text.configure(state="disabled")

        # ---------------- 底部状态条 ----------------
        tk.Frame(self, bg="#26313f", height=1).pack(fill="x")
        self.status = tk.Label(self, text="正在准备…", bg="#161d26",
                               fg="#7f8fa3", font=(FONT, 8), anchor="w")
        self.status.pack(fill="x")
        self.status.configure(padx=8, pady=3)

        # 右下角拉手:调整宽度
        self.grip = tk.Label(self, text="◢", bg="#161d26", fg="#5b6b7f",
                             cursor="sizing", font=(FONT, 9))
        self.grip.place(relx=1.0, rely=1.0, anchor="se")
        self.grip.bind("<ButtonPress-1>", self._resize_start)
        self.grip.bind("<B1-Motion>", self._resize_move)
        self.grip.bind("<ButtonRelease-1>", self._resize_end)

        self.apply_style()
        self.place_near_region()
        self.refresh_region_btn()

    def refresh_region_btn(self):
        """「👁 范围」按钮反映当前是显示还是隐藏。"""
        on = bool(self.session.cfg.get("rt_show_region", True))
        try:
            self.region_btn.configure(
                text="👁 范围 开" if on else "👁 范围 关",
                fg="#dbe6f3" if on else "#8b98a8")
        except Exception:
            pass

    # ---------------------------------------------------------- 外观

    def apply_style(self):
        """把配置里的颜色/字号/透明度应用到窗口上。"""
        cfg = self.session.cfg
        bg = str(cfg.get("rt_bg", "#0f1318"))
        self.configure(bg=bg)
        self.text.configure(bg=bg, fg=str(cfg.get("rt_fg", "#ffffff")),
                            font=(str(cfg.get("rt_font", FONT)),
                                  int(cfg.get("rt_font_size", 20))))
        try:
            self.attributes("-alpha", float(cfg.get("rt_opacity", 0.88)))
            self.attributes("-topmost", bool(cfg.get("rt_topmost", True)))
        except Exception:
            pass
        self.text.master.configure(bg=bg)
        self._autosize()

    def set_click_through(self, enabled):
        ok = _apply_click_through(self, enabled)
        self._click_through = bool(enabled) and ok
        return self._click_through

    @property
    def click_through(self):
        return self._click_through

    def place_near_region(self):
        """默认放在选区正下方;用户拖过之后就用记住的位置。"""
        cfg = self.session.cfg
        region = self.session.region
        self.update_idletasks()
        # 宽度至少放得下控制条,不然按钮会被截断
        need_w = self.header.winfo_reqwidth() + 18
        w = max(int(cfg.get("rt_overlay_w", 660) or 660), need_w)
        cfg["rt_overlay_w"] = w
        h = max(self.winfo_reqheight(), 90)
        sx, sy, sw, sh = core.virtual_screen_rect()

        x = cfg.get("rt_overlay_x")
        y = cfg.get("rt_overlay_y")
        if x is None or y is None:
            if region:
                rx1, ry1, rx2, ry2 = core.normalize_rect(region)
                x = max(sx, min(rx1, sx + sw - w))
                y = ry2 + 8
                if y + h > sy + sh:          # 下面放不下就放上面
                    y = max(sy, ry1 - h - 8)
            else:
                x, y = sx + 40, sy + 40
        x = max(sx, min(int(x), sx + sw - 160))
        y = max(sy, min(int(y), sy + sh - 60))
        self.geometry(f"{w}x{h}+{int(x)}+{int(y)}")
        cfg["rt_overlay_x"], cfg["rt_overlay_y"] = int(x), int(y)

    def show_translation(self, translation, source=""):
        cfg = self.session.cfg
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        if cfg.get("rt_show_source") and source:
            self.text.insert("end", source + "\n", "src")
        self.text.insert("end", translation or "")
        self.text.configure(state="disabled")
        self._autosize()

    def set_status(self, msg, color="#7f8fa3"):
        try:
            self.status.configure(text=msg, fg=color)
        except Exception:
            pass

    def set_running(self, running):
        if running:
            self.state_dot.configure(text="● 运行中", fg="#3ddc84")
            self.stop_btn.configure(text="⏸ 停止翻译")
        else:
            self.state_dot.configure(text="● 已停止", fg="#ffb54d")
            self.stop_btn.configure(text="▶ 继续翻译")

    def _autosize(self):
        """按内容行数自动调整高度(2~14 行之间)。"""
        try:
            res = self.text.count("1.0", "end-1c", "displaylines")
            lines = int(res[0]) if res else 1
        except Exception:
            lines = 1
        lines = max(2, min(lines + 1, 14))
        if lines != self._lines:
            self._lines = lines
            self.text.configure(height=lines)
            self.update_idletasks()
            h = self.winfo_reqheight()
            x, y = self.winfo_x(), self.winfo_y()
            w = self.winfo_width() or int(self.session.cfg.get("rt_overlay_w", 560))
            self.geometry(f"{w}x{h}+{x}+{y}")

    # ---------------------------------------------------------- 拖动 / 缩放

    def _drag_start(self, e):
        self._drag = (e.x_root - self.winfo_x(), e.y_root - self.winfo_y())

    def _drag_move(self, e):
        if not self._drag:
            return
        dx, dy = self._drag
        self.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    def _drag_end(self, _e=None):
        self._drag = None
        try:
            self.session.cfg["rt_overlay_x"] = self.winfo_x()
            self.session.cfg["rt_overlay_y"] = self.winfo_y()
        except Exception:
            pass

    def _resize_start(self, e):
        self._resize = (e.x_root, self.winfo_width())

    def _resize_move(self, e):
        if not self._resize:
            return
        x0, w0 = self._resize
        w = max(240, w0 + (e.x_root - x0))
        h = self.winfo_height()
        self.geometry(f"{w}x{h}+{self.winfo_x()}+{self.winfo_y()}")
        self.session.cfg["rt_overlay_w"] = w

    def _resize_end(self, _e=None):
        self._resize = None

    def destroy(self):
        try:
            self.session.cfg["rt_overlay_x"] = self.winfo_x()
            self.session.cfg["rt_overlay_y"] = self.winfo_y()
        except Exception:
            pass
        super().destroy()


# ---------------------------------------------------------------- 样式面板

class StylePanel(tk.Toplevel):
    """悬浮窗样式设置:位置 / 透明度 / 颜色 / 字号 / 监视范围框。"""

    def __init__(self, master, session):
        super().__init__(master)
        self.session = session
        self.cfg = session.cfg
        self.title("实时翻译 · 显示样式")
        self.configure(bg=theme.CARD)
        self.attributes("-topmost", True)
        self.resizable(False, False)

        body = tk.Frame(self, bg=theme.CARD, padx=16, pady=14)
        body.pack(fill="both", expand=True)
        self._body = body
        r = 0

        def section(text, row, pady=(14, 3)):
            tk.Label(body, text=text, bg=theme.CARD, fg=theme.TEXT, anchor="w",
                     font=(FONT, 9, "bold")).grid(
                row=row, column=0, columnspan=3, sticky="w", pady=pady)
            return row + 1

        def slider(label, key, lo, hi, fmt, row, tip=""):
            tk.Label(body, text=label, bg=theme.CARD, fg=theme.MUTED,
                     anchor="w", font=(FONT, 8)).grid(
                row=row, column=0, columnspan=3, sticky="w")
            row += 1
            try:
                initial = float(self.cfg.get(key, lo))
            except (TypeError, ValueError):
                initial = float(lo)
            var = tk.DoubleVar(value=initial)
            lbl = tk.Label(body, text=fmt(initial), bg=theme.CARD,
                           fg=theme.TEXT, width=6, anchor="e",
                           font=(FONT, 9, "bold"))
            ttk.Scale(body, from_=lo, to=hi, variable=var, length=252,
                      command=lambda _v: (lbl.configure(text=fmt(var.get())),
                                          self._apply())).grid(
                row=row, column=0, columnspan=2, sticky="w")
            lbl.grid(row=row, column=2, sticky="e")
            row += 1
            if tip:
                tk.Label(body, text=tip, bg=theme.CARD, fg=theme.MUTED,
                         anchor="w", font=(FONT, 8), wraplength=310,
                         justify="left").grid(row=row, column=0, columnspan=3,
                                              sticky="w")
                row += 1
            return var, row

        # ---- 窗口外观 ----
        r = section("窗口外观", r, pady=(0, 3))
        self.opacity_var, r = slider("不透明度", "rt_opacity", 0.2, 1.0,
                                     lambda v: f"{v:.0%}", r)
        self.font_var, r = slider("译文字号", "rt_font_size", 10, 48,
                                  lambda v: str(int(v)), r)

        # ---- 颜色 ----
        r = section("颜色", r)
        color_row = tk.Frame(body, bg=theme.CARD)
        color_row.grid(row=r, column=0, columnspan=3, sticky="w")
        self.fg_btn = tk.Button(color_row, text="字体颜色", relief="flat",
                                cursor="hand2", borderwidth=0, padx=10, pady=5,
                                font=(FONT, 9), command=self._pick_fg)
        self.fg_btn.pack(side="left")
        self.bg_btn = tk.Button(color_row, text="背景颜色", relief="flat",
                                cursor="hand2", borderwidth=0, padx=10, pady=5,
                                font=(FONT, 9), command=self._pick_bg)
        self.bg_btn.pack(side="left", padx=6)
        self.region_color_btn = tk.Button(
            color_row, text="范围框颜色", relief="flat", cursor="hand2",
            borderwidth=0, padx=10, pady=5, font=(FONT, 9),
            command=self._pick_region_color)
        self.region_color_btn.pack(side="left")
        r += 1
        theme.flat_button(body, "恢复默认配色", self._reset_colors,
                          padx=10, pady=4).grid(row=r, column=0, sticky="w",
                                                pady=(7, 0))
        r += 1

        # ---- 监视范围框(需求 2)----
        r = section("监视范围", r)
        self.region_var = tk.BooleanVar(
            value=bool(self.cfg.get("rt_show_region", True)))
        ttk.Checkbutton(body, text="在屏幕上显示监视范围框(取消勾选即隐藏)",
                        variable=self.region_var, style="Card.TCheckbutton",
                        command=self._apply).grid(row=r, column=0,
                                                  columnspan=3, sticky="w")
        r += 1
        tk.Label(body, text="范围框画在选区外面,不会挡住识别;"
                            "窗口本身也不吃鼠标点击。",
                 bg=theme.CARD, fg=theme.MUTED, font=(FONT, 8), anchor="w",
                 wraplength=310, justify="left").grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(2, 0))
        r += 1

        # ---- 显示选项 ----
        r = section("显示选项", r)
        self.source_var = tk.BooleanVar(value=bool(self.cfg.get("rt_show_source")))
        ttk.Checkbutton(body, text="同时显示原文", variable=self.source_var,
                        style="Card.TCheckbutton", command=self._apply).grid(
            row=r, column=0, columnspan=3, sticky="w")
        r += 1
        self.topmost_var = tk.BooleanVar(
            value=bool(self.cfg.get("rt_topmost", True)))
        ttk.Checkbutton(body, text="窗口始终置顶", variable=self.topmost_var,
                        style="Card.TCheckbutton", command=self._apply).grid(
            row=r, column=0, columnspan=3, sticky="w")
        r += 1
        self.ct_var = tk.BooleanVar(value=bool(self.cfg.get("rt_click_through")))
        ttk.Checkbutton(body, text="鼠标穿透(点不到这个窗口,不挡操作)",
                        variable=self.ct_var, style="Card.TCheckbutton",
                        command=self._apply).grid(row=r, column=0,
                                                  columnspan=3, sticky="w")
        r += 1
        self.merge_var = tk.BooleanVar(
            value=bool(self.cfg.get("rt_merge_lines", True)))
        ttk.Checkbutton(body, text="折行合并(把被折成多行的句子拼回一句再翻)",
                        variable=self.merge_var, style="Card.TCheckbutton",
                        command=self._apply).grid(row=r, column=0,
                                                  columnspan=3, sticky="w")
        r += 1

        # ---- 上下文与速度 ----
        r = section("上下文与速度", r)
        self.ctx_var = tk.BooleanVar(value=bool(self.cfg.get("rt_context", True)))
        ttk.Checkbutton(body, text="联系上下文翻译(看对话/长文时更连贯)",
                        variable=self.ctx_var, style="Card.TCheckbutton",
                        command=self._apply).grid(row=r, column=0,
                                                  columnspan=3, sticky="w")
        r += 1
        self.stream_var = tk.BooleanVar(value=bool(self.cfg.get("rt_stream", True)))
        ttk.Checkbutton(body, text="逐字显示(译文边生成边出现,感觉更快)",
                        variable=self.stream_var, style="Card.TCheckbutton",
                        command=self._apply).grid(row=r, column=0,
                                                  columnspan=3, sticky="w")
        r += 1
        self.ctx_tip = tk.Label(body, text="", bg=theme.CARD, fg=theme.MUTED,
                                font=(FONT, 8), anchor="w", wraplength=310,
                                justify="left")
        self.ctx_tip.grid(row=r, column=0, columnspan=3, sticky="w",
                          pady=(2, 0))
        r += 1
        self._refresh_ctx_tip()

        # ---- 性能 ----
        r = section("性能", r)
        self.interval_var, r = slider(
            "刷新间隔(秒)", "rt_interval", 0.3, 3.0,
            lambda v: f"{v:.1f}s", r, tip="越小越实时,越吃 CPU。")

        tk.Label(body, text="开启鼠标穿透后本窗口收不到鼠标事件,"
                            "可用全局快捷键或主界面的按钮关闭。",
                 bg=theme.CARD, fg=theme.MUTED, wraplength=310,
                 justify="left", font=(FONT, 8)).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(12, 0))
        r += 1

        btns = tk.Frame(body, bg=theme.CARD)
        btns.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        theme.flat_button(btns, "放回选区下方", self._reset_position,
                          padx=10, pady=5).pack(side="left")
        theme.flat_button(btns, "关闭", self.destroy, kind="primary",
                          padx=20, pady=5).pack(side="right")
        btns.columnconfigure(0, weight=1)

        self._refresh_color_buttons()

    # ------------------------------------------------------ 内部

    def _refresh_ctx_tip(self):
        """提示当前引擎支不支持上下文和流式 —— 只对大模型引擎有效。"""
        spec = engines.get_engine(self.cfg.get("engine", ""))
        name = spec.name if spec else "当前引擎"
        if spec and spec.supports_context:
            txt = f"{name} 支持上下文,开关直接生效。"
            color = theme.MUTED
        else:
            txt = (f"{name} 读不懂前文,这两个开关对它无效 —— "
                   f"想用请把引擎换成 DeepSeek 或 OpenAI 兼容接口。")
            color = theme.WARN
        try:
            self.ctx_tip.configure(text=txt, fg=color)
        except Exception:
            pass

    def _refresh_color_buttons(self):
        fg = str(self.cfg.get("rt_fg", "#ffffff"))
        bg = str(self.cfg.get("rt_bg", "#0f1318"))
        rc = str(self.cfg.get("rt_region_color", "#2f6fed"))
        self.fg_btn.configure(bg=fg,
                              fg="#000000" if _is_light(fg) else "#ffffff")
        self.bg_btn.configure(bg=bg,
                              fg="#000000" if _is_light(bg) else "#ffffff")
        self.region_color_btn.configure(bg=rc, fg="#ffffff")

    def _apply(self):
        self.cfg["rt_opacity"] = round(float(self.opacity_var.get()), 2)
        self.cfg["rt_font_size"] = int(self.font_var.get())
        self.cfg["rt_interval"] = round(float(self.interval_var.get()), 2)
        self.cfg["rt_show_source"] = bool(self.source_var.get())
        self.cfg["rt_topmost"] = bool(self.topmost_var.get())
        self.cfg["rt_merge_lines"] = bool(self.merge_var.get())
        self.cfg["rt_context"] = bool(self.ctx_var.get())
        self.cfg["rt_stream"] = bool(self.stream_var.get())
        if bool(self.ct_var.get()) != self.session.overlay.click_through:
            self.session.set_click_through(bool(self.ct_var.get()))
        self.cfg["rt_click_through"] = bool(self.ct_var.get())
        # 监视范围框:勾选即显示,取消勾选即隐藏
        self.session.set_show_region(bool(self.region_var.get()))
        self.session.overlay.apply_style()
        want_src = bool(self.cfg.get("rt_show_source"))
        if want_src != self.session._showing_source:
            self.session.repaint()
        self._refresh_color_buttons()

    def _pick_fg(self):
        c = colorchooser.askcolor(color=str(self.cfg.get("rt_fg", "#ffffff")),
                                  title="选择字体颜色", parent=self)
        if c and c[1]:
            self.cfg["rt_fg"] = c[1]
            self._apply()

    def _pick_bg(self):
        c = colorchooser.askcolor(color=str(self.cfg.get("rt_bg", "#0f1318")),
                                  title="选择背景颜色", parent=self)
        if c and c[1]:
            self.cfg["rt_bg"] = c[1]
            self._apply()

    def _pick_region_color(self):
        c = colorchooser.askcolor(
            color=str(self.cfg.get("rt_region_color", "#2f6fed")),
            title="选择监视范围框颜色", parent=self)
        if c and c[1]:
            self.cfg["rt_region_color"] = c[1]
            self.session._sync_indicator()
            self._refresh_color_buttons()

    def _reset_colors(self):
        self.cfg["rt_fg"] = core.DEFAULT_CONFIG["rt_fg"]
        self.cfg["rt_bg"] = core.DEFAULT_CONFIG["rt_bg"]
        self.cfg["rt_region_color"] = core.DEFAULT_CONFIG["rt_region_color"]
        self._apply()

    def _reset_position(self):
        self.cfg["rt_overlay_x"] = None
        self.cfg["rt_overlay_y"] = None
        self.session.overlay.place_near_region()


def _is_light(hex_color):
    """粗略判断颜色深浅,决定按钮文字用黑还是白。"""
    try:
        h = hex_color.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return (0.299 * r + 0.587 * g + 0.114 * b) > 150
    except Exception:
        return False


# ---------------------------------------------------------------- 会话控制器

class RealtimeSession:
    """一次实时翻译会话:选区 + 轮询 + 悬浮窗。"""

    def __init__(self, master, cfg, on_exit=None, on_status=None,
                 on_need_key=None):
        self.master = master
        self.cfg = cfg
        self.on_exit = on_exit
        self.on_status = on_status        # 回调:把状态显示到主窗口状态栏
        self.on_need_key = on_need_key    # 回调:缺 Key 时弹出索要 Key 的界面

        self.region = None
        self.overlay = None
        self.indicator = None
        self.style_panel = None
        self.running = False              # 轮询开关(「停止翻译」只关这个)
        self.active = False               # 整个实时模式是否还在
        self.cache = TranslationCache()

        self._busy = False                # 后台 OCR+翻译 是否在进行
        self._results = queue.Queue()
        self._timer = None
        self._last_sig = None
        self._last_source = ""            # 上一帧识别到的原文
        self._last_translation = ""       # 上一帧的译文
        self._showing_source = False
        self._fails = 0
        self._last_ok = 0.0

        # ---- 上下文翻译 ----
        self._context = []                # 最近几句原文,给大模型当前文
        self._ctx_pending = None          # 等着做上下文重译的那句
        self._text_stamp = 0.0            # 原文最后一次变化的时间
        self._ctx_busy = False
        self._streaming = False           # 正在流式接收

    # ------------------------------------------------------ 生命周期

    def start(self, region):
        self.region = core.normalize_rect(region)
        self.cfg["rt_region"] = list(self.region)
        self.active = True
        self.running = True
        self._last_sig = None
        self._last_source = ""
        self._last_translation = ""
        self._fails = 0
        self._reset_context()

        if self.overlay is None or not self.overlay.winfo_exists():
            self.overlay = RealtimeOverlay(self.master, self)
            self.overlay.protocol("WM_DELETE_WINDOW", self.exit_realtime)
        else:
            self.overlay.place_near_region()
        self.overlay.set_running(True)
        self.overlay.set_status("正在识别…")
        if bool(self.cfg.get("rt_click_through")):
            self.set_click_through(True)
        self._sync_indicator()
        self._schedule(0.15)
        self._notify("实时翻译已开始")
        return True

    # ------------------------------------------------------ 监视范围框

    def _indicator_text(self):
        if not self.region:
            return ""
        x1, y1, x2, y2 = self.region
        state = "翻译中" if self.running else "已暂停"
        return (f"🎯 实时翻译 · {state} · 范围 {x2 - x1}×{y2 - y1} "
                f"@({x1},{y1}) · 「🎨 样式」里可关掉这个框")

    def _sync_indicator(self):
        """按配置显示/隐藏监视范围框。"""
        want = bool(self.cfg.get("rt_show_region", True)) and bool(self.region)
        if not want:
            self._destroy_indicator()
            return
        color = str(self.cfg.get("rt_region_color", "#2f6fed"))
        text = self._indicator_text()
        if self.indicator is not None:
            try:
                if self.indicator.winfo_exists():
                    self.indicator.set_region(self.region, text=text,
                                              color=color)
                    return
            except Exception:
                pass
            self.indicator = None
        try:
            self.indicator = RegionIndicator(self.master, self.region,
                                             color=color, text=text)
        except Exception as e:
            log.warning("创建监视范围框失败: %s", e)
            self.indicator = None

    def _destroy_indicator(self):
        if self.indicator is not None:
            try:
                self.indicator.destroy()
            except Exception:
                pass
            self.indicator = None

    def set_show_region(self, enabled):
        """勾选/取消「显示监视范围框」。"""
        self.cfg["rt_show_region"] = bool(enabled)
        self._sync_indicator()
        if self.overlay and self.overlay.winfo_exists():
            self.overlay.refresh_region_btn()
        if self.style_panel and self.style_panel.winfo_exists():
            try:
                self.style_panel.region_var.set(bool(enabled))
            except Exception:
                pass
        return bool(enabled)

    def toggle_show_region(self):
        """「👁 范围」按钮:一键显示/隐藏监视范围框。"""
        on = self.set_show_region(not bool(self.cfg.get("rt_show_region", True)))
        self._notify("监视范围框:已显示" if on else "监视范围框:已隐藏")
        return on

    def toggle_translation(self):
        """「停止翻译」/「继续翻译」:只控制轮询,不动窗口。"""
        if not self.active:
            return
        self.running = not self.running
        if self.overlay and self.overlay.winfo_exists():
            self.overlay.set_running(self.running)
            if self.running:
                self._last_sig = None
                self.overlay.set_status("已继续,正在识别…", "#3ddc84")
                self._schedule(0.1)
            else:
                self.overlay.set_status(
                    "已停止翻译(窗口保留,点「继续翻译」恢复)", "#ffb54d")
        self._sync_indicator()
        self._notify("实时翻译已继续" if self.running else "已停止翻译(窗口保留)")

    def stop_translation(self):
        if self.running:
            self.toggle_translation()

    def resume_translation(self):
        """「继续翻译」(补好 Key 之后也会自动走这里)。"""
        if not self.active:
            return False
        if not self.running:
            self.toggle_translation()
        return self.running

    def set_click_through(self, enabled):
        if self.overlay and self.overlay.winfo_exists():
            ok = self.overlay.set_click_through(enabled)
            self.cfg["rt_click_through"] = bool(enabled)
            if enabled and not ok:
                self._notify("鼠标穿透设置失败")
            return ok
        return False

    def toggle_click_through(self):
        target = not bool(self.cfg.get("rt_click_through"))
        ok = self.set_click_through(target)
        if ok:
            self._notify("鼠标穿透:已开启(用快捷键或主界面关闭)"
                         if target else "鼠标穿透:已关闭")
        if self.style_panel and self.style_panel.winfo_exists():
            self.style_panel.ct_var.set(target)
        return ok

    def reselect_region(self):
        """重新框选监视范围(不必退出实时模式)。"""
        was_running = self.running
        self.running = False
        if self.overlay and self.overlay.winfo_exists():
            self.overlay.withdraw()
        try:
            rect = selector.select_region(
                self.master, initial=self.region,
                title="重新框选要实时翻译的区域",
                hint="松开鼠标即切换到新范围\n右键或 Esc 取消(保留原范围)")
        finally:
            if self.overlay and self.overlay.winfo_exists():
                self.overlay.deiconify()
        if rect:
            self.region = core.normalize_rect(rect)
            self.cfg["rt_region"] = list(self.region)
            self._last_sig = None
            self._last_source = ""
            self._last_translation = ""
            self._reset_context()          # 换了范围,前文不作数了
            if self.overlay and self.overlay.winfo_exists():
                self.overlay.place_near_region()
                self.overlay.set_status("已切换范围,正在识别…", "#3ddc84")
            self._sync_indicator()
            self.running = True
        else:
            self.running = was_running
        if self.overlay and self.overlay.winfo_exists():
            self.overlay.set_running(self.running)
        if self.running:
            self._schedule(0.1)
        self._notify(f"监视范围已更新: {self._fmt_region()}"
                     if rect else "已取消重选")

    def exit_realtime(self):
        """「退出实时翻译」:结束整个实时模式,回到主界面。"""
        self.active = False
        self.running = False
        self._cancel_timer()
        self._destroy_indicator()
        if self.style_panel and self.style_panel.winfo_exists():
            self.style_panel.destroy()
        self.style_panel = None
        if self.overlay and self.overlay.winfo_exists():
            self.overlay.destroy()
        self.overlay = None
        self._busy = False
        self._notify("已退出实时翻译")
        if callable(self.on_exit):
            self.on_exit()

    def open_style_panel(self):
        if self.style_panel and self.style_panel.winfo_exists():
            self.style_panel.lift()
            self.style_panel.focus_force()
            return
        self.style_panel = StylePanel(self.master, self)

    # ------------------------------------------------------ 主循环(定时器)

    def _schedule(self, delay=None):
        self._cancel_timer()
        if not self.active or not self.running:
            return
        delay = float(delay if delay is not None
                      else self.cfg.get("rt_interval", 0.7))
        self._timer = self.master.after(max(30, int(delay * 1000)), self._tick)

    def _cancel_timer(self):
        if self._timer is not None:
            try:
                self.master.after_cancel(self._timer)
            except Exception:
                pass
            self._timer = None

    def _tick(self):
        self._timer = None
        if not self.active or not self.running:
            return
        if self._busy:
            self._schedule()
            return
        try:
            self._drain_results()
            self._maybe_refine()
            if self.overlay and self.overlay.winfo_exists() and \
                    core.rect_intersects(self.region, self._overlay_rect()):
                # 悬浮窗压在采集区上,先把它变透明再截图,免得把译文自己认进去
                self.overlay.attributes("-alpha", 0.0)
                self.overlay.update_idletasks()
                self.overlay.update()
                self.master.after(25, self._capture_now)
                return
            self._capture_now()
        except Exception as e:
            log.exception("实时翻译循环出错")
            if self.overlay and self.overlay.winfo_exists():
                self.overlay.set_status(f"出错: {e}", "#ff8a80")
            self._restore_overlay_alpha()
            self._schedule()

    def _overlay_rect(self):
        o = self.overlay
        if not o or not o.winfo_exists():
            return None
        return (o.winfo_x(), o.winfo_y(),
                o.winfo_x() + o.winfo_width(), o.winfo_y() + o.winfo_height())

    def _restore_overlay_alpha(self):
        if self.overlay and self.overlay.winfo_exists():
            try:
                self.overlay.attributes(
                    "-alpha", float(self.cfg.get("rt_opacity", 0.88)))
            except Exception:
                pass

    def _capture_now(self):
        if not self.active or not self.running:
            self._restore_overlay_alpha()
            return
        try:
            img = core.capture_region(self.region)
        except Exception as e:
            self._restore_overlay_alpha()
            log.warning("实时翻译截图失败: %s", e)
            self._schedule()
            return
        self._restore_overlay_alpha()

        # 画面没变就不重复 OCR(省 CPU)
        sig = core.frame_signature(img)
        if not core.frame_changed(sig, self._last_sig):
            self._schedule()
            return
        self._last_sig = sig
        self._busy = True
        threading.Thread(target=self._worker, args=(img,), daemon=True).start()
        self._schedule()

    # ------------------------------------------------------ 后台线程

    # ---- 上下文:翻译过的前几句,用来帮大模型判断指代、称呼、专有名词 ----

    def _context_text(self):
        """把最近几句拼成前文。只取原文,不带译文 —— 带上译文反而容易串味。"""
        n = int(self.cfg.get("rt_context_lines", 4) or 0)
        if n <= 0:
            return ""
        items = self._context[-n:]
        if not items:
            return ""
        return "\n".join(items)

    def _push_context(self, source):
        """把一句原文记进上下文;太长的截断,重复的不记。"""
        s = (source or "").strip()
        if not s:
            return
        if self._context and self._context[-1] == s:
            return
        self._context.append(s)
        limit = max(int(self.cfg.get("rt_context_lines", 4) or 4), 1)
        del self._context[:-limit * 2]

    def _engine_supports_context(self):
        spec = engines.get_engine(self.cfg.get("engine", ""))
        return bool(spec and spec.supports_context)

    def _engine_supports_stream(self):
        spec = engines.get_engine(self.cfg.get("engine", ""))
        return bool(spec and spec.supports_stream)

    def _reset_context(self):
        """清空上下文(开始 / 换范围 / 退出时调用)。"""
        self._context = []
        self._ctx_pending = None
        self._text_stamp = 0.0
        self._ctx_busy = False
        self._streaming = False

    def _maybe_refine(self):
        """文本稳定一会儿之后,带前文重译一次。

        这是第二遍:第一遍已经立刻出结果了,这一遍只是让译文更贴上下文,
        所以慢一点没关系,也不会让界面空着。同一句只做一次。
        """
        if not self.cfg.get("rt_context", True):
            return
        if self._ctx_pending is None or self._ctx_busy or self._busy:
            return
        if not self._engine_supports_context():
            return
        settle = int(self.cfg.get("rt_settle_ms", 900) or 900) / 1000.0
        if time.time() - self._text_stamp < settle:
            return
        text = self._ctx_pending
        self._ctx_pending = None
        ctx = self._context_text()
        if not ctx:
            return
        self._ctx_busy = True
        threading.Thread(target=self._refine_thread, args=(text, ctx),
                         daemon=True).start()

    def _refine_thread(self, text, ctx):
        try:
            self._worker_context(text, ctx)
        finally:
            self._ctx_busy = False

    def _worker(self, img):
        try:
            raw = core.ocr_image(img, self.cfg.get("ocr_lang", "auto"),
                                 self.cfg.get("ocr_engine", "auto"), self.cfg)
            if not raw:
                self._results.put(("empty", None))
                return
            text = raw
            if self.cfg.get("rt_merge_lines", True):
                text = core.merge_ocr_lines(raw)
            text = text.strip()
            if not text:
                self._results.put(("empty", None))
                return

            # 和上一帧几乎一样 → 直接复用上次译文,不再请求接口
            if self._last_translation and \
                    core.text_similar(text, self._last_source) >= \
                    float(self.cfg.get("rt_dedupe", 0.90)):
                self._results.put(("same", text))
                return

            engine_id = self.cfg.get("engine", "mymemory")
            key = (engine_id, self.cfg.get("target_lang", "zh-CN"), text)
            cached = self.cache.get(key)
            if cached is not None:
                self._results.put(("ok", (text, cached, engine_id, True)))
                self._text_stamp = time.time()
                return

            # ---- 第一遍:立刻翻,不等上下文,保证"秒出" ----
            # 大模型引擎顺便开流式,边生成边显示
            stream = bool(self.cfg.get("rt_stream", True)) and \
                self._engine_supports_stream()
            on_delta = None
            if stream:
                def on_delta(acc, _t=text):
                    self._results.put(("delta", (acc, _t)))

            out, used = engines.translate(text, self.cfg, engine_id,
                                          allow_fallback=True,
                                          on_delta=on_delta)
            self.cache.put(key, out)
            self._results.put(("ok", (text, out, used, False)))
            self._text_stamp = time.time()

            # ---- 第二遍:等这句话稳定下来,带上前文重译一次 ----
            # 只有大模型引擎读得懂前文;而且必须等前文已经攒够,
            # 否则第一句就用不上上下文,白跑一趟
            if self.cfg.get("rt_context", True) \
                    and self._engine_supports_context() \
                    and self._context:
                self._ctx_pending = text

        except engines.MissingKeyError as e:
            self._results.put(("needkey", e))
        except core.OcrError as e:
            self._results.put(("empty", str(e)))
        except engines.TranslateError as e:
            self._results.put(("error", str(e)))
        except Exception as e:
            log.exception("实时翻译后台出错")
            self._results.put(("error", f"{type(e).__name__}: {e}"))
        finally:
            # 必须无条件清掉,否则主循环会一直以为后台还在忙,
            # 结果队列再也没人处理 —— 整个实时翻译就卡死了
            self._busy = False

    def _worker_context(self, text, context):
        """带前文重译。译文和第一遍一样就什么都不做。"""
        try:
            out, used = engines.translate(text, self.cfg,
                                          self.cfg.get("engine"),
                                          allow_fallback=True,
                                          context=context)
            if not out or not out.strip():
                return
            out = out.strip()
            if out != self._last_translation:
                self._results.put(("refined", (text, out, used)))
        except Exception as e:
            log.info("上下文重译失败(忽略): %s", e)

    # ------------------------------------------------------ 结果回填

    def repaint(self):
        """配置(颜色/原文显示)变化后重画一次。"""
        self._showing_source = bool(self.cfg.get("rt_show_source"))
        if self.overlay and self.overlay.winfo_exists():
            self.overlay.show_translation(self._last_translation,
                                          self._last_source)

    def _drain_results(self):
        try:
            while True:
                kind, payload = self._results.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass

    def _handle(self, kind, payload):
        o = self.overlay
        if not o or not o.winfo_exists():
            return
        if kind == "delta":
            # 流式:大模型边生成边往这儿推,先把已经出来的字显示上,
            # 用户感觉是"立刻就翻出来了",不用等整句生成完。
            # (同一时刻只有一个后台任务在跑,所以这些增量一定属于当前这句)
            part, src = payload
            self._streaming = True
            self._last_source = src
            self._last_translation = part
            self.repaint()
            o.set_status(f"{time.strftime('%H:%M:%S')} · 正在翻译…", "#8fb6ff")
        elif kind == "ok":
            text, out, used, from_cache = payload
            self._fails = 0
            self._last_ok = time.time()
            was_streaming = self._streaming
            self._streaming = False
            self._last_source = text
            self._last_translation = out
            self.repaint()
            tag = "缓存" if from_cache else engines.engine_name(used)
            x1, y1, x2, y2 = self.region or (0, 0, 0, 0)
            o.set_status(f"{time.strftime('%H:%M:%S')} · {tag} · "
                         f"识别 {len(text)} 字 · 范围 {x2 - x1}×{y2 - y1}",
                         "#7f8fa3")
            # 这一句翻译完了,记进上下文,供后面几句参考
            self._push_context(text)
            if was_streaming:
                self._text_stamp = time.time()
        elif kind == "refined":
            # 第二遍(带上下文)的结果回来了,悄悄替换掉
            text, out, used = payload
            if self._last_translation == out:
                return
            self._last_translation = out
            self.repaint()
            o.set_status(f"{time.strftime('%H:%M:%S')} · {engines.engine_name(used)}"
                         f" · 已结合前文重新润色", "#6fb3a0")
        elif kind == "same":
            self._last_source = payload
            o.set_status(f"{time.strftime('%H:%M:%S')} · 画面未变化,沿用上次译文",
                         "#7f8fa3")
        elif kind == "empty":
            self._fails += 1
            if self._fails == 1 or self._fails % 10 == 0:
                o.set_status(f"{time.strftime('%H:%M:%S')} · 没识别到文字,"
                             f"试着把范围调小一点对准文字", "#ffb54d")
        elif kind == "needkey":
            self.running = False
            o.set_running(False)
            o.set_status(f"缺少 API Key:{payload}", "#ff8a80")
            self._notify(f"缺少 API Key:{payload}")
            if callable(self.on_need_key):
                self.on_need_key(getattr(payload, "engine_id", None))
        elif kind == "error":
            self._fails += 1
            o.set_status(f"{time.strftime('%H:%M:%S')} · 翻译失败:{payload}",
                         "#ff8a80")

    def _fmt_region(self):
        if not self.region:
            return "-"
        x1, y1, x2, y2 = self.region
        return f"{x2 - x1}×{y2 - y1} @({x1},{y1})"

    def _notify(self, msg):
        log.info("[实时翻译] %s", msg)
        if callable(self.on_status):
            self.on_status(msg)

    def save(self):
        try:
            core.save_config(self.cfg)
        except Exception as e:
            log.warning("保存实时翻译配置失败: %s", e)
