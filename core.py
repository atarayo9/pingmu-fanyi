# -*- coding: utf-8 -*-
"""屏幕翻译工具核心:区域截图、OCR 文字识别、文本处理、配置管理。

- OCR 引擎可选:自动 / RapidOCR(离线) / Windows 系统 OCR / Tesseract / 百度在线 OCR;
- 翻译引擎见 engines.py(免费 / 需 Key 由那里统一描述);
- 截图:Windows 虚拟屏幕坐标 + PIL,支持多显示器;
- 实时翻译需要的帧去重、文本相似度工具也在本模块。
"""
import os
import sys

# 是不是被 PyInstaller 打包成了 exe
FROZEN = bool(getattr(sys, "frozen", False))
_SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))


def _app_dir():
    """配置和日志放哪。

    打包成 exe 之后,依赖都封在程序里、解包目录是临时的,所以 config.json
    和 fanyi.log 要放到 **exe 旁边**,用户才能看见、才能自己改。
    """
    if FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return _SOURCE_DIR


# 允许从项目本地 vendor 目录加载第三方依赖(无需全局安装);
# 打包成 exe 后依赖已经打进程序里了,不需要这一步。
_VENDOR = os.path.join(_SOURCE_DIR, "vendor")
if not FROZEN and os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

import asyncio
import ctypes
import difflib
import io
import json
import logging
import threading
import webbrowser

from PIL import ImageGrab

import engines
from engines import (TranslateError, MissingKeyError, ParamSpec,  # noqa: F401
                     translate, test_engine, _chunk_text,
                     _MYMEMORY_ERROR_MARKERS, TARGET_LANGS, target_name)

log = logging.getLogger("fanyi")

APP_DIR = _app_dir()
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
VENDOR_DIR = _VENDOR


# ---------------------------------------------------------------- 配置

# 引擎参数(各种 Key、接口地址、模型名)由 engines.py 的注册表自动汇总,
# 避免这里和引擎定义两边不一致。
_ENGINE_DEFAULTS = {}
for _spec in engines.all_engines():
    for _p in _spec.params:
        _ENGINE_DEFAULTS[_p.key] = _p.default

DEFAULT_CONFIG = {
    "engine": "mymemory",          # 默认用国内可直连的免费引擎
    "target_lang": "zh-CN",        # 翻译目标语言
    "target_name": "中文(简体)",
    "source_lang": "auto",         # 源语言:auto 自动检测,短句可手动指定更准
    "ocr_lang": "auto",            # auto | en | zh | ja | ko | ...
    "ocr_engine": "auto",          # auto | rapidocr | winsdk | tesseract | baidu
    "topmost": True,               # 主窗口置顶
    "auto_copy": False,            # 翻译完成后自动复制译文
    "opacity": 0.97,               # 主窗口不透明度
    "hotkey": "ctrl+shift+z",      # 全局快捷键(截图翻译),留空禁用
    "hotkey_realtime": "ctrl+shift+x",    # 全局快捷键(开始/停止实时翻译)
    "hotkey_clickthrough": "ctrl+shift+c",  # 全局快捷键(开关鼠标穿透)
    "history_limit": 30,           # 历史记录条数

    # ---- 实时翻译 ----
    "rt_region": None,             # 上次框选的范围 [x1,y1,x2,y2]
    "rt_interval": 0.7,            # 轮询间隔(秒),越小越实时、越吃 CPU
    "rt_overlay_x": None,          # 译文悬浮窗位置(退出时记住)
    "rt_overlay_y": None,
    "rt_overlay_w": 660,           # 译文悬浮窗宽度(太窄会把按钮挤掉)
    "rt_bg": "#0f1318",            # 悬浮窗背景色
    "rt_fg": "#ffffff",            # 译文字体颜色
    "rt_font_size": 20,            # 译文字号
    "rt_font": "Microsoft YaHei UI",
    "rt_opacity": 0.88,            # 悬浮窗不透明度
    "rt_show_source": False,       # 是否同时显示原文
    "rt_click_through": False,     # 鼠标穿透(不挡操作)
    "rt_topmost": True,
    "rt_dedupe": 0.90,             # 文本相似度超过该值就不重复翻译
    "rt_merge_lines": True,        # 折行合并后再翻译
    "rt_show_region": True,        # 在屏幕上画出监视范围框(看得见在翻哪一块)
    "rt_region_color": "#2f6fed",  # 范围框颜色
    "rt_context": True,            # 联系上下文翻译(只对大模型引擎生效)
    "rt_context_lines": 4,         # 上下文最多带回前面几行
    "rt_context_chars": 500,       # 上下文总字数上限(真正管住成本的是这个)
    "rt_settle_ms": 900,           # 文本稳定多久后做上下文重译
    "rt_stream": True,             # 大模型引擎流式输出(边生成边显示)
    "rt_auto_start": False,        # 记住范围后,下次直接开始
}
DEFAULT_CONFIG.update(_ENGINE_DEFAULTS)

_SECRET_SUFFIXES = ("_key", "_secret", "_apikey", "_token")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        # utf-8-sig:用记事本另存为「UTF-8」时会带 BOM,直接按 utf-8 读会解析失败,
        # 那样用户手改过的配置就会被悄悄丢掉、退回默认值。
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("读取配置失败(将使用默认配置): %s", e)
    return cfg


def save_config(cfg):
    """保存配置。Key 类字段单独提示:文件是明文,请注意保管。"""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def has_secret(cfg):
    """配置里是否存有任意 API Key(用于界面提示)。"""
    return any(str(cfg.get(k) or "").strip()
               for k in DEFAULT_CONFIG if k.endswith(_SECRET_SUFFIXES))


def open_url(url):
    """在系统默认浏览器里打开网址(获取 Key 的入口)。"""
    if not url:
        return False
    try:
        webbrowser.open(url)
        return True
    except Exception as e:
        log.warning("打开网址失败 %s: %s", url, e)
        return False


# ---------------------------------------------------------------- 截图

def enable_dpi_awareness():
    """让进程按物理像素工作,避免高分屏下截图坐标错位。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def virtual_screen_rect():
    """整个虚拟桌面(所有显示器)的矩形,返回 (x, y, w, h)。"""
    u = ctypes.windll.user32
    return (u.GetSystemMetrics(76),   # SM_XVIRTUALSCREEN
            u.GetSystemMetrics(77),   # SM_YVIRTUALSCREEN
            u.GetSystemMetrics(78),   # SM_CXVIRTUALSCREEN
            u.GetSystemMetrics(79))   # SM_CYVIRTUALSCREEN


def normalize_rect(rect):
    """把任意两点矩形整理成 (x1, y1, x2, y2) 且 x1<x2、y1<y2。"""
    x1, y1, x2, y2 = rect
    if x1 > x2:
        x1, x2 = x2, x1
    if y1 > y2:
        y1, y2 = y2, y1
    return int(x1), int(y1), int(x2), int(y2)


def capture_region(rect):
    """截取屏幕矩形 (x1, y1, x2, y2),返回 PIL Image。"""
    x1, y1, x2, y2 = normalize_rect(rect)
    if x2 - x1 < 1 or y2 - y1 < 1:
        raise ValueError("截图区域太小")
    return ImageGrab.grab(bbox=(x1, y1, x2, y2), all_screens=True)


def rect_intersects(a, b):
    """两个矩形是否相交 —— 实时翻译用它判断悬浮窗有没有盖住采集区。"""
    if not a or not b:
        return False
    ax1, ay1, ax2, ay2 = normalize_rect(a)
    bx1, by1, bx2, by2 = normalize_rect(b)
    return not (ax2 <= bx1 or bx2 <= ax1 or ay2 <= by1 or by2 <= ay1)


# ---------------------------------------------------------------- 帧 / 文本去重

def frame_signature(img, size=24):
    """把一帧画面压成很小的灰度缩略图,用于快速判断画面有没有变化。"""
    try:
        small = img.convert("L").resize((size, size))
        return small.tobytes()
    except Exception:
        return None


def frame_changed(sig_a, sig_b, threshold=2.0):
    """比较两帧签名。threshold 是平均像素差,越大越不敏感。"""
    if sig_a is None or sig_b is None:
        return True
    if len(sig_a) != len(sig_b):
        return True
    total = 0
    for a, b in zip(sig_a, sig_b):
        total += a - b if a > b else b - a
    return (total / len(sig_a)) >= threshold


def text_similar(a, b):
    """两段文本的相似度 0~1(用于避免同一句话被反复翻译)。"""
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # 先算便宜的快速上界,明显不相似就不必做完整比对
    quick = difflib.SequenceMatcher(None, a, b).real_quick_ratio()
    if quick < 0.5:
        return quick
    return difflib.SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------- OCR

class OcrError(Exception):
    pass


# OCR 引擎注册表:界面据此决定要不要向用户索要 Key、显示哪个申请网址
OCR_SPECS = {
    "auto": {
        "name": "自动(推荐)",
        "desc": "优先用系统 OCR,失败自动降级到 RapidOCR。",
        "needs_key": False, "key_url": "", "key_help": "", "params": [],
    },
    "rapidocr": {
        "name": "RapidOCR(本地·离线)",
        "desc": "内置 PP-OCR 模型,离线可用,中英混排效果好;实时翻译推荐。",
        "needs_key": False, "key_url": "", "key_help": "", "params": [],
    },
    "winsdk": {
        "name": "Windows 系统 OCR(本地)",
        "desc": "Win10/11 自带引擎,最快;需要 Python ≤3.12 且系统装了对应语言包。",
        "needs_key": False, "key_url": "", "key_help": "", "params": [],
    },
    "tesseract": {
        "name": "Tesseract(本地)",
        "desc": "需要另外安装 Tesseract-OCR 程序。",
        "needs_key": False, "key_url": "https://github.com/UB-Mannheim/tesseract/wiki",
        "key_help": "下载安装后,把安装目录加入 PATH 即可。", "params": [],
    },
    "baidu": {
        "name": "百度在线 OCR",
        "desc": "云端识别,准确率高、速度快;需要联网并配置 Key。",
        "needs_key": True,
        "key_url": "https://console.bce.baidu.com/ai-engine/ocr/overview/index",
        "key_help": "在百度智能云开通「文字识别」→ 创建应用 → 拿到 API Key 与 Secret Key 填到下面。",
        "params": [ParamSpec("baidu_ocr_apikey", "API Key", secret=True,
                             required=True, placeholder="百度智能云应用的 API Key",
                             url="https://console.bce.baidu.com/ai-engine/ocr/overview/index"),
                   ParamSpec("baidu_ocr_secret", "Secret Key", secret=True,
                             required=True, placeholder="百度智能云应用的 Secret Key",
                             url="https://console.bce.baidu.com/ai-engine/ocr/overview/index")],
    },
}

OCR_ENGINE_ORDER = ["auto", "rapidocr", "winsdk", "tesseract", "baidu"]


def ocr_engines():
    """按展示顺序返回 [(id, 描述 dict), ...]。"""
    return [(k, OCR_SPECS[k]) for k in OCR_ENGINE_ORDER if k in OCR_SPECS]


def ocr_engine_name(engine_id):
    return OCR_SPECS.get(engine_id, {}).get("name", engine_id or "自动")


def ocr_missing_params(engine_id, cfg):
    spec = OCR_SPECS.get(engine_id) or {}
    return [p for p in spec.get("params", [])
            if p.required and not str(cfg.get(p.key, "") or "").strip()]


def _png_bytes(img):
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def _pick_ocr_lang(available, pref):
    """从系统已安装的 OCR 语言中选择一个。auto 时按 英文→中文→日文→韩文 优先。"""
    if not available:
        return None
    if pref in (None, "", "auto"):
        order = ["en-US", "en", "zh-Hans-CN", "zh-Hans", "zh-CN",
                 "ja-JP", "ja", "ko-KR", "ko"]
        for tag in order:
            if tag in available:
                return tag
        for tag in available:
            if tag.lower().startswith(("zh", "ja", "ko")):
                return tag
        return available[0]
    if pref in available:
        return pref
    for tag in available:
        if tag.lower().startswith(pref.lower()):
            return tag
    return None


def _list_ocr_languages():
    from winsdk.windows.media.ocr import OcrEngine
    return [lang.language_tag for lang in OcrEngine.available_recognizer_languages]


async def _winsdk_recognize(img, lang_tag):
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.globalization import Language
    from winsdk.windows.storage.streams import InMemoryRandomAccessStream, DataWriter
    from winsdk.windows.graphics.imaging import (
        BitmapDecoder, BitmapPixelFormat, BitmapAlphaMode)

    engine = OcrEngine.try_create_from_language(Language(lang_tag))
    if engine is None:
        raise OcrError(f"无法创建 OCR 引擎({lang_tag})")

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(_png_bytes(img))
    await writer.store_async()
    stream.seek(0)

    decoder = await BitmapDecoder.create_async(stream)
    try:
        bitmap = await decoder.get_software_bitmap_async(
            BitmapPixelFormat.BGRA8, BitmapAlphaMode.PREMULTIPLIED)
    except TypeError:
        bitmap = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bitmap)
    return result.text


def winsdk_available():
    """当前 Python 环境能否使用 Windows 系统 OCR。"""
    try:
        import winsdk  # noqa: F401
    except Exception:
        return False
    try:
        return bool(_list_ocr_languages())
    except Exception:
        return False


_rapid_engine = None
_rapid_lock = threading.Lock()


def warmup_rapidocr():
    """预热 RapidOCR(首次加载模型较慢,放到启动时后台做)。"""
    try:
        from PIL import Image
        _rapid_ocr_smart(Image.new("RGB", (32, 32), "white"))
        log.info("RapidOCR 预热完成")
        return True
    except Exception as e:
        log.warning("RapidOCR 预热失败: %s", e)
        return False


# 兼容旧名字
warmup_ocr = warmup_rapidocr


def _rapid_ocr(img):
    """RapidOCR(PP-OCR 模型,离线,中英混合识别)。"""
    global _rapid_engine
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR
    with _rapid_lock:
        if _rapid_engine is None:
            _rapid_engine = RapidOCR()
    result, _ = _rapid_engine(np.asarray(img.convert("RGB")))
    if not result:
        return ""
    return "\n".join(line[1] for line in result)


# RapidOCR 内部写死了:图片宽高比 > 8 就**跳过文字检测**,把整张图当成
# 一行文字直接丢给识别模型。框一块又扁又宽的区域(字幕条、Steam 新闻条)
# 正好踩中 —— 明明有好几行字,却被当成一行,结果啥也识别不出来。
# 下面用「行投影」自己把它切成一行一行,再逐行识别。
_RAPID_MAX_RATIO = 8.0

# 文字行太扁时,上下补一点背景,把宽高比压下来。
#
# 识别模型会把图缩放到高 48 再认字。行裁剪出来的图往往很扁(宽高比能到 40+),
# 模型就得把它拉成超宽的输入,实测会把**词间空格丢掉**
# (`The old man` 变成 `Theoldman`),翻译质量跟着受影响。
# 补一点背景能保住空格。但补过头字就太小了(补到 48 以上开始认错字),
# 所以只动很扁的行,而且最多补到行高的两倍。
_ROW_PAD_TRIGGER = 24      # 行高小于这个值才补
_ROW_PAD_TARGET = 32       # 补到这个高度


def _pad_row(img):
    """行太扁就上下补点背景。背景色取图像上下边缘的中位色,免得补出突兀的色块。"""
    w, h = img.size
    if h >= _ROW_PAD_TRIGGER:
        return img
    target = int(min(_ROW_PAD_TARGET, h * 2))
    if h >= target:
        return img
    try:
        import numpy as np
        a = np.asarray(img.convert("RGB"))
        edge = np.concatenate([a[0:1].reshape(-1, 3), a[-1:].reshape(-1, 3)])
        bg = tuple(int(v) for v in np.median(edge, axis=0))
    except Exception:
        bg = (0, 0, 0)
    try:
        from PIL import Image
        canvas = Image.new("RGB", (w, target), bg)
        canvas.paste(img, (0, (target - h) // 2))
        return canvas
    except Exception:
        return img


def _text_rows(img, min_h=6, pad=2):
    """按横向投影把图切成若干「文字行」,返回 PIL 图列表。

    用「与背景中位色的偏差」来判断哪几行有内容,所以深底浅字、浅底深字都适用。
    """
    try:
        import numpy as np
    except ImportError:
        return []
    try:
        g = np.asarray(img.convert("L"), dtype=np.float32)
    except Exception:
        return []
    if g.ndim != 2 or g.shape[0] < 2:
        return []
    bg = float(np.median(g))
    ink = np.abs(g - bg).mean(axis=1)          # 每一行的"内容量"
    thr = max(float(ink.mean()) * 0.55, 1.5)   # 相对阈值,明暗底都适用
    on = ink > thr

    bands, start = [], None
    for i, v in enumerate(on):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start >= min_h:
                bands.append([start, i])
            start = None
    if start is not None and len(on) - start >= min_h:
        bands.append([start, len(on)])
    if not bands:
        return []

    # 挨得很近的条合并(同一行常被切碎)
    merged = []
    for b in bands:
        if merged and b[0] - merged[-1][1] <= 2:
            merged[-1][1] = b[1]
        else:
            merged.append(b)

    w, h = img.size
    return [img.crop((0, max(0, a - pad), w, min(h, b + pad))) for a, b in merged]


def _rapid_ocr_smart(img):
    """RapidOCR 的智能入口:宽条先按行切开再识别,绕开上面那个坑。

    实测:一条 1200x70 的单行字幕,整张丢进去识别结果为空;把文字那一行
    单独裁出来再识别就完全正确 —— 因为裁掉上下留白后,识别模型看到的字
    大了好几倍。所以只要是宽条,不管切出一行还是多行,都逐行识别。
    """
    w, h = img.size
    ratio = (w / h) if h else 0.0
    rows = None

    def _rows_text(rs):
        return "\n".join(t for t in (_rapid_ocr(_pad_row(r)) for r in rs)
                         if t.strip())

    if ratio > _RAPID_MAX_RATIO:
        rows = _text_rows(img)
        if rows:
            text = _rows_text(rows)
            if text.strip():
                return text

    text = _rapid_ocr(img)
    if text.strip():
        return text

    # 兜底:整张识别不出来,再按行切开试一次(宽高比正常时也值得一试)
    if rows is None:
        rows = _text_rows(img)
    if rows:
        return _rows_text(rows)
    return ""


def _tesseract_ocr(img, lang):
    import pytesseract
    tlang = {"zh": "chi_sim", "en": "eng", "ja": "jpn", "ko": "kor"}.get(
        lang if lang != "auto" else "en", "eng")
    return pytesseract.image_to_string(img.convert("RGB"), lang=tlang)


def _baidu_ocr(img, lang, cfg):
    key = str(cfg.get("baidu_ocr_apikey", "") or "").strip()
    secret = str(cfg.get("baidu_ocr_secret", "") or "").strip()
    if not key or not secret:
        raise MissingKeyError("baidu", None,
                              OCR_SPECS["baidu"]["params"])
    return engines.ocr_baidu(_png_bytes(img), key, secret, lang)


def ocr_image_ex(img, lang="auto", engine="auto", cfg=None):
    """对 PIL 图片做 OCR,返回 (文本, 实际使用的引擎名)。"""
    cfg = cfg or {}
    engine = engine or "auto"
    # 「自动」= 系统 OCR → RapidOCR → Tesseract 依次尝试
    order = ["winsdk", "rapidocr", "tesseract"] if engine == "auto" else [engine]

    errors = []
    for eng in order:
        try:
            if eng == "winsdk":
                available = _list_ocr_languages()
                tag = _pick_ocr_lang(available, lang)
                if tag is None:
                    raise OcrError("系统没有可用的 OCR 语言包")
                text = asyncio.run(_winsdk_recognize(img, tag))
                if text.strip():
                    return text.strip(), "Windows OCR"
                errors.append("Windows OCR: 没识别到文字")
            elif eng == "rapidocr":
                text = _rapid_ocr_smart(img)
                if text.strip():
                    return text.strip(), "RapidOCR"
                errors.append("RapidOCR: 没识别到文字")
            elif eng == "tesseract":
                text = _tesseract_ocr(img, lang)
                if text.strip():
                    return text.strip(), "Tesseract"
                errors.append("Tesseract: 没识别到文字")
            elif eng == "baidu":
                text = _baidu_ocr(img, lang, cfg)
                if text.strip():
                    return text.strip(), "百度在线 OCR"
                errors.append("百度在线 OCR: 没识别到文字")
            else:
                errors.append(f"未知 OCR 引擎: {eng}")
        except MissingKeyError:
            raise
        except ImportError as e:
            errors.append(f"{eng}: 未安装({e})")
        except Exception as e:
            errors.append(f"{eng}: {e}")
            log.warning("OCR(%s) 失败: %s", eng, e)

    # 「自动」模式下所有本地引擎都失败时,若配了百度 Key 就再试一次在线识别
    if engine == "auto" and not ocr_missing_params("baidu", cfg):
        try:
            text = _baidu_ocr(img, lang, cfg)
            if text.strip():
                return text.strip(), "百度在线 OCR"
        except Exception as e:
            errors.append(f"baidu: {e}")

    raise OcrError("未识别到文字。" + ("; ".join(errors[-3:]) if errors else ""))


def ocr_image(img, lang="auto", engine="auto", cfg=None):
    """对 PIL 图片做 OCR,返回文本(兼容旧调用方式)。"""
    return ocr_image_ex(img, lang, engine, cfg)[0]


def make_test_image(text="Hello World 2025", size=(700, 140)):
    """生成一张白底黑字的测试图,用于自检和设置里的「测试识别」。"""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    font = None
    for path in (r"C:\Windows\Fonts\arial.ttf",
                 r"C:\Windows\Fonts\segoeui.ttf",
                 r"C:\Windows\Fonts\msyh.ttc"):
        try:
            font = ImageFont.truetype(path, 56)
            break
        except Exception:
            continue
    d.text((24, 30), text, fill="black", font=font)
    return img


# ---------------------------------------------------------------- 文本处理

_SENTENCE_END = set(".!?。！？…;；:：)")


def merge_ocr_lines(text):
    """把 OCR 的硬换行合并成完整句子。

    屏幕上的句子常被折成多行,OCR 会原样保留换行,导致逐行翻译、上下文丢失。
    规则:上一行不是以句末标点结尾时,与下一行拼接(拉丁字母之间补空格,
    中日韩文字之间直接拼接);句末标点结尾的行保留为真正的换行。
    """
    if not text:
        return text
    lines = [ln.strip() for ln in
             text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    out, buf = [], ""
    for ln in lines:
        if not ln:
            if buf:
                out.append(buf)
                buf = ""
            continue
        if not buf:
            buf = ln
            continue
        if buf[-1] not in _SENTENCE_END:
            if buf.endswith("-") or buf.endswith("—"):
                buf = buf[:-1] + ln
            elif buf[-1].isascii() and buf[-1].isalnum() and \
                    ln[0].isascii() and ln[0].isalnum():
                buf = f"{buf} {ln}"
            else:
                buf += ln
        else:
            out.append(buf)
            buf = ln
    if buf:
        out.append(buf)
    return "\n".join(out)


# ---------------------------------------------------------------- 连通性探测

def probe_engines(cfg, engine_ids=None):
    """后台探测哪些引擎可用,返回 {engine_id: bool}。"""
    ids = engine_ids or [s.id for s in engines.all_engines()]
    result = {}
    for eid in ids:
        spec = engines.get_engine(eid)
        if spec is None or engines.missing_params(eid, cfg):
            continue
        try:
            result[eid] = bool(spec.runner("hello", cfg, "zh-CN").strip())
        except Exception as e:
            log.info("引擎 %s 探测失败: %s", eid, e)
            result[eid] = False
    return result
