# -*- coding: utf-8 -*-
"""GUI 端到端自检:真的把窗口建出来,真的跑一次实时翻译。

用法:python selftest_gui.py [--seconds 12]
会自动开一个写着英文的窗口当"被翻译对象",框住它跑实时翻译,然后检查
悬浮窗里有没有出现中文译文。跑完自动关闭,不需要人工干预。
"""
import os
import sys
import time
import tkinter as tk

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import core

core.enable_dpi_awareness()

import engines            # noqa: E402
import realtime           # noqa: E402
import settings as settings_ui   # noqa: E402
import selector           # noqa: E402
import theme              # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    RESULTS.append(bool(cond))
    return bool(cond)


def collect_texts(widget, out=None):
    """递归收集一个窗口里所有可见文字,方便断言界面内容。"""
    if out is None:
        out = []
    try:
        text = widget.cget("text")
        if text:
            out.append(str(text))
    except Exception:
        pass
    try:
        for child in widget.winfo_children():
            collect_texts(child, out)
    except Exception:
        pass
    return out


def pump(root, seconds, step=0.05):
    """跑 Tk 事件循环若干秒(不像 mainloop 那样阻塞)。"""
    end = time.time() + seconds
    while time.time() < end:
        try:
            root.update()
        except tk.TclError:
            return
        time.sleep(step)


def has_cjk(text):
    return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))


def main():
    seconds = 12.0
    if "--seconds" in sys.argv:
        try:
            seconds = float(sys.argv[sys.argv.index("--seconds") + 1])
        except Exception:
            pass

    cfg = core.load_config()
    cfg["engine"] = "mymemory"        # 国内可直连的免费引擎
    cfg["rt_interval"] = 0.5
    cfg["rt_region"] = None
    cfg["rt_overlay_x"] = None
    cfg["rt_overlay_y"] = None
    cfg["ocr_engine"] = "rapidocr"

    root = tk.Tk()
    root.title("主窗口(测试用,稍后会隐藏)")
    root.geometry("400x120+1000+700")
    root.update()

    # 被监视的窗口:模拟"用户在看视频/游戏"的那个窗口
    demo = tk.Toplevel(root)
    demo.title("被翻译的窗口(测试用)")
    demo.configure(bg="white")
    demo.geometry("820x240+90+90")
    # 置顶:测试环境里桌面可能有别的窗口压在上面,会把要识别的画面挡住
    demo.attributes("-topmost", True)
    tk.Label(demo, text="Hello World", bg="white", fg="black",
             font=("Arial", 30, "bold")).pack(expand=True)
    demo.update()
    pump(root, 0.8)

    # ---------------------------------------------------------- 1. 设置界面
    win = settings_ui.SettingsWindow(root, cfg)
    pump(root, 0.4)
    check("设置窗口能打开", win.winfo_exists())
    check("设置窗口尺寸够看内容",
          win.winfo_width() >= 560 and win.winfo_height() >= 520,
          f"{win.winfo_width()}x{win.winfo_height()}")
    check("设置窗口四个选项卡都可滚动",
          len(win._areas) == 4, f"{len(win._areas)} 个滚动区")

    win.engine_var.set("deepseek")
    win._on_engine_change()
    pump(root, 0.3)
    texts = collect_texts(win.engine_detail)
    joined = " | ".join(texts)
    check("选中需 Key 引擎后出现 Key 输入框",
          win.engine_key_form is not None and
          "deepseek_key" in getattr(win.engine_key_form, "vars", {}))
    check("选中需 Key 引擎后出现申请说明",
          "需要" in joined and "Key" in joined, joined[:80])
    check("Key 输入框下方有「获取 Key」网址入口",
          any("点此获取 Key" in t for t in texts) and
          any("platform.deepseek.com" in t for t in texts),
          next((t for t in texts if "点此获取" in t), "未找到"))

    win.engine_var.set("mymemory")
    win._on_engine_change()
    pump(root, 0.3)
    texts2 = collect_texts(win.engine_detail)
    check("选中免费引擎时不索要 Key",
          any("不需要 API Key" in t for t in texts2),
          next((t for t in texts2 if "不需要" in t), "未找到"))

    win.ocr_var.set("baidu")
    win._on_ocr_change()
    pump(root, 0.3)
    ocr_texts = collect_texts(win.ocr_detail)
    check("百度 OCR 会索要 Key",
          win.ocr_key_form is not None and
          len(getattr(win.ocr_key_form, "vars", {})) == 2,
          str(list(getattr(win.ocr_key_form, "vars", {}).keys())))
    check("百度 OCR 也给出获取 Key 的网址",
          any("console.bce.baidu.com" in t for t in ocr_texts),
          next((t for t in ocr_texts if "console.bce" in t), "未找到"))

    win.ocr_var.set("rapidocr")
    win._on_ocr_change()
    win.destroy()
    pump(root, 0.3)
    check("设置窗口能关闭", not win.winfo_exists())

    # ---------------------------------------------------------- 2. 框选层
    sel = selector.RegionSelector(root, title="测试框选",
                                  initial=(10, 10, 200, 60))
    pump(root, 0.4)
    check("框选遮罩能创建", sel.top.winfo_exists())
    sel._cancel()
    pump(root, 0.2)
    check("框选可取消并返回 None", sel.rect is None)

    # ---------------------------------------------------------- 3. 实时翻译
    region = (demo.winfo_rootx(), demo.winfo_rooty(),
              demo.winfo_rootx() + demo.winfo_width(),
              demo.winfo_rooty() + demo.winfo_height())
    print(f"    监视区域: {region}", flush=True)

    # 预检:桌面上可能有别的窗口压在被监视窗口上面,那样 OCR 读到的是别人的内容,
    # 测试就会时好时坏。这里先把窗口抬到最前并确认真的能读到 Hello World。
    ready = False
    for attempt in range(5):
        demo.attributes("-topmost", True)
        demo.lift()
        pump(root, 0.6)
        try:
            probe = core.ocr_image(core.capture_region(region), "auto",
                                   "rapidocr", cfg)
        except Exception:
            probe = ""
        if "Hello" in probe:
            ready = True
            break
        print(f"    (第 {attempt + 1} 次预检没读到目标文字:{probe.strip()[:30]!r},"
              f"把窗口抬前再试)", flush=True)

    check("被监视窗口没有被别的窗口挡住", ready,
          "读到 Hello World" if ready else
          "桌面有窗口遮挡,测不了 —— 请关掉其它窗口后重跑")

    # 真实场景:开始实时翻译前主窗口会藏起来
    root.withdraw()
    pump(root, 0.6)
    check("主窗口隐藏后,被监视的窗口依然可见", demo.winfo_viewable())

    exited = {"called": False}
    session = realtime.RealtimeSession(
        root, cfg,
        on_exit=lambda: exited.__setitem__("called", True),
        on_status=lambda m: None,
        on_need_key=lambda eid: None)
    session.start(region)
    pump(root, 0.5)
    check("实时翻译悬浮窗已创建",
          session.overlay is not None and session.overlay.winfo_exists())
    check("主窗口隐藏时悬浮窗依然显示得出来",
          session.overlay.winfo_viewable(),
          f"viewable={session.overlay.winfo_viewable()} "
          f"mapped={session.overlay.winfo_ismapped()}")
    check("开始时处于运行状态", session.running and session.active)

    # ---- 监视范围框(需求 2)----
    check("选区之后能看到监视范围框",
          session.indicator is not None and session.indicator.winfo_exists(),
          "未创建")
    if session.indicator is not None:
        ix, iy = session.indicator.winfo_x(), session.indicator.winfo_y()
        iw, ih = session.indicator.winfo_width(), session.indicator.winfo_height()
        rx1, ry1, rx2, ry2 = region
        # 边框画在选区外面:窗口要完全把选区包住
        check("范围框把选区包在里面",
              ix <= rx1 and iy <= ry1 and ix + iw >= rx2 and iy + ih >= ry2,
              f"框({ix},{iy},{iw}x{ih}) 选区{region}")
        # 而且取景时不会被拍进去:OCR 结果里不能出现边框标签的文字
        img = core.capture_region(region)
        raw = core.ocr_image(img, "auto", "rapidocr", cfg)
        check("范围框没有污染监视画面(拍不到边框)",
              "实时翻译" not in raw and "范围" not in raw,
              repr(raw[:40]))

    session.set_show_region(False)
    pump(root, 0.5)
    check("取消勾选后范围框消失", session.indicator is None)
    session.set_show_region(True)
    pump(root, 0.5)
    check("重新勾选后范围框回来",
          session.indicator is not None and session.indicator.winfo_exists())

    # ---- 移动悬浮窗之后重选范围,范围框必须跟着搬过去 ----
    # (曾经的 bug:Tk 的 geometry() 在 overrideredirect 窗口上只生效了尺寸,
    #  位置还停在老地方,得手动开关一次范围框才正常)
    ov = session.overlay
    ov.geometry(f"+{ov.winfo_x() + 260}+{ov.winfo_y() + 90}")
    pump(root, 0.3)
    ov._drag_end()
    pump(root, 0.5)

    new_region = (region[0] + 300, region[1] + 420,
                  region[2] - 120, region[3] + 300)
    import selector as _sel
    _orig_select = _sel.select_region
    _sel.select_region = lambda *a, **k: new_region
    try:
        session.reselect_region()
    finally:
        _sel.select_region = _orig_select
    pump(root, 1.5)

    ind = session.indicator
    if ind is None:
        check("重选范围后范围框仍在", False, "范围框没了")
    else:
        x1, y1, x2, y2 = core.normalize_rect(new_region)
        pad, lh = realtime.RegionIndicator.PAD, ind.LABEL_H
        want_w = (x2 - x1) + pad * 2
        want_h = (y2 - y1) + pad * 2 + lh
        got = (ind.winfo_x(), ind.winfo_y(),
               ind.winfo_width(), ind.winfo_height())
        check("重选范围后范围框尺寸跟着变",
              abs(got[2] - want_w) <= 2 and abs(got[3] - want_h) <= 2,
              f"实际 {got[2]}x{got[3]} 期望 {want_w}x{want_h}")
        check("重选范围后范围框位置也跟着搬(不再停在老地方)",
              abs(got[0] - (x1 - pad)) <= 2 and abs(got[1] - (y1 - pad - lh)) <= 2,
              f"实际 ({got[0]},{got[1]}) 期望 ({x1 - pad},{y1 - pad - lh})")
        # 再验一次:新范围确实被框住了
        check("新范围被框在里面",
              got[0] <= x1 and got[1] <= y1 and
              got[0] + got[2] >= x2 and got[1] + got[3] >= y2,
              f"框{got} 范围{new_region}")

    # 把监视范围切回原来那块,后面的识别测试才有内容可读
    _sel.select_region = lambda *a, **k: region
    try:
        session.reselect_region()
    finally:
        _sel.select_region = _orig_select
    pump(root, 1.0)
    check("切回原范围后范围框也回来了",
          session.region == core.normalize_rect(region),
          str(session.region))

    # 样式面板也要能在主窗口隐藏时打开
    session.open_style_panel()
    pump(root, 0.4)
    check("主窗口隐藏时样式面板能打开",
          session.style_panel is not None and
          session.style_panel.winfo_viewable())
    check("样式面板里有「显示监视范围框」的勾选框",
          hasattr(session.style_panel, "region_var"),
          str(getattr(session.style_panel, "region_var", None)))
    session.style_panel.destroy()
    pump(root, 0.2)

    # 等它识别 + 翻译
    got = ""
    src = ""
    deadline = time.time() + seconds
    while time.time() < deadline:
        pump(root, 0.3)
        try:
            got = session.overlay.text.get("1.0", "end").strip()
            src = session._last_source
        except Exception:
            break
        if has_cjk(got) and src:
            break
    print(f"    识别到的原文: {src[:60]!r}", flush=True)
    print(f"    悬浮窗内容: {got[:60]!r}", flush=True)
    check("实时翻译识别到了屏幕上的英文", "Hello" in src, src[:40] or "空")
    check("实时翻译跑出了中文译文", has_cjk(got), got[:50] or "空")
    check("译的不是原文照抄", got.strip() != src.strip())
    check("帧签名去重有生效", session._last_sig is not None)

    # ---- 停止翻译:只停轮询,窗口和译文都留着 ----
    before = session.overlay.text.get("1.0", "end").strip()
    session.toggle_translation()
    pump(root, 1.2)
    check("「停止翻译」后轮询停止", not session.running)
    check("「停止翻译」后悬浮窗仍在",
          session.overlay is not None and session.overlay.winfo_exists())
    check("「停止翻译」后译文仍在",
          session.overlay.text.get("1.0", "end").strip() == before)
    check("「停止翻译」后按钮变成继续",
          "继续" in session.overlay.stop_btn.cget("text"),
          session.overlay.stop_btn.cget("text"))

    # ---- 继续翻译 ----
    session.toggle_translation()
    pump(root, 0.8)
    check("「继续翻译」后恢复轮询", session.running)
    check("恢复后按钮变回停止",
          "停止" in session.overlay.stop_btn.cget("text"),
          session.overlay.stop_btn.cget("text"))

    # ---- 外观:透明度/颜色/字号/位置 ----
    session.overlay.attributes("-alpha", 0.5)
    pump(root, 0.2)
    check("透明度可改",
          abs(float(session.overlay.attributes("-alpha")) - 0.5) < 0.05,
          str(session.overlay.attributes("-alpha")))

    cfg["rt_fg"] = "#ff0000"
    cfg["rt_bg"] = "#000080"
    cfg["rt_font_size"] = 30
    session.overlay.apply_style()
    pump(root, 0.3)
    check("字体颜色可改",
          session.overlay.text.cget("fg") == "#ff0000",
          session.overlay.text.cget("fg"))
    check("背景颜色可改",
          session.overlay.text.cget("bg") == "#000080",
          session.overlay.text.cget("bg"))
    check("字号可改",
          "30" in str(session.overlay.text.cget("font")),
          str(session.overlay.text.cget("font")))

    old_x = session.overlay.winfo_x()
    session.overlay.geometry(f"+{old_x + 120}+{session.overlay.winfo_y()}")
    pump(root, 0.3)
    check("悬浮窗位置可拖动",
          session.overlay.winfo_x() != old_x,
          f"{old_x} -> {session.overlay.winfo_x()}")

    # ---- 鼠标穿透 ----
    ok = session.set_click_through(True)
    pump(root, 0.2)
    check("鼠标穿透可开启", ok and session.overlay.click_through)
    session.set_click_through(False)
    pump(root, 0.2)
    check("鼠标穿透可关闭", not session.overlay.click_through)

    # ---- 退出实时翻译:整个模式结束并回调 ----
    session.exit_realtime()
    pump(root, 0.6)
    check("「退出实时翻译」后悬浮窗关闭", session.overlay is None)
    check("「退出实时翻译」后模式结束",
          not session.active and not session.running)
    check("退出后触发了回到主界面的回调", exited["called"])

    root.destroy()
    pump(root, 0.2)

    # ---------------------------------------------------------- 4. 主窗口与滚动
    check_main_window()

    print("=" * 44)
    ok = all(RESULTS)
    print(f"GUI 自检全部通过 ✓ ({len(RESULTS)} 项)" if ok
          else f"GUI 自检存在失败 ✗ ({RESULTS.count(False)}/{len(RESULTS)})")
    sys.stdout.flush()
    # Tk 关闭后解释器收尾时 Variable.__del__ 会刷一堆无害警告,直接退出更干净
    os._exit(0 if ok else 1)


def check_main_window():
    """主窗口:默认尺寸要够大,放不下时能用滚轮上下翻。"""
    import main as app_main

    root2 = tk.Tk()
    app = app_main.App(root2)
    pump(root2, 2.0)

    check("主窗口建得起来", root2.winfo_exists())
    w, h = root2.winfo_width(), root2.winfo_height()
    check("主窗口初始尺寸比旧版大", w >= 500 and h >= 560, f"{w}x{h}")
    check("屏幕装得下这个窗口",
          h <= root2.winfo_screenheight(), f"{h} <= {root2.winfo_screenheight()}")

    area = app.scroll
    check("主窗口主体是可滚动区域", isinstance(area, theme.ScrollArea))

    # 把窗口压到很矮,内容就放不下了,应该出现滚动条
    root2.geometry(f"{w}x420")
    pump(root2, 1.2)
    check("窗口变小后出现滚动条", area._bar_visible,
          f"内容高 {area.inner.winfo_reqheight()} / 可视高 {area.canvas.winfo_height()}")

    before = area.canvas.yview()
    # 造一个真实的滚轮事件(handler 靠指针位置判断滚哪个区域)
    cx = area.canvas.winfo_rootx() + area.canvas.winfo_width() // 2
    cy = area.canvas.winfo_rooty() + 10
    under = root2.winfo_containing(cx, cy)
    root2.event_generate("<MouseWheel>", delta=-120, x=cx, y=cy,
                         rootx=cx, rooty=cy, when="now")
    pump(root2, 0.5)
    after = area.canvas.yview()
    check("滚轮能向下滚动内容", after[0] > before[0],
          f"{before[0]:.3f} -> {after[0]:.3f} (指针下控件={under})")

    root2.event_generate("<MouseWheel>", delta=120, x=cx, y=cy,
                         rootx=cx, rooty=cy, when="now")
    pump(root2, 0.5)
    back = area.canvas.yview()
    check("滚轮能向上滚回去", back[0] < after[0],
          f"{after[0]:.3f} -> {back[0]:.3f}")

    # 把窗口拉到比内容还高,滚动条应该收起来
    need = area.inner.winfo_reqheight()
    root2.geometry(f"{w}x{min(need + 220, root2.winfo_screenheight() - 40)}")
    pump(root2, 1.2)
    check("窗口足够高时滚动条自动收起",
          area.canvas.winfo_height() >= area.inner.winfo_reqheight()
          and not area._bar_visible,
          f"内容高 {area.inner.winfo_reqheight()} / 可视高 {area.canvas.winfo_height()} "
          f"/ 滚动条 {area._bar_visible}")

    try:
        root2.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
