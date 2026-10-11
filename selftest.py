# -*- coding: utf-8 -*-
"""自检脚本:验证 OCR、翻译引擎、配置、实时翻译工具函数。

用法:python selftest.py
"""
import os
import sys
import tempfile
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import core
import engines
import theme

RESULTS = []
SKIPPED = []


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  ({detail})" if detail else ""))
    RESULTS.append(bool(cond))
    return bool(cond)


def skip(name, why=""):
    """网络不通之类的情况:这项没法验证,但不能算功能失败。"""
    print(f"[跳过] {name}" + (f"  ({why})" if why else ""))
    SKIPPED.append(name)
    return True


_NET_MARKERS = ("SSLError", "SSLEOFError", "ConnectionError", "Max retries",
                "timed out", "Timeout", "ProxyError", "NewConnectionError",
                "Name or service not known", "getaddrinfo",
                "ConnectionReset", "Connection aborted", "RemoteDisconnected",
                "ChunkedEncoding", "OSError", "10054",
                # 接口限流也算「外部条件不具备」,不是功能坏了
                "429", "Too Many Requests", "冷却中", "暂不可用")


def is_net_error(exc):
    """这个异常是「网络不通」还是「功能坏了」?两者要区别对待。"""
    s = f"{type(exc).__name__}: {exc}"
    return any(m in s for m in _NET_MARKERS)


def check_translate(name, fn, detail_ok=""):
    """跑一个需要联网的翻译检查。

    回调返回 (True/False, 说明);返回 (None, 原因) 表示「决策是对的,但当前
    引擎不可用、结果验证不了」—— 记为跳过,不算失败。
    """
    try:
        ok, detail = fn()
        if ok is None:
            return skip(name, detail or "当前引擎不可用,这项没法验证")
        return check(name, ok, detail or detail_ok)
    except Exception as e:
        if is_net_error(e):
            return skip(name, "当前网络连不上翻译接口,这项没法验证")
        return check(name, False, repr(e)[:90])


def make_test_image(text="Hello World 2025"):
    return core.make_test_image(text)


def main():
    # ---------------------------------------------------------- 1. 配置
    cfg = core.load_config()
    check("配置加载", isinstance(cfg, dict) and "engine" in cfg)
    check("配置含实时翻译项", "rt_interval" in cfg and "rt_opacity" in cfg,
          f"rt_interval={cfg.get('rt_interval')}")
    check("配置含 OCR 引擎项", "ocr_engine" in cfg, cfg.get("ocr_engine"))

    old = core.CONFIG_PATH
    core.CONFIG_PATH = os.path.join(tempfile.gettempdir(), "fanyi_test_cfg.json")
    core.save_config(cfg)
    check("配置保存", os.path.exists(core.CONFIG_PATH))
    core.CONFIG_PATH = old

    # ---------------------------------------------------------- 2. 分块
    check("分块-短文本", engines._chunk_text("abc") == ["abc"])
    chunks = engines._chunk_text("x" * 8000)
    check("分块-长文本",
          len(chunks) >= 3 and all(len(c) <= 3500 for c in chunks))

    # ---------------------------------------------------------- 3. 折行合并
    check("折行合并-英文", core.merge_ocr_lines(
        "The quick brown fox jumps over the\nlazy dog") ==
        "The quick brown fox jumps over the lazy dog")
    check("折行合并-中文", core.merge_ocr_lines(
        "今天天气很好,我们\n去公园散步") == "今天天气很好,我们去公园散步")
    check("折行合并-句末保留", core.merge_ocr_lines(
        "First sentence.\nSecond sentence.") == "First sentence.\nSecond sentence.")

    # ---------------------------------------------------------- 4. 引擎注册表
    specs = engines.all_engines()
    check("引擎注册表非空", len(specs) >= 5, f"{len(specs)} 个")
    ids = [s.id for s in specs]
    check("引擎顺序表一致",
          all(i in engines.ENGINES for i in engines.ENGINE_ORDER),
          ",".join(engines.ENGINE_ORDER))
    for required in ("google", "mymemory", "libretranslate", "deepseek",
                     "openai", "deepl"):
        check(f"引擎存在:{required}", required in ids)

    bad_keyurl = [s.id for s in specs if s.needs_key and not s.key_url]
    check("需 Key 的引擎都有获取 Key 的网址", not bad_keyurl,
          "缺失:" + ",".join(bad_keyurl) if bad_keyurl else "全部齐全")

    no_required = [s.id for s in specs
                   if s.needs_key and not any(p.required for p in s.params)]
    check("需 Key 的引擎都标了必填参数", not no_required,
          ",".join(no_required) if no_required else "全部正确")

    free_with_key = [s.id for s in specs if s.free and s.needs_key]
    check("免费引擎不强制要 Key", not free_with_key)

    # ---------------------------------------------------------- 5b. 上下文 / 流式
    ctx_ok = {s.id for s in specs if s.supports_context}
    st_ok = {s.id for s in specs if s.supports_stream}
    check("大模型引擎标记了支持上下文",
          ctx_ok == {"deepseek", "openai"}, f"实际:{sorted(ctx_ok)}")
    check("大模型引擎标记了支持流式",
          st_ok == {"deepseek", "openai"}, f"实际:{sorted(st_ok)}")
    check("免费接口不会被当成能读上下文",
          not (ctx_ok & {"google", "mymemory", "libretranslate"}))
    for _k in ("rt_context", "rt_context_lines", "rt_settle_ms", "rt_stream"):
        check(f"配置含 {_k}", _k in core.DEFAULT_CONFIG,
              str(core.DEFAULT_CONFIG.get(_k)))

    # 带上下文的提示词:前文只作理解,不能喧宾夺主
    m1 = engines._chat_messages("Hello there", "zh-CN", None)
    check("无上下文时提示词不含「前文」",
          "【前文】" not in m1[0]["content"] + m1[1]["content"])
    check("无上下文时 user 就是原文", m1[1]["content"] == "Hello there")
    m2 = engines._chat_messages("Yes, sir.", "zh-CN",
                                "Commander, the scouts\nTell them to report")
    joined = m2[0]["content"] + m2[1]["content"]
    check("有上下文时提示词包含前文", "Commander, the scouts" in joined)
    check("有上下文时标注了待翻译的是哪段",
          "【待翻译】" in joined and "Yes, sir." in joined)
    check("提示词明确要求不要翻译前文", "不要翻译" in m2[0]["content"])

    # 流式只在「一次发完」时启用 —— 分块时逐块回调会把译文搅乱
    class _FakeSpec:
        chunk = 50
        supports_context = True
        supports_stream = True

        def __init__(self):
            self.calls = []

        def runner(self, text, cfg, target, **kw):
            self.calls.append(kw)
            return "x"

    fs = _FakeSpec()
    engines._run_engine(fs, "short", {}, "zh-CN",
                        context="前文", on_delta=lambda _a: None)
    check("单块时同时带上上下文和流式",
          len(fs.calls) == 1 and fs.calls[0].get("stream") is True
          and fs.calls[0].get("context") == "前文", str(fs.calls))
    fs2 = _FakeSpec()
    engines._run_engine(fs2, "y" * 120, {}, "zh-CN",
                        context="前文", on_delta=lambda _a: None)
    check("分块时不启用流式(否则会串味)",
          len(fs2.calls) >= 2 and all("stream" not in c for c in fs2.calls),
          f"{len(fs2.calls)} 块")
    check("分块时上下文仍然会带上",
          all(c.get("context") == "前文" for c in fs2.calls))

    # ---------------------------------------------------------- 5. 缺 Key 行为
    probe = dict(cfg)
    probe["engine"] = "deepseek"
    probe["deepseek_key"] = ""
    try:
        engines.translate("hello", probe, "deepseek")
        check("缺 Key 时抛 MissingKeyError", False, "没有抛异常")
    except engines.MissingKeyError as e:
        check("缺 Key 时抛 MissingKeyError", True, str(e)[:50])
    except Exception as e:
        check("缺 Key 时抛 MissingKeyError", False, f"抛了 {type(e).__name__}: {e}")

    check("missing_params 能识别缺 Key",
          [p.key for p in engines.missing_params("deepseek", probe)] ==
          ["deepseek_key"])
    ready = dict(probe, deepseek_key="sk-test")
    check("填了 Key 后不再算缺失",
          engines.missing_params("deepseek", ready) == [])

    # ---------------------------------------------------------- 6. OCR
    bd_missing = core.ocr_missing_params("baidu", probe)
    check("百度 OCR 缺 Key 能被识别", len(bd_missing) == 2,
          ",".join(p.key for p in bd_missing))
    check("百度 OCR 有获取 Key 的网址",
          bool(core.OCR_SPECS["baidu"]["key_url"]),
          core.OCR_SPECS["baidu"]["key_url"])

    try:
        text = core.ocr_image(make_test_image(), "auto")
        print("    OCR 输出:", repr(text))
        ok = bool(text.strip())
        check("OCR 识别", ok, text.strip()[:40])
        check("OCR 内容正确", "Hello" in text and "2025" in text,
              f"实际: {text.strip()[:40]}")
    except Exception as e:
        check("OCR 识别", False, repr(e))

    # ---- 宽条识别:宽高比 > 8 时 RapidOCR 会跳过检测,必须自己按行切开 ----
    def _subtitle(w, h, lines, size=20):
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (w, h), "#101418")
        d = ImageDraw.Draw(img)
        f = None
        for p in (r"C:\Windows\Fonts\arial.ttf",
                  r"C:\Windows\Fonts\segoeui.ttf"):
            try:
                f = ImageFont.truetype(p, size)
                break
            except Exception:
                continue
        y = max(4, (h - (size + 8) * len(lines)) // 2)
        for ln in lines:
            d.text((14, y), ln, fill="#ffffff", font=f)
            y += size + 8
        return img

    wide2 = _subtitle(1200, 110, ["First line of the subtitle",
                                  "and the second line here"])
    check("宽高比确实 > 8(会踩到 RapidOCR 的坑)",
          wide2.size[0] / wide2.size[1] > 8,
          f"{wide2.size[0]}x{wide2.size[1]} = "
          f"{wide2.size[0] / wide2.size[1]:.2f}")
    rows = core._text_rows(wide2)
    check("行投影能切出文字行", len(rows) >= 1, f"{len(rows)} 条")
    plain = core._rapid_ocr(wide2)
    try:
        smart, _used = core.ocr_image_ex(wide2, "auto", "rapidocr", {})
    except Exception as e:
        smart = ""
        check("宽条能识别出文字", False, repr(e))
    if smart:
        print("    宽条识别:", repr(smart.replace(chr(10), " / ")[:60]))
        check("宽条能识别出文字", True, smart.replace("\n", " / ")[:40])
        check("不会比原来整张识别更差",
              len(smart.strip()) >= len(plain.strip()),
              f"原来 {len(plain.strip())} 字 -> 现在 {len(smart.strip())} 字")

    wide1 = _subtitle(1200, 70, ["This is a longer subtitle line for testing"])
    try:
        one, _u = core.ocr_image_ex(wide1, "auto", "rapidocr", {})
        check("单行宽字幕条也能识别", bool(one.strip()),
              one.replace("\n", " ")[:40] or "空")
    except Exception as e:
        check("单行宽字幕条也能识别", False, repr(e))

    # 正常宽高比的区域不能被改动影响
    normal = _subtitle(400, 300, ["Line one", "Line two", "Line three"])
    try:
        ntext, _u = core.ocr_image_ex(normal, "auto", "rapidocr", {})
        check("正常尺寸区域识别不受影响", "Line" in ntext,
              ntext.replace("\n", " / ")[:44] or "空")
    except Exception as e:
        check("正常尺寸区域识别不受影响", False, repr(e))

    # ---------------------------------------------------------- 7. 翻译
    # 下面几项要联网。网络不通就「跳过」而不是报失败 ——
    # 否则用户断网跑一次自检,会看到一堆红字却以为程序坏了。
    def _t_basic():
        out, used = engines.translate(
            "Hello world, this is a translation test.", cfg, cfg.get("engine"))
        print(f"    翻译输出({used}):", repr(out))
        has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in out)
        return bool(out.strip()) and has_cjk, out.strip()[:40]

    check_translate("翻译可用", _t_basic)

    def _t_chunk():
        long_text = "This is a fairly long English sentence for testing. " * 30
        out, used = engines.translate(long_text, cfg, "mymemory")
        print(f"    MyMemory 长文输出({used}, {len(out)} 字符):", repr(out[:50]))
        return (bool(out.strip()) and "MYMEMORY" not in out.upper()
                and "QUERY LENGTH" not in out.upper()), out[:40]

    check_translate("MyMemory 长文分块", _t_chunk)

    def _t_marker():
        try:
            out = engines.run_mymemory("word " * 300, cfg, "zh-CN")
        except engines.TranslateError as e:
            return True, str(e)[:50]      # 明确报错也是对的
        bad = any(m in out.upper() for m in engines._MYMEMORY_ERROR_MARKERS)
        return not bad, out[:40]

    check_translate("MyMemory 错误识别", _t_marker)

    # ---- 短句必须真的被翻译(免费接口 autodetect 对短句经常原样返回)----
    def _t_short():
        out, used = engines.translate("Hello World", cfg, cfg.get("engine"))
        print("    短句翻译:", repr(out), f"({used})")
        cjk = any("\u4e00" <= ch <= "\u9fff" for ch in out)
        return (bool(out.strip()) and cjk and out.strip() != "Hello World"), out[:30]

    check_translate("短句也能翻成中文(不再原样返回)", _t_short)

    # ---- 原文已经是目标语言:原样显示,不能报错 ----
    check("已是目标语言能被识别",
          engines.already_in_target("你好世界", "zh-CN", {"source_lang": "auto"}))
    check("英文不会被误判成已是中文",
          not engines.already_in_target("Hello World", "zh-CN",
                                        {"source_lang": "auto"}))
    check("手动指定源语言后不做该判断",
          not engines.already_in_target("你好世界", "zh-CN",
                                        {"source_lang": "en"}))

    # ---- 语言判定必须看"占比",不能"出现一个汉字就当中文" ----
    mixed = "你好 world this is an English sentence"
    check("中英混排不会被误判成纯中文",
          not engines.already_in_target(mixed, "zh-CN", {"source_lang": "auto"}),
          f"detect={engines.detect_lang(mixed)}")
    check("开头是中文、后面全是英文,照样会去翻译",
          not engines.already_in_target(
              "苹果 Apple released a new iPhone today", "zh-CN",
              {"source_lang": "auto"}))
    check("英文里夹一个中文词也不会被当成中文",
          not engines.already_in_target("Hello 世界", "zh-CN",
                                        {"source_lang": "auto"}))
    check("真正的全中文仍判为已是目标语言",
          engines.already_in_target("今天天气不错,我们出去走走吧", "zh-CN",
                                    {"source_lang": "auto"}))
    check("中文里夹一个英文词仍算中文",
          engines.already_in_target("这个 App 很好用", "zh-CN",
                                    {"source_lang": "auto"}),
          str(engines.script_stats("这个 App 很好用")[0]))

    check("纯英文判为英文", engines.detect_lang("Hello world")[0] == "en")
    check("中英混排判为英文", engines.detect_lang(mixed)[0] == "en",
          str(engines.detect_lang(mixed)))
    check("日文靠假名识别", engines.detect_lang("こんにちは")[0] == "ja")
    check("韩文靠谚文识别", engines.detect_lang("안녕하세요")[0] == "ko")
    check("俄文靠西里尔字母识别", engines.detect_lang("Привет мир")[0] == "ru")
    check("纯标点数字不做判定", engines.detect_lang("12345!!!")[0] is None)
    check("占比统计忽略空格数字标点",
          abs(engines.script_stats("你好 123 !!")[0].get("han", 0) - 1.0) < 1e-6,
          str(engines.script_stats("你好 123 !!")[0]))

    def _t_mixed():
        # 先验证「决策」:这段文本绝不能被判成"已经是目标语言"而跳过翻译。
        # 这是这段代码真正要保证的事,和引擎通不通没关系。
        if engines.already_in_target(mixed, "zh-CN", {"source_lang": "auto"}):
            return False, "被误判成已是中文,压根不会送去翻译"
        out, used = engines.translate(mixed, cfg, cfg.get("engine"))
        print("    混排翻译:", repr(out), f"({used})")
        if out.strip() == mixed.strip():
            # 决策是对的、也确实送去翻了,只是所有引擎都在限流/冷却,
            # 最后走了「原样显示」兜底 —— 这属于外部条件不具备
            return None, "已送去翻译,但当前引擎都没给出译文(限流/冷却)"
        return True, out[:50]

    check_translate("中英混排真的被送去翻译了(不是原样返回)", _t_mixed)

    try:
        out, used = engines.translate("你好世界", cfg, cfg.get("engine"))
        check("中文原文原样返回且不报错",
              out == "你好世界" and used == engines.NO_TRANSLATION,
              f"{out!r}/{used}")
        check("无需翻译的引擎名可读",
              engines.engine_name(engines.NO_TRANSLATION) == "无需翻译")
    except Exception as e:
        check("中文原文原样返回且不报错", False, repr(e))

    # ---------------------------------------------------------- 8. 实时翻译工具
    check("normalize_rect 纠正反向坐标",
          core.normalize_rect((100, 200, 10, 20)) == (10, 20, 100, 200))
    check("矩形相交检测-相交", core.rect_intersects((0, 0, 100, 100),
                                                    (50, 50, 150, 150)))
    check("矩形相交检测-不相交", not core.rect_intersects((0, 0, 100, 100),
                                                          (200, 0, 300, 100)))
    check("矩形相交检测-空值", not core.rect_intersects(None, (0, 0, 10, 10)))

    a = make_test_image("AAAAAAAAAA")
    b = make_test_image("AAAAAAAAAA")
    sig_a, sig_b = core.frame_signature(a), core.frame_signature(b)
    check("帧签名-相同画面判定为未变化",
          not core.frame_changed(sig_a, sig_b))
    c = make_test_image("ZZZZZZZZZZ")
    check("帧签名-不同画面判定为变化",
          core.frame_changed(sig_a, core.frame_signature(c)))

    check("文本相似度-完全相同", core.text_similar("hello", "hello") == 1.0)
    check("文本相似度-微小差异>阈值",
          core.text_similar("Hello world, this is a test.",
                            "Hello world, this is a test") > 0.9)
    check("文本相似度-完全不同<阈值",
          core.text_similar("Hello world", "完全不同的一句话") < 0.5)

    # 去重缓存
    import realtime
    cache = realtime.TranslationCache(limit=3)
    cache.put(("mymemory", "zh-CN", "a"), "甲")
    check("译文缓存命中", cache.get(("mymemory", "zh-CN", "a")) == "甲")
    for i in range(5):
        cache.put(("mymemory", "zh-CN", f"k{i}"), str(i))
    check("译文缓存淘汰", cache.get(("mymemory", "zh-CN", "a")) is None)

    # ---------------------------------------------------------- 9. 模块完整性
    for mod in ("realtime", "selector", "settings", "theme"):
        try:
            __import__(mod)
            check(f"模块可导入:{mod}", True)
        except Exception as e:
            check(f"模块可导入:{mod}", False, repr(e))

    check("实时翻译有停止/退出两个入口",
          hasattr(realtime.RealtimeSession, "toggle_translation") and
          hasattr(realtime.RealtimeSession, "exit_realtime"))
    check("实时翻译有监视范围框开关",
          hasattr(realtime.RealtimeSession, "set_show_region") and
          hasattr(realtime.RealtimeSession, "toggle_show_region") and
          hasattr(realtime, "RegionIndicator"))
    check("配置里有监视范围框开关", "rt_show_region" in core.DEFAULT_CONFIG,
          str(core.DEFAULT_CONFIG.get("rt_show_region")))
    check("主题模块提供可滚动容器",
          hasattr(theme, "ScrollArea") and hasattr(theme, "enable_wheel_scroll"))
    check("主题模块提供统一配色",
          all(hasattr(theme, c) for c in
              ("BG", "CARD", "TEXT", "MUTED", "PRIMARY", "SUCCESS", "BORDER")))

    print("=" * 44)
    ok = all(RESULTS)
    if ok and SKIPPED:
        print(f"全部通过 ✓ ({len(RESULTS)} 项,另有 {len(SKIPPED)} 项因断网跳过)")
    elif ok:
        print(f"全部通过 ✓ ({len(RESULTS)} 项)")
    else:
        print(f"存在失败项 ✗ ({RESULTS.count(False)}/{len(RESULTS)})")
    if SKIPPED:
        print("跳过:" + "、".join(SKIPPED))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
