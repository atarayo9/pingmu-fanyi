# -*- coding: utf-8 -*-
"""屏幕区域框选遮罩 —— 「截屏翻译」和「实时翻译」共用。

框选层覆盖整个虚拟桌面(支持多显示器),拖动鼠标画出矩形,松开即返回
屏幕坐标 (x1, y1, x2, y2);Esc 或右键取消返回 None。
"""
import tkinter as tk

import core

FONT = "Microsoft YaHei UI"


class RegionSelector:
    """全屏半透明框选层。

    hint      —— 中间显示的提示文字
    initial   —— 已有选区(实时翻译再次框选时高亮显示)
    title     —— 顶部标题
    """

    def __init__(self, master, hint="", initial=None, title="框选要翻译的区域"):
        x, y, w, h = core.virtual_screen_rect()
        self.origin = (x, y)
        self.rect = None
        self.initial = tuple(initial) if initial else None

        self.top = tk.Toplevel(master)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.attributes("-alpha", 0.35)
        self.top.geometry(f"{w}x{h}+{x}+{y}")

        self.canvas = tk.Canvas(self.top, cursor="crosshair",
                                highlightthickness=0, bg="#101010")
        self.canvas.pack(fill="both", expand=True)

        self._start = None
        self._rect_id = None
        self._label_id = None

        self.canvas.create_text(
            w // 2, h // 2 - 34, text=title, fill="white",
            font=(FONT, 17, "bold"))
        self.canvas.create_text(
            w // 2, h // 2,
            text=hint or "按住鼠标左键拖动,框选要翻译的区域\n"
                         "松开鼠标即开始 ·右键或 Esc 取消",
            fill="#c9d4e0", font=(FONT, 12), justify="center")

        if self.initial:
            ix1, iy1, ix2, iy2 = core.normalize_rect(self.initial)
            self.canvas.create_rectangle(
                ix1 - x, iy1 - y, ix2 - x, iy2 - y,
                outline="#2f7df6", width=2, dash=(6, 4))
            self.canvas.create_text(
                ix1 - x + 4, iy1 - y - 12, anchor="sw", fill="#2f7df6",
                font=(FONT, 10), text="上次的范围(可直接重新框选)")

        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.top.bind("<Escape>", self._cancel)
        self.top.bind("<Button-3>", self._cancel)
        self.top.focus_force()

    # ---------------------------------------------------------- 交互

    def _press(self, e):
        self._start = (e.x, e.y)
        self.canvas.delete(self._rect_id)
        self.canvas.delete(self._label_id)
        self._rect_id = self.canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline="#ff3b30", width=2)
        self._fill_id = self.canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline="", fill="#ffffff", stipple="gray25")
        self._label_id = self.canvas.create_text(
            e.x + 6, e.y - 12, text="", fill="#ff6b60", anchor="sw",
            font=("Consolas", 11, "bold"))

    def _drag(self, e):
        if not self._start:
            return
        x0, y0 = self._start
        self.canvas.coords(self._rect_id, x0, y0, e.x, e.y)
        self.canvas.coords(self._fill_id, x0, y0, e.x, e.y)
        self.canvas.coords(self._label_id, min(x0, e.x) + 6, min(y0, e.y) - 8)
        self.canvas.itemconfigure(
            self._label_id, text=f"{abs(e.x - x0)} × {abs(e.y - y0)}")

    def _release(self, e):
        if not self._start:
            return
        x0, y0 = self._start
        ox, oy = self.origin
        # 拖得太小视为误触,沿用上次范围(如果有)
        if abs(e.x - x0) >= 6 and abs(e.y - y0) >= 6:
            self.rect = core.normalize_rect((x0 + ox, y0 + oy,
                                             e.x + ox, e.y + oy))
        elif self.initial:
            self.rect = core.normalize_rect(self.initial)
        self._finish()

    def _cancel(self, _e=None):
        self.rect = None
        self._finish()

    def _finish(self):
        try:
            self.top.destroy()
        except Exception:
            pass

    def run(self):
        """显示框选层并阻塞,返回 (x1, y1, x2, y2) 或 None。"""
        try:
            self.top.wait_window()
        except Exception:
            pass
        return self.rect


def select_region(master, hint="", initial=None, title="框选要翻译的区域"):
    """便捷入口:弹一次框选层,返回屏幕矩形或 None。"""
    return RegionSelector(master, hint=hint, initial=initial, title=title).run()
