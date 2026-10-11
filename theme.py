# -*- coding: utf-8 -*-
"""统一的界面主题:配色、字体、控件工厂、可滚动容器。

把外观收在一处,主界面 / 设置 / 实时翻译悬浮窗都从这里取色,避免各写各的
导致风格不统一。
"""
import tkinter as tk
from tkinter import ttk

FONT = "Microsoft YaHei UI"

# ---------------------------------------------------------------- 配色
BG = "#eef1f6"          # 窗口底色
CARD = "#ffffff"        # 卡片底
CARD_ALT = "#f7f9fc"    # 次级卡片
BORDER = "#dde3ec"      # 描边
TEXT = "#1e2632"        # 主文字
MUTED = "#78849a"       # 次要文字
PRIMARY = "#2f6fed"     # 主按钮
PRIMARY_HOVER = "#2559c9"
SUCCESS = "#0e9f7e"     # 实时翻译按钮
SUCCESS_HOVER = "#0b8166"
DANGER = "#e05252"
DANGER_HOVER = "#c53f3f"
WARN = "#b35309"
WARN_BG = "#fff7ea"
WARN_BORDER = "#f0d9b5"
LINK = "#1a6fe0"
GHOST = "#e4e9f1"       # 次要按钮
GHOST_HOVER = "#d5dce8"
OK_FG = "#1e8e3e"
ERR_FG = "#c0392b"

# 实时翻译悬浮窗默认配色(与 config 默认值一致)
OVERLAY_BG = "#12171f"
OVERLAY_HEAD = "#1c2431"


def apply_theme(root):
    """设置 ttk 主题。用 clam 才能让自定义配色在所有控件上生效。"""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(".", background=BG, foreground=TEXT, font=(FONT, 9))
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("CardAlt.TFrame", background=CARD_ALT)
    style.configure("Head.TFrame", background=BG)

    style.configure("TLabel", background=BG, foreground=TEXT, font=(FONT, 9))
    style.configure("Card.TLabel", background=CARD, foreground=TEXT, font=(FONT, 9))
    style.configure("CardAlt.TLabel", background=CARD_ALT, foreground=TEXT,
                    font=(FONT, 9))
    style.configure("Muted.TLabel", background=BG, foreground=MUTED,
                    font=(FONT, 8))
    style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED,
                    font=(FONT, 8))
    style.configure("Title.TLabel", background=BG, foreground=TEXT,
                    font=(FONT, 13, "bold"))
    style.configure("Section.TLabel", background=CARD, foreground=TEXT,
                    font=(FONT, 10, "bold"))

    # 输入框 / 下拉框
    style.configure("TEntry", fieldbackground=CARD, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, padding=4)
    style.configure("TCombobox", fieldbackground=CARD, background=CARD,
                    foreground=TEXT, bordercolor=BORDER, lightcolor=BORDER,
                    darkcolor=BORDER, arrowcolor=MUTED, padding=4,
                    selectbackground=CARD, selectforeground=TEXT)
    style.map("TCombobox",
              fieldbackground=[("readonly", CARD), ("disabled", CARD_ALT)],
              background=[("readonly", CARD)],
              bordercolor=[("focus", PRIMARY)])

    # 滚动条:细一点、低调一点
    style.configure("Vertical.TScrollbar", background="#c6cfdd",
                    troughcolor=BG, bordercolor=BG, arrowcolor=MUTED,
                    gripcount=0, width=11, relief="flat")
    style.map("Vertical.TScrollbar", background=[("active", "#aab6c9")])
    style.configure("Card.Vertical.TScrollbar", background="#c6cfdd",
                    troughcolor=CARD, bordercolor=CARD, arrowcolor=MUTED,
                    gripcount=0, width=11, relief="flat")

    # 选项卡
    style.configure("TNotebook", background=BG, borderwidth=0,
                    tabmargins=(4, 6, 4, 0))
    style.configure("TNotebook.Tab", padding=(18, 8), font=(FONT, 9, "bold"),
                    background="#e0e6f0", foreground=MUTED, borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", CARD)],
              foreground=[("selected", PRIMARY)],
              expand=[("selected", (0, 0, 0, 2))])

    style.configure("TScale", background=CARD, troughcolor="#dbe2ee",
                    bordercolor=CARD, lightcolor=PRIMARY, darkcolor=PRIMARY)

    for name, bg in (("Card", CARD), ("Page", BG)):
        style.configure(f"{name}.TCheckbutton", background=bg, foreground=TEXT,
                        font=(FONT, 9), focuscolor=bg)
        style.map(f"{name}.TCheckbutton", background=[("active", bg)])
        style.configure(f"{name}.TRadiobutton", background=bg, foreground=TEXT,
                        font=(FONT, 9), focuscolor=bg)
        style.map(f"{name}.TRadiobutton", background=[("active", bg)])

    style.configure("TButton", padding=(12, 6), font=(FONT, 9),
                    borderwidth=0, relief="flat")
    return style


# ---------------------------------------------------------------- 控件工厂

def flat_button(master, text, command, kind="ghost", font=None, padx=12,
                pady=6, width=None, **kw):
    """扁平按钮,带鼠标悬停变色(比 ttk 按钮更容易统一配色)。"""
    if kind == "primary":
        bg, hover, fg = PRIMARY, PRIMARY_HOVER, "#ffffff"
    elif kind == "success":
        bg, hover, fg = SUCCESS, SUCCESS_HOVER, "#ffffff"
    elif kind == "danger":
        bg, hover, fg = DANGER, DANGER_HOVER, "#ffffff"
    else:
        bg, hover, fg = GHOST, GHOST_HOVER, TEXT

    btn = tk.Button(master, text=text, command=command, relief="flat",
                    bg=bg, fg=fg, activebackground=hover, activeforeground=fg,
                    font=font or (FONT, 9), padx=padx, pady=pady,
                    borderwidth=0, cursor="hand2", **kw)
    if width:
        btn.configure(width=width)
    btn.bind("<Enter>", lambda _e: btn.configure(bg=hover)
             if str(btn["state"]) != "disabled" else None)
    btn.bind("<Leave>", lambda _e: btn.configure(bg=bg)
             if str(btn["state"]) != "disabled" else None)
    return btn


def link_label(master, text, url, open_url, bg=CARD, font=None, **kw):
    """可点击的网址入口。"""
    lbl = tk.Label(master, text=text, fg=LINK, bg=bg, cursor="hand2",
                   anchor="w", justify="left",
                   font=font or (FONT, 9, "underline"), **kw)
    lbl.bind("<Button-1>", lambda _e: open_url(url))
    lbl.bind("<Enter>", lambda _e: lbl.configure(fg="#0b4fb0"))
    lbl.bind("<Leave>", lambda _e: lbl.configure(fg=LINK))
    return lbl


def badge(master, is_free, bg=CARD):
    return tk.Label(master, text=" 免费 " if is_free else " 需 Key ",
                    bg="#e3f5e6" if is_free else "#fdeee0",
                    fg=OK_FG if is_free else WARN,
                    font=(FONT, 8, "bold"))


def separator(master, bg=CARD, pady=8):
    f = tk.Frame(master, bg=bg)
    line = tk.Frame(f, bg=BORDER, height=1)
    line.pack(fill="x", pady=pady)
    return f


# ---------------------------------------------------------------- 可滚动容器

class ScrollArea(tk.Frame):
    """纵向可滚动容器:把内容放进 `area.inner`。

    内容比窗口矮时不显示滚动条;放不下时自动出现,并支持鼠标滚轮。
    宽度始终跟随容器,所以内部的 fill/expand 布局照常工作。
    """

    def __init__(self, master, bg=BG, bar_style="Vertical.TScrollbar", **kw):
        super().__init__(master, bg=bg, **kw)
        self._bg = bg
        self._bar_visible = False
        self._syncing = False

        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0,
                                takefocus=0)
        self.vbar = ttk.Scrollbar(self, orient="vertical",
                                  command=self.canvas.yview,
                                  style=bar_style)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner,
                                              anchor="nw")
        self.inner.bind("<Configure>", lambda _e: self._sync())
        self.canvas.bind("<Configure>", lambda _e: self._sync())

    def _sync(self):
        if self._syncing:
            return
        self._syncing = True
        try:
            cw = max(self.canvas.winfo_width(), 1)
            ch = max(self.canvas.winfo_height(), 1)
            ih = self.inner.winfo_reqheight()
            target_h = max(ih, ch)
            self.canvas.itemconfigure(self._win, width=cw, height=target_h)
            self.canvas.configure(scrollregion=(0, 0, cw, target_h))

            need = ih > ch + 1
            if need != self._bar_visible:
                self._bar_visible = need
                if need:
                    self.vbar.pack(side="right", fill="y")
                else:
                    self.vbar.pack_forget()
                    self.canvas.yview_moveto(0)
        finally:
            self._syncing = False

    def scroll_by(self, delta):
        """滚轮:delta>0 向上。"""
        if not self._bar_visible:
            return False
        self.canvas.yview_scroll(-1 if delta > 0 else 1, "units")
        return True

    def scroll_to_top(self):
        self.canvas.yview_moveto(0)


def is_descendant(widget, ancestor):
    """widget 是不是 ancestor 的后代(用于判断滚轮落在哪个滚动区)。"""
    w = widget
    while w is not None:
        if w is ancestor:
            return True
        try:
            w = w.master
        except Exception:
            return False
    return False


def enable_wheel_scroll(window, areas, skip=(tk.Text, tk.Listbox)):
    """给一个窗口接上鼠标滚轮:滚轮落在哪个滚动区就滚哪个。

    Text / Listbox 自己有滚动条,滚轮交给它们自己处理,不抢。
    """

    def on_wheel(event):
        w = event.widget
        if isinstance(w, skip):
            return None
        try:
            under = window.winfo_containing(event.x_root, event.y_root)
        except Exception:
            under = None
        if under is None:
            return None
        for area in areas:
            try:
                if not area.winfo_exists():
                    continue
            except Exception:
                continue
            if under is area or is_descendant(under, area):
                if area.scroll_by(event.delta):
                    return "break"
                return None
        return None

    window.bind("<MouseWheel>", on_wheel, add="+")
    return on_wheel
