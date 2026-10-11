# -*- coding: utf-8 -*-
"""翻译引擎注册表与实现。

每个引擎用一份 EngineSpec 描述自己:叫什么、是否免费、要不要 API Key、
Key 去哪里申请、需要用户填哪些参数。界面完全由这份描述驱动,所以:

  * 选了「需 Key」的引擎,设置界面会自动弹出 Key 输入框,并在输入框**下方**
    显示可点击的「获取 Key」网址入口(EngineSpec.key_url);
  * 缺 Key 时 translate() 会抛 MissingKeyError,由界面负责向用户索要;
  * 以后想加引擎,只要在 ENGINES 里加一条 + 写一个 runner 函数,界面自动适配。

本模块不依赖 core.py,可以单独 import 使用。
"""
import logging
import time

import requests

log = logging.getLogger("fanyi")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class TranslateError(Exception):
    """翻译失败(网络、限额、返回格式异常等)。"""


class AlreadyTargetError(TranslateError):
    """源文本本来就是目标语言,没什么可翻的。不算错误,直接原样显示。"""


class MissingKeyError(TranslateError):
    """该引擎需要 API Key,但用户还没填。界面据此向用户索要 Key。"""

    def __init__(self, engine_id, spec, missing):
        self.engine_id = engine_id
        self.spec = spec
        self.missing = missing          # 缺哪个参数(ParamSpec)
        name = getattr(spec, "name", None) or engine_id or "该引擎"
        labels = "、".join(getattr(p, "label", str(p)) for p in missing)
        super().__init__(f"「{name}」需要先填写:{labels}")


# ---------------------------------------------------------------- 描述结构

class ParamSpec:
    """引擎需要的一个配置项(多数是 Key,也可能是接口地址/模型名)。"""

    __slots__ = ("key", "label", "secret", "default", "required",
                 "placeholder", "help", "url", "choices")

    def __init__(self, key, label, secret=False, default="", required=False,
                 placeholder="", help="", url="", choices=None):
        self.key = key                # config.json 中的键名
        self.label = label            # 界面显示名
        self.secret = secret          # 是否密码式输入
        self.default = default
        self.required = required
        self.placeholder = placeholder
        self.help = help
        self.url = url                # 该参数单独的申请/说明网址
        self.choices = choices or []  # 非空则渲染成下拉框


class EngineSpec:
    """一个翻译引擎的完整描述。"""

    __slots__ = ("id", "name", "free", "needs_key", "key_url", "key_help",
                 "note", "params", "chunk", "runner",
                 "supports_context", "supports_stream")

    def __init__(self, id, name, free, needs_key, runner, key_url="",
                 key_help="", note="", params=(), chunk=3500,
                 supports_context=False, supports_stream=False):
        self.id = id
        self.name = name
        self.free = free
        self.needs_key = needs_key
        self.key_url = key_url
        self.key_help = key_help
        self.note = note
        self.params = tuple(params)
        self.chunk = chunk
        self.runner = runner
        # 只有大模型引擎能读懂"前文",也只有它能边生成边吐字
        self.supports_context = supports_context
        self.supports_stream = supports_stream

    @property
    def badge(self):
        return "免费" if self.free else "需 Key"

    def label(self):
        return f"{self.name}  [{self.badge}]"


# ---------------------------------------------------------------- 语言

TARGET_LANGS = [
    ("中文(简体)", "zh-CN"),
    ("中文(繁体)", "zh-TW"),
    ("English", "en"),
    ("日本語", "ja"),
    ("한국어", "ko"),
    ("Français", "fr"),
    ("Deutsch", "de"),
    ("Español", "es"),
    ("Русский", "ru"),
]

_LANG_NAME = {code: name for name, code in TARGET_LANGS}

# 各引擎对语言代码的写法不一样,这里做一层映射
_DEEPL_LANG = {"zh-CN": "ZH", "zh-TW": "ZH-HANT", "en": "EN-US", "ja": "JA",
               "ko": "KO", "fr": "FR", "de": "DE", "es": "ES", "ru": "RU"}
_LIBRE_LANG = {"zh-CN": "zh", "zh-TW": "zt", "en": "en", "ja": "ja",
               "ko": "ko", "fr": "fr", "de": "de", "es": "es", "ru": "ru"}
_BAIDU_LANG = {"zh-CN": "zh", "zh-TW": "cht", "en": "en", "ja": "jp",
               "ko": "kor", "fr": "fra", "de": "de", "es": "spa",
               "ru": "ru"}


def target_name(code):
    return _LANG_NAME.get(code, code)


# ---------------------------------------------------------------- 通用工具

def _chunk_text(text, size=3500):
    """把长文本按段落切成不超过 size 的块,供接口分段翻译。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if len(text) <= size:
        return [text]
    chunks, buf = [], ""
    for para in text.split("\n"):
        if len(para) > size:
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(para), size):
                chunks.append(para[i:i + size])
            continue
        if len(buf) + len(para) + 1 > size:
            chunks.append(buf)
            buf = para
        else:
            buf = f"{buf}\n{para}" if buf else para
    if buf:
        chunks.append(buf)
    return chunks


def _run_chunks(text, fn, size):
    return "".join(fn(chunk) for chunk in _chunk_text(text, size))


def _get(cfg, key, default=""):
    v = cfg.get(key, default)
    return v if v is not None else default


# ---------------------------------------------------------------- 源语言

SOURCE_LANGS = [
    ("自动检测", "auto"),
    ("English", "en"),
    ("中文(简体)", "zh-CN"),
    ("中文(繁体)", "zh-TW"),
    ("日本語", "ja"),
    ("한국어", "ko"),
    ("Français", "fr"),
    ("Deutsch", "de"),
    ("Español", "es"),
    ("Русский", "ru"),
]


def _source_lang(cfg):
    src = str(_get(cfg, "source_lang", "auto") or "auto").strip()
    return src or "auto"


def _char_script(ch):
    """把一个字符归到某种书写系统。"""
    o = ord(ch)
    if 0x3040 <= o <= 0x30FF:                 # 平假名 / 片假名
        return "kana"
    if 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF:   # 谚文
        return "hangul"
    if (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF
            or 0xF900 <= o <= 0xFAFF):        # 汉字
        return "han"
    if 0x0400 <= o <= 0x04FF:                 # 西里尔字母
        return "cyrillic"
    if ch.isalpha():                          # 拉丁字母等
        return "latin"
    return None                               # 空格、数字、标点都不算


def script_stats(text):
    """统计各书写系统的字符占比(忽略空格/数字/标点)。

    关键点:判定"这是什么语言"要看**哪种字符占多数**,而不是"出现过没有"。
    否则「你好 world this is English」会因为开头两个字是中文,被整段当成中文。
    """
    counts = {}
    total = 0
    for ch in text or "":
        s = _char_script(ch)
        if s is None:
            continue
        counts[s] = counts.get(s, 0) + 1
        total += 1
    if not total:
        return {}, 0
    return {k: v / total for k, v in counts.items()}, total


# 认定"这就是目标语言"的最低占比:必须过半,个别外来词不影响判断
_DOMINANT = 0.5
# 日文里假名占比不用过半,但至少要有几个,才不会把中文当成日文
_KANA_MIN = 0.15


def detect_lang(text):
    """粗略猜一下文本主要是什么语言,返回 (语言代码 或 None, 该脚本占比)。"""
    r, total = script_stats(text)
    if not total:
        return None, 0.0
    if r.get("kana", 0) >= _KANA_MIN:
        return "ja", r["kana"]
    if r.get("hangul", 0) >= _DOMINANT:
        return "ko", r["hangul"]
    if r.get("han", 0) >= _DOMINANT:
        return "zh", r["han"]
    if r.get("cyrillic", 0) >= _DOMINANT:
        return "ru", r["cyrillic"]
    if r.get("latin", 0) >= _DOMINANT:
        return "en", r["latin"]
    return None, max(r.values()) if r else 0.0


# 兼容旧名字
def _has_cjk(s):
    return script_stats(s)[0].get("han", 0) > 0


def _same_text(a, b):
    return (a or "").strip().casefold() == (b or "").strip().casefold()


def _guess_sources(text, target, current):
    """autodetect 失败时,按字符构成猜几个可能的源语言来重试。"""
    cands = []
    guess, _ratio = detect_lang(text)
    if guess == "zh":
        # 全是汉字时中文和日文分不清,两个都试
        cands += ["zh-CN", "zh-TW", "ja"]
    elif guess == "ja":
        cands.append("ja")
    elif guess == "ko":
        cands.append("ko")
    elif guess == "ru":
        cands.append("ru")
    cands.append("en")                 # 最常见的兜底
    out = []
    for c in cands:
        if c != current and c != target and c not in out:
            out.append(c)
    return out


def already_in_target(text, target, cfg=None):
    """文本看起来已经是目标语言了吗?

    中文用户看中文字幕是很常见的情况。这时如果把中文再送去"翻译成中文",
    接口会直接报「源语言和目标语言相同」。所以先自己判断一下,是的话就不翻。

    判断依据是**字符占比**:某种文字必须占到一半以上才算"就是这门语言"。
    所以「你好 world this is English」不会被误判成中文,照样会送去翻译。
    用户显式指定了源语言时不作判断,交给引擎处理(例如简体→繁体转换)。
    """
    src = _source_lang(cfg) if cfg else "auto"
    if src not in ("", "auto"):
        return False
    s = (text or "").strip()
    if not s:
        return False
    guess, ratio = detect_lang(s)
    if guess is None:
        return False
    t = (target or "").lower()
    if t.startswith("zh"):
        return guess == "zh"
    if t.startswith("ja"):
        return guess == "ja"
    if t.startswith("ko"):
        return guess == "ko"
    if t.startswith("ru"):
        return guess == "ru"
    # 拉丁字母语言之间靠这几行代码分不出来,交给引擎自己判断
    return False


def _with_echo_retry(text, target, cfg, call):
    """翻译,并在「接口把原文原样返回」时换源语言重试一次。

    第三方免费接口在「源语言自动检测」下遇到短句(字幕、按钮文字)时,
    经常直接把原文原样返回 —— 看起来像"翻译没生效"。这里检测到译文和原文
    完全相同,就换一个明确的源语言再试一次。
    """
    src = _source_lang(cfg)
    out = call(src)
    if not _same_text(out, text):
        return out
    for guess in _guess_sources(text, target, src):
        try:
            out2 = call(guess)
        except AlreadyTargetError:
            raise
        except Exception:
            continue
        if not _same_text(out2, text):
            return out2
    return out


# ---------------------------------------------------------------- 各引擎实现

def run_google(text, cfg, target, timeout=8):
    """Google 免费网页接口,免 Key,质量好(国内需代理)。"""
    url = "https://translate.googleapis.com/translate_a/single"

    def call(src):
        params = {"client": "gtx", "sl": src or "auto", "tl": target,
                  "dt": "t", "q": text}
        r = requests.get(url, params=params,
                         headers={"User-Agent": USER_AGENT}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        try:
            out = "".join(seg[0] for seg in data[0] if seg and seg[0])
        except (IndexError, TypeError):
            raise TranslateError(f"Google 返回格式异常: {str(data)[:200]}")
        if not out.strip():
            raise TranslateError("Google 返回空译文")
        return out

    return _with_echo_retry(text, target, cfg, call)


_MYMEMORY_ERROR_MARKERS = (
    "MYMEMORY WARNING", "QUERY LENGTH LIMIT", "NO QUERY SPECIFIED",
    "PLEASE SELECT TWO DISTINCT LANGUAGES", "INVALID EMAIL",
    "INVALID SOURCE LANGUAGE", "INVALID TARGET LANGUAGE",
)


def run_mymemory(text, cfg, target, timeout=12):
    """MyMemory 免费接口。匿名单次限 500 字符,填邮箱可提额。"""
    url = "https://api.mymemory.translated.net/get"
    email = (_get(cfg, "mymemory_email") or "").strip()

    def call(src):
        # MyMemory 的自动检测写死是 "autodetect",不认 "auto"
        pair_src = "autodetect" if src in ("", "auto", "autodetect") else src
        params = {"q": text, "langpair": f"{pair_src}|{target}", "mt": "1"}
        if email:
            params["de"] = email
        r = requests.get(url, params=params,
                         headers={"User-Agent": USER_AGENT}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        out = (data.get("responseData") or {}).get("translatedText")
        if not out or not out.strip():
            raise TranslateError(
                f"MyMemory 翻译失败: {str(data.get('responseDetails', ''))[:200]}")
        upper = out.upper()
        if "PLEASE SELECT TWO DISTINCT LANGUAGES" in upper:
            raise AlreadyTargetError("源文本已经是目标语言")
        if any(m in upper for m in _MYMEMORY_ERROR_MARKERS):
            raise TranslateError(f"MyMemory 拒绝请求: {out.strip()[:160]}")
        return out

    return _with_echo_retry(text, target, cfg, call)


def run_libretranslate(text, cfg, target, timeout=15):
    """LibreTranslate(开源,可用公共实例或自建实例)。"""
    base = (_get(cfg, "libre_url", "https://libretranslate.com") or "").strip()
    base = base.rstrip("/")
    if not base:
        raise TranslateError("未填写 LibreTranslate 服务器地址")
    key = (_get(cfg, "libre_key") or "").strip()

    def call(src):
        payload = {"q": text, "source": src or "auto",
                   "target": _LIBRE_LANG.get(target, target), "format": "text"}
        if key:
            payload["api_key"] = key
        r = requests.post(base + "/translate", json=payload,
                          headers={"User-Agent": USER_AGENT}, timeout=timeout)
        if r.status_code in (401, 403):
            raise TranslateError("LibreTranslate 拒绝访问(该实例需要 API Key)")
        if r.status_code == 429:
            raise TranslateError("LibreTranslate 免费额度已用完,请稍后再试")
        r.raise_for_status()
        try:
            out = r.json().get("translatedText", "")
        except ValueError:
            raise TranslateError(f"LibreTranslate 返回格式异常: {r.text[:200]}")
        if not out.strip():
            raise TranslateError("LibreTranslate 返回空译文")
        return out

    # LibreTranslate 用 ISO 639-1,猜测的源语言要映射过去
    def norm(src):
        return "auto" if src in ("", "auto") else _LIBRE_LANG.get(src, src)

    out = call(norm(_source_lang(cfg)))
    if not _same_text(out, text):
        return out
    for guess in _guess_sources(text, target, _source_lang(cfg)):
        try:
            out2 = call(norm(guess))
        except Exception:
            continue
        if not _same_text(out2, text):
            return out2
    return out


def run_deepl(text, cfg, target, timeout=20):
    """DeepL 官方 API(需 Key,免费版每月 50 万字符)。"""
    key = (_get(cfg, "deepl_key") or "").strip()
    if not key:
        raise MissingKeyError("deepl", ENGINES["deepl"],
                              [p for p in ENGINES["deepl"].params if p.required])
    # 免费版 Key 以 :fx 结尾,走 api-free;付费版走 api
    host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
    r = requests.post(f"https://{host}/v2/translate",
                      data={"auth_key": key, "text": text,
                            "target_lang": _DEEPL_LANG.get(target, target.upper())},
                      headers={"User-Agent": USER_AGENT}, timeout=timeout)
    if r.status_code in (401, 403):
        raise TranslateError("DeepL Key 无效或无权限,请检查是否复制完整")
    if r.status_code == 456:
        raise TranslateError("DeepL 本月免费额度已用完")
    r.raise_for_status()
    out = "".join(t.get("text", "") for t in r.json().get("translations", []))
    if not out.strip():
        raise TranslateError("DeepL 返回空译文")
    return out


def _chat_messages(text, target, context=None):
    """拼一段给大模型的消息。带上下文时,前文只用来理解,不翻译。"""
    if context:
        system = (f"你是字幕/对话翻译。把【待翻译】的内容翻译成"
                  f"{target_name(target)}。\n"
                  "【前文】是已经出现过的内容,只用来帮你判断指代、专有名词、"
                  "称呼和语气,不要翻译它,也不要复述它。\n"
                  "只输出【待翻译】的译文本身,不要解释、不要加引号、"
                  "不要加任何前缀或标注。")
        user = f"【前文】\n{context}\n\n【待翻译】\n{text}"
    else:
        system = (f"你是一名专业翻译。把用户提供的文本翻译成"
                  f"{target_name(target)},"
                  "忠实原意、保留换行与语气,只输出译文本身,"
                  "不要解释、不要加引号、不要加任何前缀。")
        user = text
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def _openai_compat(text, cfg, target, base_key, key_key, model_key,
                   default_base, default_model, timeout=60,
                   context=None, stream=False, on_delta=None):
    """OpenAI 兼容 /chat/completions。DeepSeek 与自定义接口共用。

    context   —— 前文(只作理解用,不翻译),让前后句的指代和术语对得上;
    stream    —— 边生成边回调 on_delta(累积到当前的文本),观感上"立刻出字";
    on_delta  —— 每收到一小段就调一次,传的是**累积后的完整译文**。
    """
    base = (_get(cfg, base_key, default_base) or "").strip().rstrip("/")
    key = (_get(cfg, key_key) or "").strip()
    model = (_get(cfg, model_key, default_model) or "").strip() or default_model
    if not base:
        raise TranslateError("未填写接口地址")
    if not key:
        raise TranslateError("未填写 API Key")
    if base.endswith("/v1"):
        url = base + "/chat/completions"
    elif "/chat/completions" in base:
        url = base
    else:
        url = base + "/v1/chat/completions"

    use_stream = bool(stream and on_delta)
    payload = {"model": model, "temperature": 0.2, "stream": use_stream,
               "messages": _chat_messages(text, target, context)}
    headers = {"Authorization": f"Bearer {key}",
               "Content-Type": "application/json",
               "User-Agent": USER_AGENT}

    if use_stream:
        return _openai_stream(url, payload, headers, timeout, on_delta)

    r = requests.post(url, json=payload, timeout=timeout, headers=headers)
    if r.status_code in (401, 403):
        raise TranslateError("API Key 无效或无权限,请检查后重新填写")
    if r.status_code == 404:
        raise TranslateError(f"接口地址或模型名不对(404): {url}")
    if r.status_code == 429:
        raise TranslateError("请求过于频繁或额度不足(429)")
    if r.status_code >= 400:
        raise TranslateError(f"接口返回 {r.status_code}: {r.text[:200]}")
    try:
        out = r.json()["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, ValueError):
        raise TranslateError(f"接口返回格式异常: {r.text[:200]}")
    if not out:
        raise TranslateError("接口返回空译文")
    return out


def _openai_stream(url, payload, headers, timeout, on_delta):
    """流式读 SSE:每来一小段就回调一次,让人感觉"立刻就翻出来了"。"""
    import json as _json
    acc = []
    try:
        with requests.post(url, json=payload, timeout=timeout, headers=headers,
                           stream=True) as r:
            if r.status_code in (401, 403):
                raise TranslateError("API Key 无效或无权限,请检查后重新填写")
            if r.status_code == 404:
                raise TranslateError(f"接口地址或模型名不对(404): {url}")
            if r.status_code == 429:
                raise TranslateError("请求过于频繁或额度不足(429)")
            if r.status_code >= 400:
                raise TranslateError(f"接口返回 {r.status_code}: "
                                     f"{r.text[:200]}")
            r.encoding = "utf-8"
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    chunk = _json.loads(line)
                except ValueError:
                    continue
                try:
                    delta = chunk["choices"][0].get("delta") or {}
                    piece = delta.get("content") or ""
                except (KeyError, IndexError, TypeError):
                    piece = ""
                if piece:
                    acc.append(piece)
                    try:
                        on_delta("".join(acc))
                    except Exception:
                        pass
    except TranslateError:
        raise
    except Exception as e:
        # 流式失败(有些兼容服务不支持):已经有内容就先用着,否则整体报错
        if not acc:
            raise TranslateError(f"流式请求失败: {type(e).__name__}: {e}")
    out = "".join(acc).strip()
    if not out:
        raise TranslateError("接口返回空译文")
    return out


def run_deepseek(text, cfg, target, timeout=60, context=None, stream=False,
                 on_delta=None):
    return _openai_compat(text, cfg, target, "deepseek_base", "deepseek_key",
                          "deepseek_model", "https://api.deepseek.com",
                          "deepseek-chat", timeout,
                          context=context, stream=stream, on_delta=on_delta)


def run_openai(text, cfg, target, timeout=60, context=None, stream=False,
               on_delta=None):
    return _openai_compat(text, cfg, target, "openai_base", "openai_key",
                          "openai_model", "https://api.openai.com",
                          "gpt-4o-mini", timeout,
                          context=context, stream=stream, on_delta=on_delta)


# ---------------------------------------------------------------- 注册表

ENGINES = {}

# 表示「原文已经是目标语言,原样显示」,不是真的引擎
NO_TRANSLATION = "none"


def _register(spec):
    ENGINES[spec.id] = spec
    return spec


_register(EngineSpec(
    id="google", name="Google 翻译", free=True, needs_key=False,
    runner=run_google, chunk=3500,
    note="免配置、质量好;但国内网络无法直连,需自备代理。",
))

_register(EngineSpec(
    id="mymemory", name="MyMemory", free=True, needs_key=False,
    runner=run_mymemory, chunk=450,
    key_url="https://mymemory.translated.net/doc/keygen.php",
    key_help="匿名可用(单次 500 字符);填邮箱可提高免费额度。",
    note="国内可直连,适合做兜底引擎。短句自动检测可能失败,"
         "可到「通用」里把源语言设成具体语言(如 English)。",
    params=[ParamSpec("mymemory_email", "邮箱(可选,提高额度)", secret=False,
                      placeholder="you@example.com",
                      help="仅用于向 MyMemory 标识身份,可留空。",
                      url="https://mymemory.translated.net/doc/keygen.php")],
))

_register(EngineSpec(
    id="libretranslate", name="LibreTranslate", free=True, needs_key=False,
    runner=run_libretranslate, chunk=3000,
    key_url="https://portal.libretranslate.com",
    key_help="公共实例 portal.libretranslate.com 可免费申请 Key;也可自建实例后免 Key 使用。",
    note="开源引擎;官方公共实例通常需要 Key,自建实例可留空。",
    params=[
        ParamSpec("libre_url", "服务器地址",
                  default="https://libretranslate.com",
                  placeholder="https://libretranslate.com",
                  help="自建实例示例:http://localhost:5000"),
        ParamSpec("libre_key", "API Key(可选)", secret=True,
                  placeholder="留空表示该实例不需要 Key",
                  url="https://portal.libretranslate.com"),
    ],
))

_register(EngineSpec(
    id="deepseek", name="DeepSeek", free=False, needs_key=True,
    runner=run_deepseek, chunk=3000,
    supports_context=True, supports_stream=True,
    key_url="https://platform.deepseek.com/api_keys",
    key_help="登录 DeepSeek 开放平台 → API Keys → 创建,复制以 sk- 开头的 Key。国内可直连,按量计费。",
    note="大模型翻译,译文自然、上下文好,适合整段文字。",
    params=[
        ParamSpec("deepseek_key", "DeepSeek API Key", secret=True,
                  required=True, placeholder="sk-...",
                  help="填好后可点「测试连接」验证。",
                  url="https://platform.deepseek.com/api_keys"),
        ParamSpec("deepseek_base", "接口地址",
                  default="https://api.deepseek.com",
                  help="一般不用改。"),
        ParamSpec("deepseek_model", "模型", default="deepseek-chat",
                  choices=["deepseek-chat", "deepseek-reasoner"]),
    ],
))

_register(EngineSpec(
    id="openai", name="OpenAI 兼容接口", free=False, needs_key=True,
    runner=run_openai, chunk=3000,
    supports_context=True, supports_stream=True,
    key_url="https://platform.openai.com/api-keys",
    key_help="OpenAI 官方在 platform.openai.com 申请;也可换成任意兼容服务(硅基流动 / Kimi / 通义 / 本地 Ollama 等),只改「接口地址」即可。",
    note="通用适配器:任何 OpenAI 兼容的 /v1/chat/completions 服务都能接。",
    params=[
        ParamSpec("openai_base", "接口地址", default="https://api.openai.com",
                  placeholder="https://api.openai.com",
                  help="填到 /v1 之前即可,例如 https://api.siliconflow.cn/v1"),
        ParamSpec("openai_key", "API Key", secret=True, required=True,
                  placeholder="sk-...",
                  help="本地服务(如 Ollama)不需要校验,随便填一个字符即可。",
                  url="https://platform.openai.com/api-keys"),
        ParamSpec("openai_model", "模型名", default="gpt-4o-mini",
                  placeholder="gpt-4o-mini",
                  help="例如 gpt-4o-mini / Qwen/Qwen2.5-7B-Instruct / llama3.1"),
    ],
))

_register(EngineSpec(
    id="deepl", name="DeepL", free=False, needs_key=True,
    runner=run_deepl, chunk=3500,
    key_url="https://www.deepl.com/pro-api",
    key_help="注册后选「DeepL API Free」,每月 50 万字符免费;Key 以 :fx 结尾的是免费版。",
    note="译文质量高;免费版 Key 每月 50 万字符。",
    params=[
        ParamSpec("deepl_key", "DeepL API Key", secret=True, required=True,
                  placeholder="xxxxxxxx-xxxx-...:fx",
                  url="https://www.deepl.com/pro-api"),
    ],
))

# 界面展示顺序:免费引擎在前,需 Key 的在后
ENGINE_ORDER = ["google", "mymemory", "libretranslate",
                "deepseek", "openai", "deepl"]


def all_engines():
    """按展示顺序返回全部引擎。"""
    return [ENGINES[i] for i in ENGINE_ORDER if i in ENGINES]


def get_engine(engine_id):
    return ENGINES.get(engine_id)


def engine_name(engine_id):
    if engine_id == NO_TRANSLATION:
        return "无需翻译"
    spec = ENGINES.get(engine_id)
    return spec.name if spec else (engine_id or "未知")


def missing_params(engine_id, cfg):
    """返回该引擎还缺哪些必填参数(空列表表示配置完整)。"""
    spec = ENGINES.get(engine_id)
    if not spec:
        return []
    return [p for p in spec.params
            if p.required and not str(cfg.get(p.key, "") or "").strip()]


def is_ready(engine_id, cfg):
    return not missing_params(engine_id, cfg)


# ---------------------------------------------------------------- 调用入口

# 引擎健康缓存:某引擎失败后冷却一段时间,期间直接跳过,避免每次都白等超时。
# 连续失败会指数级拉长冷却 —— 像国内连不上 Google 这种情况,第一次等一次
# 超时就够了,不该每两分钟又白等一遍。
_engine_down = {}
_engine_fails = {}
_engine_good = {}          # 本会话成功过的引擎:最近成功的排前面
_ENGINE_COOLDOWN = 120
_ENGINE_COOLDOWN_MAX = 900


def _engine_ok(engine_id):
    return time.time() >= _engine_down.get(engine_id, 0)


def mark_down(engine_id):
    n = _engine_fails.get(engine_id, 0) + 1
    _engine_fails[engine_id] = n
    cool = min(_ENGINE_COOLDOWN * (2 ** (n - 1)), _ENGINE_COOLDOWN_MAX)
    _engine_down[engine_id] = time.time() + cool
    log.info("引擎 %s 第 %d 次失败,冷却 %d 秒", engine_id, n, cool)


def mark_good(engine_id):
    _engine_fails.pop(engine_id, None)
    _engine_down.pop(engine_id, None)
    _engine_good[engine_id] = time.time()


def clear_down(engine_id=None):
    if engine_id is None:
        _engine_down.clear()
        _engine_fails.clear()
        _engine_good.clear()
    else:
        _engine_down.pop(engine_id, None)
        _engine_fails.pop(engine_id, None)


def _fallback_order(preferred, cfg):
    """首选引擎失败后的兜底顺序。

    本会话成功过的引擎排最前 —— 国内 Google 连不上、MyMemory 又常限额,
    这样第一次失败之后就能直奔真正能用的那个,不用每次重头撞一遍。
    """
    ready = [s.id for s in all_engines()
             if s.id != preferred and is_ready(s.id, cfg) and _engine_ok(s.id)]
    proven = sorted((e for e in ready if e in _engine_good),
                    key=lambda e: -_engine_good[e])
    fresh_free = [e for e in ready
                  if e not in _engine_good and ENGINES[e].free]
    fresh_paid = [e for e in ready
                  if e not in _engine_good and not ENGINES[e].free]
    return proven + fresh_free + fresh_paid


def _run_engine(s, text, cfg, target, context=None, on_delta=None):
    """按引擎的分块大小调用它;能读前文就带上,能流式就流式。"""
    chunks = _chunk_text(text, s.chunk)
    # 只有在「整段一次发完」时流式才有意义,分块时逐块回调会把译文搅乱
    use_stream = bool(on_delta) and s.supports_stream and len(chunks) == 1
    parts = []
    for ch in chunks:
        kw = {}
        if context and s.supports_context:
            kw["context"] = context
        if use_stream:
            kw["stream"] = True
            kw["on_delta"] = on_delta
        parts.append(s.runner(ch, cfg, target, **kw))
    return "".join(parts)


def translate(text, cfg, engine=None, allow_fallback=True,
              context=None, on_delta=None):
    """翻译文本,返回 (译文, 实际使用的引擎 id)。

    context  —— 前文,给大模型当参考(只支持 supports_context 的引擎);
    on_delta —— 流式回调,每生成一小段就收到一次累积译文(只支持
                supports_stream 的引擎)。两者对不支持的引擎自动忽略。

    首选引擎失败会按 _fallback_order 兜底;缺 Key 会抛 MissingKeyError,
    界面据此向用户索要 Key(而不是静默换引擎)。
    """
    text = (text or "").strip()
    preferred = engine or cfg.get("engine", "google")
    if not text:
        return "", preferred

    spec = ENGINES.get(preferred)
    if spec is None:
        raise TranslateError(f"未知的翻译引擎: {preferred}")

    # 首选引擎缺 Key —— 直接告诉界面去要 Key,不做静默降级
    missing = missing_params(preferred, cfg)
    if missing:
        raise MissingKeyError(preferred, spec, missing)

    target = cfg.get("target_lang", "zh-CN")

    # 原文已经是目标语言(比如中文字幕要翻成中文)就直接原样显示,
    # 既不发多余请求,也避免接口报「源语言和目标语言相同」
    if already_in_target(text, target, cfg):
        return text, NO_TRANSLATION

    order = [preferred]
    if allow_fallback:
        order += _fallback_order(preferred, cfg)

    errors = []
    echoed = None       # 有引擎把原文原样退回来时的兜底结果
    for idx, eng in enumerate(order):
        s = ENGINES.get(eng)
        if s is None:
            continue
        if missing_params(eng, cfg):
            continue
        # 冷却只用来跳过**兜底**引擎(免得每次都白等 Google 超时)。
        # 用户自己选的那个引擎永远要试 —— 不然一旦网络抖一下进了冷却,
        # 用户点翻译只会看到「暂不可用」,还以为程序坏了。
        if idx > 0 and not _engine_ok(eng):
            errors.append(f"{s.name}: 暂不可用(冷却中)")
            continue
        try:
            out = _run_engine(s, text, cfg, target, context, on_delta)
            if not (out and out.strip()):
                errors.append(f"{s.name}: 返回空译文")
                mark_down(eng)
                continue
            out = out.strip()
            if _same_text(out, text):
                # 原文原样退回来了。中英混排这类文本,有些引擎(比如 MyMemory)
                # 会直接把原文吐回来。这不算翻译成功,先留着当兜底,继续试下一个
                # 引擎 —— 否则用户看到的是"没翻译",却当成翻译结果显示出去。
                if echoed is None:
                    echoed = out
                errors.append(f"{s.name}: 原样退回,没翻译")
                continue
            mark_good(eng)
            return out, eng
        except AlreadyTargetError:
            return text, NO_TRANSLATION
        except MissingKeyError:
            raise
        except Exception as e:
            errors.append(f"{s.name}: {e}")
            mark_down(eng)

    # 所有引擎都没翻出来:至少把原文还给用户,比直接报错强
    if echoed is not None:
        log.info("所有引擎都原样退回,改为显示原文:%s", "; ".join(errors))
        return echoed, NO_TRANSLATION

    raise TranslateError("; ".join(errors) if errors else "翻译失败")


def test_engine(engine_id, cfg, timeout=12):
    """测试引擎连通性,返回 (是否成功, 说明文字)。"""
    spec = ENGINES.get(engine_id)
    if spec is None:
        return False, "未知引擎"
    missing = missing_params(engine_id, cfg)
    if missing:
        return False, "还缺:" + "、".join(p.label for p in missing)
    probe = dict(cfg)
    try:
        out = spec.runner("hello", probe, "zh-CN")
    except MissingKeyError as e:
        return False, str(e)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if not (out or "").strip():
        return False, "接口返回空结果"
    mark_good(engine_id)
    return True, f"连接正常,测试译文:{out.strip()[:40]}"


# ---------------------------------------------------------------- 百度在线 OCR 支持

_baidu_token_cache = {"token": "", "expire": 0.0}


def baidu_access_token(api_key, secret_key, timeout=10):
    """用百度 OCR 的 API Key / Secret Key 换取 access_token(缓存 25 天)。"""
    now = time.time()
    if _baidu_token_cache["token"] and now < _baidu_token_cache["expire"]:
        return _baidu_token_cache["token"]
    r = requests.post(
        "https://aip.baidubce.com/oauth/2.0/token",
        params={"grant_type": "client_credentials",
                "client_id": api_key, "client_secret": secret_key},
        headers={"User-Agent": USER_AGENT}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    token = data.get("access_token")
    if not token:
        raise TranslateError(
            f"百度 OCR 取 token 失败: {data.get('error_description', data)}")
    _baidu_token_cache["token"] = token
    _baidu_token_cache["expire"] = now + 25 * 24 * 3600
    return token


def ocr_baidu(img_bytes, api_key, secret_key, lang="auto", timeout=15):
    """百度通用文字识别(高精度版),返回识别文本。"""
    import base64
    token = baidu_access_token(api_key, secret_key)
    url = ("https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic"
           f"?access_token={token}")
    data = {"image": base64.b64encode(img_bytes).decode("ascii"),
            "detect_direction": "true",
            "paragraph": "true"}
    if lang in _BAIDU_LANG:
        data["language_type"] = _BAIDU_LANG[lang].upper()
    r = requests.post(url, data=data, timeout=timeout,
                      headers={"Content-Type": "application/x-www-form-urlencoded",
                               "User-Agent": USER_AGENT})
    r.raise_for_status()
    body = r.json()
    if "error_code" in body:
        raise TranslateError(
            f"百度 OCR 失败({body.get('error_code')}): {body.get('error_msg')}")
    words = body.get("words_result") or []
    return "\n".join(w.get("words", "") for w in words).strip()
