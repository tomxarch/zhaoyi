"""照译：统一的翻译接口，可以切换多家模型。

每家模型的 key 可以放在环境变量里，也可以在界面里填（会存进本机的 config.json）：
    gemini    ZY_KEY_GEMINI    （有免费额度）
    deepseek  ZY_KEY_DEEPSEEK
    qwen      ZY_KEY_QWEN      （新用户有免费额度）
    deepl     ZY_KEY_DEEPL     （DeepL API Free 每月 50 万字符免费）
    microsoft ZY_KEY_MICROSOFT （微软翻译 Azure Translator，免费档每月 200 万字符，另要填资源所在区域）
    tencent   ZY_KEY_TENCENT   （腾讯云机器翻译，每月 500 万字符免费，实名认证即可，不用信用卡；另要填 SecretId）
    google_mt ZY_KEY_GOOGLE_MT （谷歌翻译 Cloud Translation，每月 50 万字符免费，要绑卡开通，国内一般连不上）
    还有 Kimi、智谱、豆包、硅基流动、Claude，以及“自定义”（任何 OpenAI 兼容的接口，自己填地址）
    test      不需要 key，只在原文前加 [译]，用来不花钱地测试流程
"""
import csv
import hashlib
import hmac
import html
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None
try:  # 插件里只用到拆分、写回，没装 openai 也能跑
    from openai import OpenAI
except ImportError:
    OpenAI = None

# 名称: (显示名, 是否有免费额度, 接口类型, 地址, 默认模型)
PROVIDERS = {
    "gemini": ("Google Gemini", True, "openai",
               "https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-3.5-flash-lite"),
    "deepseek": ("DeepSeek", False, "openai", "https://api.deepseek.com", "deepseek-flash"),
    "qwen": ("通义千问", True, "openai",
             "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "kimi": ("Kimi（月之暗面）", False, "openai", "https://api.moonshot.cn/v1", "kimi-k3"),
    "zhipu": ("智谱 GLM", True, "openai", "https://open.bigmodel.cn/api/paas/v4/", "glm-4-flash"),
    "doubao": ("豆包（火山方舟）", True, "openai", "https://ark.cn-beijing.volces.com/api/v3", "doubao-seed-2-0-lite"),
    "siliconflow": ("硅基流动", True, "openai", "https://api.siliconflow.cn/v1", "Qwen/Qwen3-8B"),
    "claude": ("Claude", False, "openai", "https://api.anthropic.com/v1/", "claude-haiku-5-5"),
    "custom": ("自定义（OpenAI 兼容）", False, "openai", None, None),
    "deepl": ("DeepL", True, "deepl", None, None),
    # 传统机器翻译：免费额度大、快，但没有场景和风格要求，术语表靠“这几个词不许翻”的标记硬塞进去
    # 腾讯云机器翻译代码在下面（_tencent），用户暂时不要，先不放进列表：
    # "tencent": ("腾讯翻译（机器翻译）", True, "mt", "https://tmt.tencentcloudapi.com", None),
    "microsoft": ("微软翻译（机器翻译）", True, "mt", "https://api.cognitive.microsofttranslator.com/translate", None),
    "google_mt": ("谷歌翻译（机器翻译）", True, "mt", "https://translation.googleapis.com/language/translate/v2", None),
    "test": ("测试（不联网）", False, "test", None, None),
}

# 能看图的模型：PDF 转 Word（看图翻译）要用。None 表示用当前选的模型本身；没列出的（比如 DeepSeek）不能看图
VISION_MODELS = {"gemini": None, "qwen": "qwen-vl-max", "zhipu": "glm-4v-flash", "kimi": None, "doubao": None,
                 "siliconflow": "Qwen/Qwen2.5-VL-72B-Instruct", "claude": None, "custom": None}

# 每家常用的模型，界面上给个参考；也可以点型号旁的“查询”从服务商那里拿完整列表，或者直接填名字
MODEL_HINTS = {
    "gemini": ["gemini-3.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro"],
    "deepseek": ["deepseek-flash", "deepseek-v4-pro", "deepseek-chat"],
    "qwen": ["qwen-plus", "qwen-turbo", "qwen-max", "qwen-vl-max"],
    "kimi": ["kimi-k3", "kimi-k3.1-pro"],
    "zhipu": ["glm-4-flash", "glm-4.6", "glm-4v-flash"],
    "doubao": ["doubao-seed-2-0-lite"],
    "siliconflow": ["Qwen/Qwen3-8B", "Qwen/Qwen3.5-9B", "Qwen/Qwen2.5-7B-Instruct", "THUDM/GLM-4-9B-0414",
                    "deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"],
    "claude": ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"],
}

# 不要钱的型号（服务商的列表里不标价格，这里记一下，界面上标“免费”）
FREE_MODELS = {
    "zhipu": ["glm-4-flash", "glm-4v-flash"],
    # 硅基流动的免费型号会变，以它网站“模型广场”里标“免费”的为准
    "siliconflow": ["Qwen/Qwen3-8B", "Qwen/Qwen3.5-9B", "Qwen/Qwen2.5-7B-Instruct", "THUDM/GLM-4-9B-0414", "THUDM/glm-4-9b-chat",
                    "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B", "internlm/internlm2_5-7b-chat"],
}

# 目标语言: (DeepL 代码, BabelDOC 代码)
LANGUAGES = {
    "英文": ("EN-US", "en"), "中文": ("ZH", "zh"), "日文": ("JA", "ja"), "韩文": ("KO", "ko"),
    "法文": ("FR", "fr"), "德文": ("DE", "de"), "西班牙文": ("ES", "es"), "俄文": ("RU", "ru"),
}

# 机器翻译用的语言代码：目标语言: (微软, 谷歌, 腾讯)
MT_CODES = {
    "英文": ("en", "en", "en"), "中文": ("zh-Hans", "zh-CN", "zh"), "日文": ("ja", "ja", "ja"), "韩文": ("ko", "ko", "ko"),
    "法文": ("fr", "fr", "fr"), "德文": ("de", "de", "de"), "西班牙文": ("es", "es", "es"), "俄文": ("ru", "ru", "ru"),
}
LLM_ONLY = "机器翻译只会照字面翻，不能润色、看图或按意见修改，这一步请选一个大模型。"

# 翻译场景：给模型设定身份和规则。名称: (显示名, 一句话说明, 提示词)
SCENES = {
    "general": ("通用", "不确定是什么文件时用", ""),
    "contract": ("合同与法律", "用词严谨，宁直译不意译", (
        "你是有十年经验的法律翻译。用词严谨、前后一致，宁可直译也不要意译；"
        "条款编号、金额、日期、当事人名称原样保留；shall、may、must 这类表示义务和权利的词要区分清楚；"
        "不要增加或删减任何义务、条件和例外。")),
    "paper": ("学术论文", "学术书面语，术语用通行译法", (
        "你是学术论文翻译。使用学术书面语，专业术语采用该领域通行的译法，"
        "引用标记、公式、变量名、图表编号保持不变；不要口语化，也不要添加原文没有的解释。")),
    "manual": ("说明书与技术文档", "步骤清楚，参数和按钮名准确", (
        "你是技术文档翻译。句子简洁明确，操作步骤用祈使句；"
        "界面按钮、菜单名、型号、参数和单位保持准确，不要意译；警告和注意事项的语气不能减弱。")),
    "marketing": ("营销文案", "读起来自然有吸引力，可以意译", (
        "你是营销文案本地化专家。目标是让目标语言的读者读起来自然、有吸引力，"
        "可以意译、调整句式和修辞，但不能改变产品事实、价格和承诺；口号要简短有力。")),
    "resume": ("简历与求职", "招聘方熟悉的写法", (
        "你是求职材料翻译。使用目标语言招聘方熟悉的简历写法，经历描述用动词开头、简洁有力；"
        "公司名、学校名用官方译名，不确定时保留原文；时间和数字保持准确。")),
    "electrical": ("电气工程", "IEC/国标用语，含西门子、施耐德", (
        "你是电气工程与工业自动化领域的资深译者，熟悉 IEC 60947、IEC 61439、IEC 60204、GB/T 2900、GB/T 7251 等标准的用语，"
        "也熟悉西门子（SIMATIC、SINAMICS、SIRIUS、SENTRON、TIA Portal 博途）和施耐德电气"
        "（Modicon、Altivar、TeSys、Acti9、ComPacT、MasterPacT、EcoStruxure）的产品手册。"
        "术语按 IEC 和国家标准的通行译法；产品系列名、型号、订货号保持原样不翻译；"
        "参数符号（In、Ie、Ue、Ui、Uimp、Icu、Ics、Icw、Icm）、单位和数值原样保留；"
        "安全警告（危险、警告、小心、注意）的语气不能减弱。")),
    "casual": ("日常与聊天", "口语化，保留语气", (
        "你翻译的是日常对话或网络内容。语气自然口语化，保留原文的情绪和语气词，"
        "网络用语找目标语言里对应的说法，不要翻得像公文。")),
}


def scene_prompt(scene_id, custom_scenes=None):
    """按场景 id 找提示词。自定义场景存在 config.json 的 scenes 里。"""
    if scene_id in SCENES:
        return SCENES[scene_id][2]
    for scene in custom_scenes or []:
        if scene.get("id") == scene_id:
            return scene.get("prompt", "")
    return ""


ONE_RULE = ("每条只给一种译法，不要用斜杠、“或”并列好几种译法，也不要加括号注释。"
            "很短的词（特别是表格里的表头、单个词）要结合上下文和整份文件的行业来理解，用该行业的标准术语，"
            "不要按日常意思直译（比如电气设备文件里法语 Cellule 是开关柜，Unité 是单位，Mesures 是测量，arrivée 是进线）。")

TRANSLATE_PROMPT = ("把用户给的文字翻译成{lang}。"
                    "只输出译文，不要解释，不要加引号。原文里的数字、网址、代码保持不变。") + ONE_RULE


def one_choice(source, result):
    """模型偶尔会给“最低要求/必要条件”这样并列的两种译法：原文里没有斜杠时只留第一种。
    只管表头、格子这类短译文，长句里分不清斜杠两边到哪儿为止，不动。"""
    if "/" in source or "／" in source or len(result) > 40:
        return result
    return re.sub(r"([\u4e00-\u9fff]{1,12})(?:[/／][\u4e00-\u9fff]{1,12})+", r"\1", result)

TAG_RULE = ("原文用 <x1>…</x1>、<x2>…</x2> 这样的标记区分不同格式（比如加粗、斜体）。"
            "译文里必须保留每一个标记，用它包住对应意思的译文，可以按{lang}的语序调整标记的先后，"
            "不要新增或删掉标记。")

REVIEW_PROMPT = ("你是资深{lang}审校。用户会给你原文和一份{lang}初译。"
                 "检查错译、漏译、不通顺和术语不一致，必要时改写成更自然的{lang}。"
                 "如果初译里有 <x1>…</x1> 这类格式标记，改后的译文也要保留同样的标记。"
                 "只输出一个 JSON 对象，不要输出其他内容："
                 '{{"translation": "改后的译文（没改就原样给出）", "changed": true 或 false, '
                 '"reason": "改了什么、为什么，一句话；没改就留空"}}')


# ---------- key 和设置 ----------

def config_path():
    base = os.environ.get("APPDATA") or Path.home() / ".config"
    return Path(base) / "ZhaoYi" / "config.json"


def load_config():
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(config):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def update_config(change):
    """改设置：先从文件重新读一遍最新的，只改这一处，再存回去。
    不用窗口内存里那份旧的整个覆盖，免得别处刚存的 key、翻译记录被冲掉。返回改好的设置。"""
    config = load_config()
    change(config)
    save_config(config)
    return config


def get_key(name):
    # 环境变量优先；也兼容第 1 天的 ZY_API_KEY
    return (os.environ.get(f"ZY_KEY_{name.upper()}")
            or load_config().get("keys", {}).get(name)
            or os.environ.get("ZY_API_KEY"))


def list_providers():
    """返回 [(名称, 显示名, 是否免费)]，界面上用来做下拉框。"""
    return [(k, v[0], v[1]) for k, v in PROVIDERS.items()]


# ---------- 术语表 ----------

def load_glossary(path):
    """读取 CSV 术语表，第一行是表头 source,target（和 BabelDOC 的格式一样）。"""
    terms = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            src = (row.get("source") or "").strip()
            tgt = (row.get("target") or "").strip()
            if src and tgt:
                terms[src] = tgt
    return terms


# 行业术语库：随程序一起提供，翻译时可以勾选。名称: (显示名, 一句话说明, 文件)
TERM_BANKS = {
    "electrical": ("电气行业", "断路器、接触器、PLC 等，按 IEC 和国标译法，含西门子、施耐德常用词，也认法文技术规范的说法",
                   "electrical.csv"),
}
BANK_DIR = Path(__file__).parent / "glossaries"


def load_term_bank(name, target_lang):
    """读取行业术语库。文件是中英对照（zh,en,abbr,only），按翻译方向取一边做原文。
    目前只支持中译英和英译中，其他目标语言返回空。"""
    if name not in TERM_BANKS or target_lang not in ("英文", "中文"):
        return {}
    terms = {}
    with open(BANK_DIR / TERM_BANKS[name][2], encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            zh, en = row["zh"].strip(), row["en"].strip()
            if target_lang == "英文":
                terms.setdefault(zh, en)
            elif row.get("only") != "zh2en":
                terms.setdefault(en, zh)
                abbr = (row.get("abbr") or "").strip()
                if len(abbr) >= 3:  # 太短的缩写（N、PE、NO）容易误伤，不用
                    terms.setdefault(abbr, zh)
    # 法文原文（法语区的招标技术规范）：另一张表，法文 → 英文 / 中文
    french = BANK_DIR / (Path(TERM_BANKS[name][2]).stem + "_fr.csv")
    if french.exists():
        with open(french, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                terms.setdefault(row["fr"].strip(), row["en" if target_lang == "英文" else "zh"].strip())
    return terms


def terms_in(text, glossary):
    lower = text.lower()
    found = {}
    for s, t in (glossary or {}).items():
        if s.isascii():
            # 英文按整词匹配，免得 coil 匹配到 recoil
            if re.search(r"(?<![A-Za-z])" + re.escape(s.lower()) + r"(?![A-Za-z])", lower):
                found[s] = t
        elif s.lower() in lower:
            found[s] = t
    return found


def glossary_prompt(text, glossary):
    terms = terms_in(text, glossary)
    if not terms:
        return ""
    return ("以下术语必须按指定方式翻译（大小写随位置调整，句首、标题、表格单元格开头要首字母大写；"
            "用在句子里时按语法调整词形，比如“合格 → Pass”在句中写成 passed，不要写出 is Pass 这种不通的句子）："
            + "；".join(f"{s} → {t}" for s, t in terms.items()))


# ---------- 翻译 ----------

class Translator:
    def __init__(self, provider="gemini", target_lang="英文", model=None, glossary=None, scene="",
                 banks=()):
        if provider not in PROVIDERS:
            raise ValueError(f"不认识的模型：{provider}，可选：{', '.join(PROVIDERS)}")
        if target_lang not in LANGUAGES:
            raise ValueError(f"暂不支持目标语言：{target_lang}，可选：{'、'.join(LANGUAGES)}")
        self.provider = provider
        self.target_lang = target_lang
        # 用户自己的术语表优先，行业术语库补在后面；只有用户自己的会在译完后检查
        self.check_glossary = dict(glossary or {})
        self.glossary = {}
        for bank in banks:
            self.glossary.update(load_term_bank(bank, target_lang))
        config = load_config()
        # 用户提意见时记下的习惯（“合格一律译 Pass”“标题全大写”），按目标语言分开存
        learned = config.get("learned", {}).get(target_lang, {})
        self.learned_terms = dict(learned.get("terms", {}))
        # “合格一律译 Pass”这种只是在重复术语，不算风格要求（算了的话整份文件都要重翻）
        self.learned_rules = [r for r in learned.get("rules", []) if not any(t in r for t in self.learned_terms)]
        self.base_glossary = {**self.glossary, **self.check_glossary}  # 不含习惯，用来算“译过没有”的记号
        self.glossary.update(self.learned_terms)
        self.glossary.update(self.check_glossary)
        self.scene = scene.strip()
        if self.learned_rules:
            self.scene = ((self.scene or "你是专业翻译。") + "另外必须遵守用户提过的要求："
                          + "；".join(self.learned_rules) + "。")
        self.pairs = {}  # 这份文件的“原文 → 译文”，提意见修改时用
        self.label, _, self.kind, self.base_url, default_model = PROVIDERS[provider]
        if provider == "custom":
            self.base_url = (config.get("custom_url") or "").strip() or None
            if not self.base_url:
                raise RuntimeError("自定义模型还没填接口地址，在设置的“模型与 key”里填。")
        self.model = (model or os.environ.get("ZY_MODEL") or config.get("models", {}).get(provider)
                      or default_model)
        if self.kind == "openai" and not self.model:
            raise RuntimeError(f"{self.label} 还没填模型名，在设置的“模型与 key”里填。")
        self.key = get_key(provider)
        # Gemini 免费版每分钟只能请求 15 次，同时翻的段数少一点，免得被限流
        self.workers = 2 if provider == "gemini" else 4

        if self.kind != "test" and not self.key:
            raise RuntimeError(f"还没有设置 {self.label} 的 key。")
        if self.kind == "openai":
            self.client = OpenAI(api_key=self.key, base_url=self.base_url, max_retries=0, timeout=60)

    def translate(self, text, tagged=False):
        if not text.strip():
            return text
        if self.kind == "test":
            return "[译]" + text
        if self.kind == "deepl":
            return with_retry(lambda: self._deepl(text, tagged))
        if self.kind == "mt":
            return self.translate_many([text])[0]

        hit = self.recall(text)
        if hit:
            self.pairs[text] = hit
            return hit
        system = (self.scene or "你是专业翻译。") + TRANSLATE_PROMPT.format(lang=self.target_lang)
        if tagged:
            system += TAG_RULE.format(lang=self.target_lang)
        system += glossary_prompt(text, self.glossary)
        raw = with_retry(lambda: self._chat(system, text))
        result = plain_reply(text, raw)
        if result is None:  # 模型回了一段 JSON、又找不到这条的译文：再要一次纯文字，还不行就留原文，绝不把 JSON 写进文件
            raw = with_retry(lambda: self._chat(system + "只输出这一条的译文本身，不要 JSON，不要列表。", text))
            result = plain_reply(text, raw)
            if result is None:
                self.pairs[text] = text
                return text
        result = capitalize_first(text, one_choice(text, result), self.target_lang)
        self.remember(text, result)
        self.pairs[text] = result
        return result

    # ---------- 译过的存起来：额度用完、手动停止后再翻同一个文件，已经译好的不再请求、不再花钱 ----------

    def _memo_key(self, text):
        # 模型、语言、场景、术语表任何一样变了，都算新的翻译，不拿旧的。
        # 记下的习惯术语只看这段用到的那几个：加一条“合格 → Pass”，只有含“合格”的段要重翻
        if not hasattr(self, "_setup"):
            drafter = getattr(self, "drafter", None)  # 机器初译 + 大模型把关，和直接用大模型翻的分开记
            # "v3"：批量译文改成逐条核对原文以后的记录。以前存的可能有串格（拿了别的格子的译文）或混进了 JSON，不再拿出来用
            setup = json.dumps(["v3", self.provider, self.model, drafter.provider if drafter else "", self.target_lang, self.scene,
                                sorted(self.base_glossary.items())], ensure_ascii=False)
            self._setup = hashlib.sha1(setup.encode("utf-8")).hexdigest()
        used = json.dumps(sorted(terms_in(text, self.learned_terms).items()), ensure_ascii=False)
        return hashlib.sha1((self._setup + "\x00" + used + "\x00" + text).encode("utf-8")).hexdigest()

    def recall(self, text):
        if self.provider == "test":
            return None
        return memo_get(self._memo_key(text))

    def remember(self, text, result):
        if self.provider != "test" and result:
            memo_put(self._memo_key(text), result)

    def review(self, source, draft):
        """润色：返回 (改后译文, 是否改动, 理由)。"""
        if self.kind in ("deepl", "mt"):
            raise ValueError(LLM_ONLY)
        if self.kind == "test":
            changed = len(source) > 10
            return (draft + "（润色）" if changed else draft), changed, "测试用的润色" if changed else ""

        system = self.scene + REVIEW_PROMPT.format(lang=self.target_lang) + glossary_prompt(source, self.glossary)
        user = f"原文：\n{source}\n\n初译：\n{draft}"
        reply = with_retry(lambda: self._chat(system, user))
        match = re.search(r"\{.*\}", reply, re.S)
        try:
            data = json.loads(match.group(0))
            final = capitalize_first(source, str(data.get("translation") or draft).strip(), self.target_lang)
            changed = bool(data.get("changed")) and final != draft
            return final, changed, str(data.get("reason") or "").strip()
        except (AttributeError, ValueError):
            return draft, False, ""

    # ---------- 机器翻译（微软、谷歌）：一次送一批，按字符算免费额度 ----------

    def translate_many(self, texts):
        """一批文字用机器翻译，返回同样长度的列表。译过的直接拿（不占额度），术语表里的词按指定译法。"""
        results = list(texts)
        todo = []
        for i, t in enumerate(texts):
            hit = self.recall(t) if t.strip() else t
            if hit:
                results[i] = hit
                self.pairs[t] = hit
            else:
                todo.append(i)
        chunks, cur, size = [], [], 0
        for i in todo:  # 每次最多 50 段、约 5000 字（两家的单次上限都比这大）
            if cur and (len(cur) >= 50 or size + len(texts[i]) > 5000):
                chunks.append(cur)
                cur, size = [], 0
            cur.append(i)
            size += len(texts[i])
        if cur:
            chunks.append(cur)
        for chunk in chunks:
            sent = [self._mt_markup(texts[i]) if self.provider != "tencent" else self._mt_plain(texts[i]) for i in chunk]
            got = with_retry(lambda: self._mt_call(sent))
            for i, out in zip(chunk, got):
                out = capitalize_first(texts[i], self._mt_unmarkup(out) if self.provider != "tencent" else out.strip(),
                                       self.target_lang)
                results[i] = out
                self.remember(texts[i], out)
                self.pairs[texts[i]] = out
        return results

    def _mt_markup(self, text):
        """转成 HTML 送去翻：格式标记 <x1> 原样保留；术语表里的词直接换成译法，包上“不许翻”的标记。"""
        out = html.escape(text, quote=False)
        out = re.sub(r"&lt;(/?x\d+)&gt;", r"<\1>", out)
        for s, t in sorted(terms_in(text, self.glossary).items(), key=lambda kv: -len(kv[0])):
            flags = re.I if s.isascii() else 0
            pattern = (r"(?<![A-Za-z])" + re.escape(html.escape(s, quote=False)) + r"(?![A-Za-z])"
                       if s.isascii() else re.escape(html.escape(s, quote=False)))
            out = re.sub(pattern, lambda m: f'<span class="notranslate" translate="no">{html.escape(t, quote=False)}</span>',
                         out, flags=flags)
        return out

    def _mt_plain(self, text):
        """腾讯翻译只收纯文字：术语表里的词先直接换成译法（夹在原文里，机器翻译一般会原样留着）。"""
        for s, t in sorted(terms_in(text, self.glossary).items(), key=lambda kv: -len(kv[0])):
            pattern = r"(?<![A-Za-z])" + re.escape(s) + r"(?![A-Za-z])" if s.isascii() else re.escape(s)
            text = re.sub(pattern, lambda m: f" {t} ", text, flags=re.I if s.isascii() else 0)
        return re.sub(r" {2,}", " ", text).strip()

    def _tencent(self, texts, target):
        """腾讯云机器翻译 TextTranslateBatch，按腾讯云 API 3.0 的 TC3-HMAC-SHA256 签名。"""
        secret_id = (load_config().get("tencent_id") or os.environ.get("ZY_TENCENT_ID") or "").strip()
        if not secret_id:
            raise MTError(401, "missing SecretId", "腾讯翻译还没填 SecretId，在设置的“模型与 key”里填（和 SecretKey 在同一个页面）。")
        host, action, version = "tmt.tencentcloudapi.com", "TextTranslateBatch", "2018-03-21"
        payload = json.dumps({"Source": "auto", "Target": target, "ProjectId": 0, "SourceTextList": texts},
                             ensure_ascii=False)
        stamp = int(time.time())
        day = time.strftime("%Y-%m-%d", time.gmtime(stamp))
        sha = lambda b: hashlib.sha256(b.encode("utf-8")).hexdigest()
        canonical = ("POST\n/\n\ncontent-type:application/json; charset=utf-8\nhost:" + host
                     + "\nx-tc-action:" + action.lower() + "\n\ncontent-type;host;x-tc-action\n" + sha(payload))
        scope = f"{day}/tmt/tc3_request"
        to_sign = f"TC3-HMAC-SHA256\n{stamp}\n{scope}\n{sha(canonical)}"
        sign = lambda k, m: hmac.new(k, m.encode("utf-8"), hashlib.sha256).digest()
        signing = sign(sign(sign(("TC3" + self.key).encode("utf-8"), day), "tmt"), "tc3_request")
        signature = hmac.new(signing, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        headers = {"Authorization": f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, "
                                    f"SignedHeaders=content-type;host;x-tc-action, Signature={signature}",
                   "Content-Type": "application/json; charset=utf-8", "Host": host, "X-TC-Action": action,
                   "X-TC-Timestamp": str(stamp), "X-TC-Version": version, "X-TC-Region": "ap-guangzhou"}
        r = requests.post(self.base_url, headers=headers, data=payload.encode("utf-8"), timeout=60)
        check_mt(r)
        body = r.json().get("Response", {})
        err = body.get("Error")
        if err:  # 腾讯云出错也回 200，错误写在 Response.Error 里
            code, message = err.get("Code", ""), err.get("Message", "")
            if code.startswith("AuthFailure"):
                raise MTError(401, code + " " + message, "腾讯翻译的 SecretId 或 SecretKey 不对，在设置里检查（两个都在腾讯云“访问管理 → API 密钥”页面）。")
            if code in ("FailedOperation.NoFreeAmount", "FailedOperation.ServiceIsolate", "FailedOperation.StopUsing"):
                raise MTError(402, code + " " + message)
            if "UserNotRegistered" in code or "ActionNotFound" in code or code.startswith("UnauthorizedOperation"):
                raise MTError(403, code + " " + message, "腾讯翻译还没开通：在腾讯云控制台搜“机器翻译”，点开通（免费额度内不收费）。")
            if code.startswith("RequestLimitExceeded"):
                raise MTError(429, code + " " + message)
            raise MTError(400, code + " " + message)
        return body["TargetTextList"]

    @staticmethod
    def _mt_unmarkup(text):
        text = re.sub(r'<span class="notranslate" translate="no">(.*?)</span>', r" \1 ", text, flags=re.S)
        text = re.sub(r"(?<=[\u4e00-\u9fff]) | (?=[\u4e00-\u9fff，。；：、）])", "", text)  # 中文里不要多出空格
        text = re.sub(r" {2,}", " ", text)
        text = re.sub(r" ([,.;:!?)])", r"\1", text)
        return html.unescape(text).strip()

    def _mt_call(self, texts):
        if stop_event.is_set():
            raise Stopped()
        self.calls = getattr(self, "calls", 0) + 1
        ms, google, tencent = MT_CODES[self.target_lang]
        if self.provider == "tencent":
            return self._tencent(texts, tencent)
        if self.provider == "microsoft":
            headers = {"Ocp-Apim-Subscription-Key": self.key, "Content-Type": "application/json"}
            region = (load_config().get("ms_region") or "").strip()
            if region:
                headers["Ocp-Apim-Subscription-Region"] = region
            r = requests.post(self.base_url, params={"api-version": "3.0", "to": ms, "textType": "html"},
                              headers=headers, json=[{"Text": t} for t in texts], timeout=60)
            check_mt(r)
            return [item["translations"][0]["text"] for item in r.json()]
        # key 放在请求头里，不放进网址：网址会出现在报错信息里
        r = requests.post(self.base_url, headers={"X-Goog-Api-Key": self.key},
                          json={"q": texts, "target": google, "format": "html"}, timeout=60)
        check_mt(r)
        return [item["translatedText"] for item in r.json()["data"]["translations"]]

    def _chat(self, system, user):
        if self.kind != "openai":
            raise ValueError(LLM_ONLY)
        if stop_event.is_set():
            raise Stopped()
        self.calls = getattr(self, "calls", 0) + 1  # 发了几次请求（按次、按 token 收费，给用户对账用）
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        extra = NO_THINK.get(self.provider) if getattr(self, "_no_think", True) else None
        try:
            response = self.client.chat.completions.create(model=self.model, messages=messages, temperature=0.3,
                                                           extra_body=extra)
        except Exception as e:
            # 有的型号不认“关掉思考”这个参数，去掉再发一次
            if not extra or getattr(e, "status_code", None) != 400 or "think" not in str(e).lower():
                raise
            self._no_think = False
            response = self.client.chat.completions.create(model=self.model, messages=messages, temperature=0.3)
        return strip_thinking(response.choices[0].message.content or "")

    def _deepl(self, text, tagged):
        # 免费版 key 以 :fx 结尾，用另一个地址
        host = "api-free.deepl.com" if self.key.endswith(":fx") else "api.deepl.com"
        body = {"text": [text], "target_lang": LANGUAGES[self.target_lang][0]}
        if tagged:
            body["tag_handling"] = "xml"
        r = requests.post(
            f"https://{host}/v2/translate",
            headers={"Authorization": f"DeepL-Auth-Key {self.key}"},
            json=body,
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["translations"][0]["text"]


CAPITALIZED_LANGS = ("英文", "法文", "德文", "西班牙文", "俄文")
KEEP_LOWER = {"rpm", "min", "max", "mol", "lux", "ppm", "etc"}


def plain_reply(source, reply):
    """单条翻译的回复整理成译文。有的小模型单条也回 JSON（{"1": ["原文", "译文"]}）：能认出这条的译文就拿出来，
    认不出返回 None。"""
    reply = (reply or "").strip()
    if not re.match(r"^(```\w*\s*)?[\[{]", reply):
        return reply
    try:
        data = json.loads(re.sub(r"^```\w*\s*|\s*```$", "", reply), strict=False)
    except ValueError:
        return None
    norm = lambda t: re.sub(r"[\W_]+", "", str(t)).lower()
    items = list(data.values()) if isinstance(data, dict) else data if isinstance(data, list) else []
    if isinstance(data, dict) and len(data) == 1 and isinstance(items[0], str):
        return items[0].strip() or None
    for item in items:
        if isinstance(item, list) and len(item) == 2 and all(isinstance(x, str) for x in item) \
                and norm(item[0]) == norm(source):
            return item[1].strip() or None
    return None


def capitalize_first(source, result, target_lang):
    """中文原文译成英文等语言时，段首（含表格单元格）首字母大写。模型常把术语原样小写放在段首。"""
    if target_lang not in CAPITALIZED_LANGS or not re.match(r"\s*[\u4e00-\u9fff]", source):
        return result
    match = re.match(r"((?:\s|<[^>]+>)*)([^\W\d_]+)", result)
    if not match:
        return result
    word = match.group(2)
    if len(word) < 3 or not word.islower() or word in KEEP_LOWER:
        return result
    start = match.start(2)
    return result[:start] + word[0].upper() + result[start + 1:]


# Qwen3 这类“先思考再回答”的型号，翻一句话要先写几千字的思考，慢几十倍。翻译用不着思考，关掉
NO_THINK = {
    "siliconflow": {"enable_thinking": False},
    "qwen": {"enable_thinking": False},
    "zhipu": {"thinking": {"type": "disabled"}},
    "doubao": {"thinking": {"type": "disabled"}},
}


def strip_thinking(text):
    """有的型号会把思考过程用 <think>…</think> 写在回答前面，去掉。"""
    return re.sub(r"^\s*<think>.*?</think>", "", text, flags=re.S).strip()


class MTError(Exception):
    """机器翻译接口的报错，带上状态码和服务商说的原因（不含 key）。"""

    def __init__(self, status_code, message, hint=""):
        super().__init__(f"{status_code} {message}")
        self.status_code = status_code
        self.hint = hint  # 直接给用户看的中文说明


def check_mt(r):
    if r.ok:
        return
    try:
        body = r.json()
        err = body.get("error", body) if isinstance(body, dict) else body
        message = err.get("message", "") if isinstance(err, dict) else str(err)
    except ValueError:
        message = r.text[:300]
    raise MTError(r.status_code, message)


class Stopped(Exception):
    """用户点了“停止翻译”。"""

    def __str__(self):
        return "已停止。已经译好的部分存下来了，再点开始翻译会接着翻，译过的不会重复收费。"


stop_event = threading.Event()  # 界面点“停止”时设上，正在等的、要发的请求都不再继续

_memo_lock = threading.Lock()
_memo_db = None


def _memo():
    global _memo_db
    if _memo_db is None:
        path = config_path().with_name("cache.db")
        path.parent.mkdir(parents=True, exist_ok=True)
        _memo_db = sqlite3.connect(str(path), check_same_thread=False)
        _memo_db.execute("create table if not exists memo (k text primary key, v text, t real)")
    return _memo_db


def memo_get(key):
    try:
        with _memo_lock:
            row = _memo().execute("select v from memo where k = ?", (key,)).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def memo_put(key, value):
    try:
        with _memo_lock:
            _memo().execute("insert or replace into memo values (?, ?, ?)", (key, value, time.time()))
            _memo().commit()
    except sqlite3.Error:
        pass


on_wait = None  # 界面设上它：被限流要等的时候告诉用户等几秒（参数：秒数）


def with_retry(call, attempts=5):
    """网络出错或请求太快被限流时，等一会儿再试（2、4、8、16 秒）。key 错了就不重试。
    被限流（429）时按服务商说的等待时间来等，比如 Gemini 免费版会说“48 秒后再试”。"""
    for i in range(attempts):
        try:
            return call()
        except Exception as e:
            status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
            if isinstance(e, Stopped) or status in (400, 401, 402, 403, 404) or no_balance(e) or i == attempts - 1:
                raise
            wait = 2 ** (i + 1)
            if status == 429:
                said = re.search(r"retry(?:Delay)?\D{0,6}?(\d+(?:\.\d+)?)\s*s", str(e), re.I)
                wait = min(float(said.group(1)) + 1, 90) if said else max(wait, 15)
                if on_wait:
                    on_wait(wait)
            if stop_event.wait(wait):
                raise Stopped()


def no_balance(e):
    """余额、额度用完了（DeepSeek 是 402 Insufficient Balance，OpenAI 一类是 insufficient_quota）。等也没用，不重试。"""
    text = str(e).lower()
    return any(w in text for w in ("insufficient balance", "insufficient_quota", "quota exceeded", "余额不足", "欠费",
                                    "out of call volume quota"))


def friendly_error(e):
    """把模型接口的报错换成看得懂的中文。"""
    text = str(e)
    status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
    if isinstance(e, Stopped):
        return str(e)
    resume = "已经译好的部分存下来了，充值或换个模型后再点开始翻译，会接着翻，译过的不会重复收费。"
    if status == 402 or no_balance(e):
        return "账户余额或额度不够了。" + resume
    if isinstance(e, MTError) and e.hint:
        return e.hint
    if isinstance(e, MTError) and status in (401, 403):
        return ("微软翻译的 key 或区域不对，请在设置里检查（区域要和 Azure 里翻译资源的“位置”一致，比如 eastasia）。"
                if "subscription" in text.lower() or status == 401
                else "谷歌翻译没开通：要在 Google Cloud 控制台启用 Cloud Translation API，并绑定结算账号（每月 50 万字符内不收费）。")
    if status == 401 or "API key" in text or "api_key" in text.lower():
        return "key 不对或已失效，请在设置里检查。"
    if status == 404 or "no longer available" in text or "not found" in text.lower():
        return "这个模型已经停用或名字不对。在设置的“模型与 key”里点型号旁的“查询”，换一个型号。"
    if status == 429:
        return "请求太频繁或免费额度用完了，等一会儿再试，或换一个模型。" + resume
    if status in (500, 502, 503) or "high demand" in text:
        return "模型服务现在太忙，重试几次后还是不行。过几分钟再试，或换一个模型。"
    if "Connection" in type(e).__name__ or "timed out" in text.lower():
        return "连不上模型服务。Gemini、谷歌翻译在国内网络一般连不上，可以换 DeepSeek、通义或微软翻译。"
    return text
