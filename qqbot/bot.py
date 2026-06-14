# -*- coding: utf-8 -*-
"""拟人 QQ 群聊机器人 —— 角色卡驱动版（群聊 + 私聊；被动消息由模型判断是否接话）。"""

import asyncio
import base64
import json
import random
import re
import sys
import time
from pathlib import Path

import httpx
import websockets


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


CONFIG = load_json(str(Path(__file__).parent / "config.json"))
CARD_PATH = sys.argv[1] if len(sys.argv) > 1 else "characters/傲娇少女_示例.json"
CARD = load_json(CARD_PATH)

# ============ 长期记忆 ============
# 记忆按角色分目录存放：memory/<角色名>/users/<QQ号>.json（按人）
#                      memory/<角色名>/groups/<群号>.json（按群）
#                      memory/<角色名>/history/...（聊天上下文，重启不丢）
MEM_CFG = CARD.get("memory", {})
MEM_ENABLED = MEM_CFG.get("enabled", True)
MEM_MAX_ITEMS = MEM_CFG.get("max_items", 40)        # 每份记忆最多保留几条（超出淘汰最旧的）
MEM_INJECT_ITEMS = MEM_CFG.get("inject_items", 8)   # 每次回复时注入最近几条记忆
MEM_EXTRACT_EVERY = MEM_CFG.get("extract_every", 30)  # 每隔多少条消息自动提炼一次
MEM_DIALOGUE_ONLY = MEM_CFG.get("dialogue_only", True)  # 存记忆时只留对话，去掉括号里的动作/旁白
ARCHIVE_ALL = MEM_CFG.get("archive_all", False)         # 全程记录：每轮对话都自动逐字存档（她照常聊天）
ARCHIVE_MAX = MEM_CFG.get("archive_max_items", 2000)    # 存档上限（远大于普通记忆的 40，存得下长期点滴）
ARCHIVE_INJECT = MEM_CFG.get("archive_inject_items", MEM_INJECT_ITEMS)  # 每次回复从存档按相关度召回几条
MEM_DIR = Path(__file__).parent / "memory" / CARD.get("name", "默认角色")

# ============ 人设切换 ============
# 在 characters/personas/<角色名>/ 放多个 .txt（每个是一整套人设），
# 聊天里说「角色名，切换人设 XX」就让她改用 XX.txt 这套提示词（模型每次只看到当前这一套）。
# 说「角色名，切换到默认」回到角色卡里的原始人设。改了 .txt 再切一次即可热加载，不用重启。
PERSONA_DIR = Path(__file__).parent / "characters" / "personas" / CARD.get("name", "默认角色")
_active_persona_name = None   # None 表示用角色卡里的默认人设
_active_persona_text = None

# ============ 表情包 ============
# 表情包按角色分文件夹：memes/<角色名>/，文件名就是表情含义，例如：无语.jpg、开心.png、狗头.gif
# AI 会根据聊天气氛自己决定发不发、发哪个。支持 jpg/png/gif/webp，加图删图不用重启。
# 发送时带 subType=1 标记，在 QQ 里以"表情"样式显示（小图贴纸），不是大图照片。
MEME_CFG = CARD.get("memes", {})
MEME_ENABLED = MEME_CFG.get("enabled", True)
MEME_DIR = Path(__file__).parent / MEME_CFG.get("dir", "memes/" + CARD.get("name", "默认角色"))
MEME_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
MEME_TAG_RE = re.compile(r"\[表情[:：]\s*([^\[\]]+?)\s*\]")

# ============ 珍贵回忆·自动铭记 ============
# 她在回复里【单独占一行】写隐藏标记 [铭记] 或 [铭记:一句话] 时，
# 系统会把对方刚才那条原话完整存进长期记忆，并把标记从可见消息里去掉（对方看不到）。
# 这样"记住珍贵回忆"是她自己在对话中决定的，不需要用户发指令，不出戏。
MEMO_TAG_RE = re.compile(r"\[铭记(?:[:：]\s*([^\[\]]*))?\]")
MEMO_PROMPT = (
    "\n【珍藏回忆】遇到下面任一情况，就在回复中【单独占一行】写一个隐藏标记 [铭记]"
    "（对方看不到，会被自动去掉），系统会把这段珍贵的话存进你的长期记忆：\n"
    "  - 对方说起你们珍贵的共同回忆、或关于 TA 的重要细节；\n"
    "  - 【你自己讲起、回忆起一段你们之间的往事时】，也要铭记，把它当作真实发生过的事"
    "记住，以后才不会前后矛盾、忘了自己说过什么。\n"
    "想顺手记一句自己的概括就写成 [铭记:一句话]。只在真正动情、值得珍藏的瞬间用，普通闲聊不要用。\n"
)
# 频率控制（双重闸门，防止每条回复都带表情）：
MEME_OFFER_PROB = MEME_CFG.get("offer_prob", 0.25)      # 每次回复把表情列表提供给模型的概率
MEME_MIN_INTERVAL = MEME_CFG.get("min_interval", 180)   # 两次发表情的最小间隔（秒），不到间隔强制不发


def list_memes() -> dict:
    """扫描表情包文件夹，返回 {名字: 路径}。每次调用都重新扫，加图不用重启。"""
    if not MEME_ENABLED or not MEME_DIR.is_dir():
        return {}
    return {p.stem: p for p in sorted(MEME_DIR.iterdir())
            if p.is_file() and p.suffix.lower() in MEME_EXTS}


def meme_block(state=None):
    """注入提示词：告诉模型有哪些表情包、怎么发。
    频率闸门①：距上次发表情不足 min_interval 秒、或没抽中 offer_prob 概率时，
    干脆不把表情列表给模型——模型不知道有表情，自然就不会发。"""
    names = list(list_memes().keys())
    if not names:
        return ""
    if state is not None:
        if time.time() - state.last_meme_ts < MEME_MIN_INTERVAL:
            return ""
        if random.random() > MEME_OFFER_PROB:
            return ""
    return (
        "\n【表情包】你手机里存了这些表情包：" + "、".join(names) + "\n"
        "想发的时候，在消息里单独占一行写 [表情:名字]（这是唯一允许的特殊标记），"
        "例如：\n[表情:" + names[0] + "]\n"
        "规则：只在气氛合适时偶尔发一个，大多数消息不带表情包；一次回复最多一个；"
        "只能用上面列表里的名字，不要自己编。\n"
    )


def meme_segment(name):
    """按名字找表情包图片，返回 OneBot 图片消息段；找不到返回 None。"""
    path = list_memes().get(name.strip())
    if path is None:
        return None
    try:
        b64 = base64.b64encode(path.read_bytes()).decode()
    except Exception as e:
        print("[表情] 读取失败 " + str(path) + ": " + str(e))
        return None
    return [{"type": "image", "data": {"file": "base64://" + b64,
                                       "subType": 1, "sub_type": 1,
                                       "summary": "[动画表情]"}}]


# ============ 活跃窗口（省 token） ============
# 平时绝大多数群消息直接忽略、不调用 API；只有被 @、喊昵称、提到关键词时才激活"活跃窗口"。
# 窗口内（默认 10 分钟或 50 条消息，先到为准）每条消息都交给模型判断要不要接话。
# 窗口外仅以很小的概率（默认 0.01）随机抽查一条，保留一点"偶然冒泡"的真实感。
ACT_CFG = CARD.get("active_window", {})
ACT_MINUTES = ACT_CFG.get("minutes", 10)            # 激活后保持活跃几分钟
ACT_MAX_MSGS = ACT_CFG.get("max_messages", 50)      # 或最多逐条浏览几条消息（先到为准）
IDLE_JUDGE_PROB = ACT_CFG.get("idle_judge_prob", 0.01)  # 非活跃期随机抽查概率

# ============ 禁言指令 ============
# 群里有人发「角色名，禁言10」→ 该聊天里静默 10 分钟（不带数字用默认值）；
# 「角色名，解除禁言」或时间到 → 恢复。昵称也能用。禁言期间完全不调用 API。
MUTE_DEFAULT_MIN = CARD.get("mute", {}).get("default_minutes", 10)
_call_names = "|".join(re.escape(n) for n in [CARD.get("name", "")] + CARD.get("nicknames", []) if n)
MUTE_RE = re.compile(r"(?:" + _call_names + r")\s*[，,。:：!！\s]*禁言\s*(\d+)?\s*分?钟?")
UNMUTE_RE = re.compile(r"(?:" + _call_names + r")\s*[，,。:：!！\s]*解除禁言\s*(活跃)?")

# ============ 连续记忆模式 ============
# 说「角色名，开始记忆」进入：之后你一条条发的内容会被原文攒下来，她期间不正常接话；
# 说「角色名，记完了」结束：把这期间所有原话整段写入你的个人长期记忆。
# 适合对方一条条把长故事发过来时，省得每条都打「记住」。私聊里名字可省略。
CAPTURE_START_RE = re.compile(
    r"^\s*(?:(?:" + _call_names + r")[，,。:：!！\s]*)?(?:开始记忆|记忆开始|进入记忆模式|开始记录|开始记)\s*$")
CAPTURE_END_RE = re.compile(
    r"^\s*(?:(?:" + _call_names + r")[，,。:：!！\s]*)?(?:记完了|记完|记忆结束|结束记忆|结束记录|退出记忆模式|记好了)\s*$")
CAPTURE_MAX_BUF = 100  # 安全上限：忘了说「记完了」时，攒够这么多条自动先落盘一次

# ============ 人设切换指令 ============
# 「角色名，切换人设 温柔日常」/「换人设：认真」/「切换到默认」等；私聊里名字可省略。
PERSONA_SWITCH_RE = re.compile(
    r"^\s*(?:(?:" + _call_names + r")[，,。:：!！\s]*)?(?:切换人设|切换到|换人设|人设切换|切人设|换成人设)\s*[：:]?\s*(.+?)\s*(?:人设|模式)?\s*$")
PERSONA_LIST_RE = re.compile(
    r"^\s*(?:(?:" + _call_names + r")[，,。:：!！\s]*)?(?:有哪些人设|人设列表|人设有哪些|查看人设|列出人设|当前人设)\s*[？?]?\s*$")


async def _mute_expiry_announce(ws, state, sender_func, target, until, tag):
    """禁言到点后自动播报"xx已经解除禁言啦"；若期间被手动解除/重新禁言则不播报。"""
    await asyncio.sleep(max(0.0, until - time.time()))
    if state.muted_until != until:
        return  # 已被手动解除，或被重新禁言（截止时间变了）
    state.muted_until = 0.0
    state.deactivate()  # 到期解禁：默认回到非活跃期
    try:
        await sender_func(ws, target, CARD["name"] + "已经解除禁言啦")
    except Exception as e:
        print(tag + " 解禁播报发送失败(连接可能已重连): " + str(e))
    print(tag + " 禁言到期，自动恢复（非活跃期）")


def _drop_current_message(state):
    """把刚刚记进去的这条「指令消息」从短期上下文和提炼队列里撤掉，不留痕。
    用在禁言/解禁、人设切换、开始/结束记忆这类纯指令被识别之后。"""
    if state.history:
        state.history.pop()
        try:
            state._save_history()
        except Exception:
            pass
    if state.extract_buf:
        state.extract_buf.pop()


async def handle_mute_command(ws, text, state, sender_func, target, tag):
    """处理禁言/解禁指令。返回 True 表示这条消息是指令（或正处于禁言期），外层直接返回。"""
    m = UNMUTE_RE.search(text)
    if m:
        if time.time() < state.muted_until:
            state.muted_until = 0.0  # 手动解除：到期播报任务看到时间被清零就不播了
            if m.group(1):  # 「xx，解除禁言 活跃」→ 直接进入活跃窗口
                state.activate()
                await sender_func(ws, target, "（已解除禁言，进入活跃模式）")
                print(tag + " 被手动解除禁言（活跃模式）")
            else:  # 普通解除：默认非活跃期
                state.deactivate()
                await sender_func(ws, target, "（已解除禁言）")
                print(tag + " 被手动解除禁言（非活跃期）")
        _drop_current_message(state)  # 解禁指令本身不计入记录
        return True
    m = MUTE_RE.search(text)
    if m:
        minutes = int(m.group(1)) if m.group(1) else MUTE_DEFAULT_MIN
        until = time.time() + minutes * 60
        state.muted_until = until
        state.deactivate()  # 禁言同时清掉活跃窗口
        await sender_func(ws, target, "（已禁言 " + str(minutes) + " 分钟）")
        print(tag + " 被禁言 " + str(minutes) + " 分钟")
        asyncio.create_task(_mute_expiry_announce(ws, state, sender_func, target, until, tag))
        _drop_current_message(state)  # 禁言指令本身不计入记录
        return True
    if time.time() < state.muted_until:
        return True  # 禁言期内：只记上下文，其余一概不处理
    return False


def _flush_capture(state):
    """把「连续记忆模式」攒下的原话整段写入发起人的长期记忆；超长自动切块。返回写入条数。"""
    msgs = state.capture_buf
    state.capture_buf = []
    if MEM_DIALOGUE_ONLY:
        msgs = [_dialogue_only(m) for m in msgs]
    full = "\n".join(m for m in msgs if m.strip()).strip()
    if not full:
        return 0
    chunks = [full[i:i + REMEMBER_MAX_CHARS] for i in range(0, len(full), REMEMBER_MAX_CHARS)]
    if len(chunks) == 1:
        items = ["【口述回忆】" + chunks[0]]
    else:
        items = ["【口述回忆 %d/%d】%s" % (i + 1, len(chunks), c) for i, c in enumerate(chunks)]
    add_memories("users", state.capture_uid, state.capture_name, items)
    return len(items)


async def handle_capture(ws, text, name, user_id, state, sender_func, target, tag):
    """连续记忆模式。返回 True 表示这条消息已被记忆模式处理（外层应直接 return，不再正常接话）。"""
    if not MEM_ENABLED:
        return False
    # 结束记忆模式：把攒下的原话整段落盘
    if state.capturing and CAPTURE_END_RE.match(text):
        n = _flush_capture(state)
        state.capturing = False
        state.capture_uid = None
        state.capture_name = ""
        msg = ("（记好啦，这段我都记住了～存了 " + str(n) + " 条）") if n else "（咦，刚才好像没收到要记的内容）"
        await sender_func(ws, target, msg)
        print(tag + " 退出记忆模式，写入 " + str(n) + " 条")
        _drop_current_message(state)  # 「记完了」这条指令本身不计入记录
        return True
    # 进入记忆模式
    if not state.capturing and CAPTURE_START_RE.match(text):
        state.capturing = True
        state.capture_uid = user_id
        state.capture_name = name
        state.capture_buf = []
        await sender_func(ws, target, "（好，我开始记了，你慢慢一条条发，发完跟我说「记完了」就行～）")
        print(tag + " 进入记忆模式 by " + name)
        _drop_current_message(state)  # 「开始记忆」这条指令本身不计入记录
        return True
    # 记忆模式进行中：只收录发起人的话，期间这个聊天一律不正常接话
    if state.capturing:
        if str(user_id) == str(state.capture_uid):
            state.capture_buf.append(text)
            if state.extract_buf:
                state.extract_buf.pop()   # 这条已被记忆模式逐字收录，别让自动提炼(途径2)再摘要一遍
            if len(state.capture_buf) >= CAPTURE_MAX_BUF:
                n = _flush_capture(state)
                await sender_func(ws, target, "（内容有点多，先帮你存了 " + str(n) + " 条，继续发或说「记完了」）")
        return True
    return False


# ============ 时间感知（时钟系统） ============
# 给模型一个"时钟"：提示词里注入当前时间，聊天记录每条带发送时间，记忆条目盖日期戳。
# 防止模型把"明天要做的事"当成已经完成、或对消息间隔没概念。
def now_str():
    t = time.localtime()
    wd = "一二三四五六日"[t.tm_wday]
    return time.strftime("%Y-%m-%d %H:%M", t) + " 星期" + wd


def fmt_ts(ts):
    """聊天记录行首的时间前缀；老记录没有时间戳就不加。"""
    if not ts:
        return ""
    return time.strftime("[%m-%d %H:%M] ", time.localtime(ts))


_DATE_PREFIX_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2}\]\s*")


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    tmp.replace(path)


def _mem_file(kind, ident) -> Path:
    return MEM_DIR / kind / (str(ident) + ".json")


def load_memories(kind, ident) -> dict:
    path = _mem_file(kind, ident)
    if path.exists():
        try:
            data = load_json(str(path))
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return data
        except Exception:
            pass
    return {"name": "", "items": []}


def add_memories(kind, ident, name, new_items):
    """把新记忆合并进文件：去重、超出上限时淘汰最旧的。"""
    new_items = [str(x).strip() for x in new_items if str(x).strip()]
    if not new_items:
        return
    data = load_memories(kind, ident)
    if name:
        data["name"] = name
    for item in new_items:
        bare = _DATE_PREFIX_RE.sub("", item)
        # 同内容旧条目（不论日期）去掉，再以今天的日期重新记入（挪到最新）
        data["items"] = [x for x in data["items"] if _DATE_PREFIX_RE.sub("", x) != bare]
        data["items"].append(time.strftime("[%Y-%m-%d] ") + bare)
    data["items"] = data["items"][-MEM_MAX_ITEMS:]
    _save_json(_mem_file(kind, ident), data)
    print("[记忆] 已记录(" + kind + "/" + str(ident) + "): " + "；".join(new_items))


def _bigrams(s):
    """把一段中文/文本拆成相邻两字的集合，用来做无需分词的相似度比较。"""
    s = re.sub(r"\s+", "", _DATE_PREFIX_RE.sub("", s))
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _select_relevant(items, query, k):
    """C：按与当前聊天内容的相关度，从 items 里挑最相关的 k 条。
    - 条目很少（<=k）时直接全给，最稳、也不费多少 token；
    - 否则用字符 bigram 重叠度打分（覆盖了 query 多少），相关度相同再按时间靠后优先；
    - query 为空 / 全都没相关度时，自然退化成「取最近 k 条」。
    返回结果按原始时间顺序排列，读起来更自然。"""
    if len(items) <= k:
        return items
    qb = _bigrams(query or "")
    scored = []
    for idx, it in enumerate(items):
        if qb:
            ib = _bigrams(it)
            rel = (len(qb & ib) / len(qb)) if ib else 0.0
        else:
            rel = 0.0
        scored.append((rel, idx, it))   # idx 越大越新，用作相关度相同时的次级排序
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    chosen = sorted(scored[:k], key=lambda x: x[1])  # 选出后按时间顺序还原
    return [it for _, _, it in chosen]


def memory_block(group_id=None, speaker_id=None, speaker_name="", query=""):
    """组装注入提示词的记忆片段；按与当前话题的相关度召回最相关的几条（省 token 又能想起对的事）。"""
    if not MEM_ENABLED:
        return ""
    parts = []
    if group_id is not None:
        items = _select_relevant(load_memories("groups", group_id)["items"], query, MEM_INJECT_ITEMS)
        if items:
            parts.append("关于这个群：" + "；".join(items))
    if speaker_id is not None:
        items = _select_relevant(load_memories("users", speaker_id)["items"], query, MEM_INJECT_ITEMS)
        if items:
            parts.append("关于「" + speaker_name + "」：" + "；".join(items))
        # 全程记录的存档：按当前话题相关度，再挑几条你俩相处的点滴（即使开关后来关了，旧存档仍可用）
        arch = _select_relevant(load_memories("archive_u", speaker_id)["items"], query, ARCHIVE_INJECT)
        if arch:
            parts.append("你和「" + speaker_name + "」相处的点滴（挑了和当前话题相关的）：" + "；".join(arch))
    if not parts:
        return ""
    return ("\n【你的长期记忆（可自然地用上，但别生硬复述、别一次全提；"
            "条目开头的[日期]是记下这件事的时间，可据此判断是最近还是很久以前）】\n"
            + "\n".join(parts) + "\n")


class GroupState:
    def __init__(self, kind="group", ident=""):
        self.kind = kind          # "group" 或 "private"
        self.ident = str(ident)
        self.history = []
        self.last_reply_ts = 0.0
        self.user_ids = {}        # 昵称 -> QQ号，提炼记忆时用
        self.extract_buf = []     # 攒给记忆提炼用的消息，攒够一批提炼一次后清空
        self.active_until = 0.0   # 活跃窗口截止时间戳
        self.active_msgs_left = 0  # 活跃窗口剩余可浏览条数
        self.last_meme_ts = 0.0   # 上次发表情包的时间戳（控制表情频率）
        self.muted_until = 0.0    # 禁言截止时间戳（0 表示未被禁言）
        self.capturing = False    # 是否处于「连续记忆模式」
        self.capture_uid = None   # 记忆模式的发起人 QQ（只收录他的话）
        self.capture_name = ""    # 发起人昵称
        self.capture_buf = []     # 记忆模式期间攒下的原话，结束时整段写入长期记忆
        self._hist_path = MEM_DIR / "history" / (kind + "_" + str(ident) + ".json")
        self._load_history()

    def activate(self):
        """被 @ / 喊昵称 / 提到关键词时调用：开启（或刷新）活跃窗口。"""
        self.active_until = time.time() + ACT_MINUTES * 60
        self.active_msgs_left = ACT_MAX_MSGS

    def deactivate(self):
        """关闭活跃窗口，回到非活跃期（禁言/解禁后调用）。"""
        self.active_until = 0.0
        self.active_msgs_left = 0

    def consume_active(self):
        """活跃窗口内每来一条消息消耗一条配额；返回当前是否仍处于活跃期。"""
        if time.time() > self.active_until or self.active_msgs_left <= 0:
            return False
        self.active_msgs_left -= 1
        return True

    def _load_history(self):
        if self._hist_path.exists():
            try:
                saved = load_json(str(self._hist_path))
                self.history = [(h[0], h[1], h[2] if len(h) > 2 else 0)
                                for h in saved.get("history", []) if len(h) >= 2]
                self.user_ids = saved.get("user_ids", {})
            except Exception:
                pass

    def _save_history(self):
        try:
            _save_json(self._hist_path, {"history": self.history, "user_ids": self.user_ids})
        except Exception as e:
            print("[历史保存失败] " + str(e))

    def add(self, name, text, limit):
        self.history.append((name, text, time.time()))
        if len(self.history) > limit:
            self.history = self.history[-limit:]
        self.extract_buf.append((name, text))
        self._save_history()


GROUPS = {}
PRIVATES = {}


def get_state(group_id):
    if group_id not in GROUPS:
        GROUPS[group_id] = GroupState("group", group_id)
    return GROUPS[group_id]


def get_private_state(user_id):
    if user_id not in PRIVATES:
        PRIVATES[user_id] = GroupState("private", user_id)
    return PRIVATES[user_id]


def extract_text_and_at(message):
    self_id = str(CARD["self_id"])
    text_parts = []
    at_me = False
    if isinstance(message, str):
        return message, False
    for seg in message:
        seg_type = seg.get("type")
        data = seg.get("data", {})
        if seg_type == "text":
            text_parts.append(data.get("text", ""))
        elif seg_type == "at":
            if str(data.get("qq")) == self_id:
                at_me = True
    return "".join(text_parts).strip(), at_me


def sender_name(event):
    sender = event.get("sender", {})
    return sender.get("card") or sender.get("nickname") or str(event.get("user_id"))


def decide_reply(text, at_me, state):
    """返回 at/name/keyword（必回，并激活活跃窗口）、judge（交给模型判断）、或 None（不处理）。"""
    if at_me:
        state.activate()
        return "at"
    for nick in CARD.get("nicknames", []):
        if nick and nick in text:
            state.activate()
            return "name"
    for kw in CARD.get("keywords", []):
        if kw and kw in text:
            state.activate()
            return "keyword"
    # 活跃窗口内：每条都交给模型判断；只保留一个很短的反刷屏地板
    if state.consume_active():
        gap = time.time() - state.last_reply_ts
        if gap < CARD.get("judge_min_gap", 3):
            return None
        return "judge"
    # 非活跃期：绝大多数消息直接忽略（完全不调用 API），小概率随机抽查
    if random.random() < IDLE_JUDGE_PROB:
        return "judge"
    return None


def _time_hint(gap):
    cooldown = CARD.get("cooldown_seconds", 5)
    window = CARD.get("engagement", {}).get("window_seconds", 90)
    if gap < cooldown:
        return "（你刚刚才发过言，这种时候请尽量保持安静，除非这条明显在跟你互动、或有人急需你，否则一律 [skip]。）"
    elif gap < window:
        return "（你不久前说过话了，别太黏。只有当这条确实点到你、或有人需要你时才接，其余 [skip]。）"
    else:
        return "（你有一阵子没出声了。若这条明显和你有关、或有人需要回应，可以自然接一句；只是和你无关的普通闲聊就 [skip]。）"


def list_personas():
    """扫描 characters/personas/<角色>/ 下的人设文件，返回 {名字: 路径}。每次调用都重扫，加文件不用重启。"""
    if not PERSONA_DIR.is_dir():
        return {}
    return {p.stem: p for p in sorted(PERSONA_DIR.iterdir())
            if p.is_file() and p.suffix.lower() in (".txt", ".md")
            and not p.stem.startswith("_")}  # 下划线开头的当说明文件，不计入人设列表


def get_persona():
    """当前生效的人设文本：切换过就用切换的那套，否则用角色卡里的默认。"""
    return _active_persona_text if _active_persona_text else CARD["persona"]


def _persona_state_file():
    return MEM_DIR / "active_persona.txt"


def _apply_persona(name):
    """切换到名为 name 的人设。返回 (是否成功, 实际应用的名字)。"""
    global _active_persona_name, _active_persona_text
    name = (name or "").strip()
    if name in ("默认", "原始", "原版", "默认人设", "原始人设"):
        _active_persona_name, _active_persona_text = None, None
        try:
            _persona_state_file().parent.mkdir(parents=True, exist_ok=True)
            _persona_state_file().write_text("", encoding="utf-8")
        except Exception:
            pass
        return True, "默认"
    files = list_personas()
    match = None
    for k in files:                       # 先精确（忽略大小写）
        if k == name or k.lower() == name.lower():
            match = k
            break
    if not match:                         # 再包含匹配兜底
        for k in files:
            if name in k or k in name:
                match = k
                break
    if not match:
        return False, None
    try:
        txt = files[match].read_text(encoding="utf-8").strip()
    except Exception as e:
        print("[人设] 读取失败 " + str(files[match]) + ": " + str(e))
        return False, None
    if not txt:
        return False, None
    _active_persona_name, _active_persona_text = match, txt
    try:
        _persona_state_file().parent.mkdir(parents=True, exist_ok=True)
        _persona_state_file().write_text(match, encoding="utf-8")
    except Exception:
        pass
    return True, match


def _load_active_persona_on_start():
    """启动时恢复上次切换的人设（重启不丢）。"""
    f = _persona_state_file()
    if f.exists():
        try:
            name = f.read_text(encoding="utf-8").strip()
        except Exception:
            name = ""
        if name:
            ok, applied = _apply_persona(name)
            if ok:
                print("[人设] 已恢复上次的人设：" + applied)


async def handle_persona_command(ws, text, state, sender_func, target, tag):
    """处理人设查看/切换指令。返回 True 表示这条是指令、已处理（外层应直接 return）。"""
    if PERSONA_LIST_RE.match(text):
        names = list(list_personas().keys())
        cur = _active_persona_name or "默认（角色卡）"
        if names:
            msg = "可切换的人设：" + "、".join(names) + "。当前：" + cur + "。说「切换人设 名字」即可。"
        else:
            msg = "（还没放人设文件。去 characters/personas/" + CARD.get("name", "") + "/ 里放 .txt，每个文件一整套人设。）当前：" + cur
        await sender_func(ws, target, msg)
        _drop_current_message(state)  # 查看人设指令不计入记录
        return True
    m = PERSONA_SWITCH_RE.match(text)
    if m:
        ok, applied = _apply_persona(m.group(1))
        if ok:
            await sender_func(ws, target, "（好，已切换到「" + applied + "」人设～）")
            print(tag + " 切换人设 -> " + applied)
        else:
            names = "、".join(list_personas().keys()) or "（暂无）"
            await sender_func(ws, target, "（没找到「" + m.group(1).strip() + "」这套人设。现有：" + names + "）")
        _drop_current_message(state)  # 切换人设指令不计入记录
        return True
    return False


def _persona_rules(name, state=None):
    return (
        "你叫「" + name + "」，正在 QQ 群里和群友们一起水群闲聊。\n"
        "你的人物设定：\n" + get_persona() + "\n\n"
        "【最重要：像真人水群，不要像 AI 助手】\n"
        "1. 你在用手机打字水群，不是客服也不是助手。绝对不要主动提供帮助、不要问『要不要我帮你』『有什么需要』、不要做总结、不要解释自己。\n"
        "2. 消息要短！大多数时候只发几个字到一句话，偶尔才长一点。长短随机，别每条都两三句那么工整。\n"
        "3. 说话要碎、口语、随意：可以省略句号，可以用语气词、网络梗、偶尔颜文字，但要符合人设。\n"
        "4. 不用回应对方说的全部内容，挑一个点接话或吐槽就行，可以答非所问、可以只甩一句。\n"
        "5. 严格保持上面的人设性格，但用群聊口吻，别文绉绉、别像写作文。\n"
        "6. 想多说就用换行拆成两三条很短的消息，别挤成一大段。\n"
        "7. 只输出要发的消息本身，不要加引号、不要写旁白、不要带名字前缀、不要写括号里的动作，"
        "也【绝对不要】写出 [月-日 时:分] 这类时间戳——那只是系统给你参考时间用的，不是让你说出来的。\n"
        + MEMO_PROMPT
        + meme_block(state)
    )


def _conv_user(state, name):
    hist = list(state.history)
    latest = hist[-1] if hist else ("", "", 0)
    earlier = hist[:-1]
    lines = [fmt_ts(h[2] if len(h) > 2 else 0) + h[0] + "：" + h[1] for h in earlier]
    transcript = "\n".join(lines) if lines else "（暂无）"
    return (
        "现在的时间是 " + now_str() + "。\n"
        "聊天记录每行开头的 [月-日 时:分] 是那条消息的发送时间，请据此判断事件先后和时间间隔：\n"
        "- 别人几分钟前说『明天/之后』要做的事，现在依然还没发生，绝对不要当成已经完成去问结果；\n"
        "- 也别凭感觉以为过了很久，先看时间差再说话。\n\n"
        "群里之前的聊天记录(仅作背景参考)：\n" + transcript + "\n\n"
        "【最新一条】" + fmt_ts(latest[2] if len(latest) > 2 else 0) + latest[0] + "：" + latest[1] + "\n\n"
        "请紧跟当前话题，不要老揪着旧话题或你自己说过的话反复打转。"
    )


def _build_private_prompt(state, name, speaker_id=None, speaker_name=""):
    system = (
        "你叫「" + name + "」，正在 QQ 上和一个网友一对一私聊。\n"
        "你的人物设定：\n" + get_persona() + "\n"
        + memory_block(speaker_id=speaker_id, speaker_name=speaker_name,
                       query=_recent_text(state)) + "\n"
        "【像真人私聊，不要像 AI 助手】\n"
        "1. 你在用手机打字聊天，不是客服/助手。不要主动提供帮助、不要问『要不要我帮你』、不要做总结、不要解释自己。\n"
        "2. 消息短、口语、随意，长短随机，可用语气词和网络梗，但要符合人设。\n"
        "3. 严格保持人设性格，自然回应对方，别冷场、别文绉绉。\n"
        "4. 想多说就换行拆成两三条短消息。\n"
        "5. 只输出要发的消息本身，不要加引号、旁白、名字前缀或括号动作，"
        "也【绝对不要】写出 [月-日 时:分] 这类时间戳——那只是给你参考时间的，不是让你说出来的。\n"
        + MEMO_PROMPT
        + meme_block(state)
    )
    hist = list(state.history)
    latest = hist[-1] if hist else ("", "", 0)
    earlier = hist[:-1]
    lines = [fmt_ts(h[2] if len(h) > 2 else 0) + h[0] + "：" + h[1] for h in earlier]
    transcript = "\n".join(lines) if lines else "（暂无）"
    user = (
        "现在的时间是 " + now_str() + "。记录行首的 [月-日 时:分] 是消息发送时间，"
        "请据此判断先后与间隔：对方几分钟前说『明天/之后』要做的事现在还没发生，"
        "不要当成已经完成去问结果。\n\n"
        "你们私聊的之前记录(仅作背景参考)：\n" + transcript + "\n\n"
        "【对方最新一条】" + fmt_ts(latest[2] if len(latest) > 2 else 0) + latest[0] + "：" + latest[1] + "\n\n"
        "请以「" + name + "」的身份回应这条最新消息，紧跟当前话题自然聊。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _recent_text(state, n=3):
    """取最近 n 条消息的文本拼起来，作为「当前话题」给记忆召回打分用。"""
    return " ".join(h[1] for h in list(state.history)[-n:] if len(h) > 1 and h[1])


def build_prompt(state, trigger, private=False, gap=0.0,
                 group_id=None, speaker_id=None, speaker_name=""):
    name = CARD["name"]
    if private:
        return _build_private_prompt(state, name, speaker_id, speaker_name)
    system = _persona_rules(name, state)
    system += memory_block(group_id=group_id, speaker_id=speaker_id,
                           speaker_name=speaker_name, query=_recent_text(state))
    if trigger == "judge":
        system += (
            "8. 现在没有人直接 @ 你或喊你名字。【默认就是不接话、输出 [skip]】，这是常态。\n"
            "只有当【非常明确地】满足下面某一条时，你才接话：\n"
            "  - 这条消息在直接跟你互动、回应你、或在问你；\n"
            "  - 明确点到了你人设里特别在意、特别专属的事；\n"
            "  - 有人情绪低落、求助、或明显需要有人接住。\n"
            "下列情况一律 [skip]，不要接：别人之间在对话、闲聊与你无关、只是普通玩笑或刷屏、"
            "你只是觉得'好像可以接一句'、或者你只是想刷存在感。判断不了就 [skip]。\n"
            + _time_hint(gap) +
            " 真的要接时，只回很短的一两句，自然随意。\n"
        )
    else:
        system += "8. 有人直接 @ 你、喊你名字、或聊到你在意的话题——自然接话回应，像被叫到的真人那样搭一句，别冷场。\n"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": _conv_user(state, name)},
    ]


async def _deepseek_call(messages, max_tokens=None, temperature=None):
    url = CONFIG["deepseek_base_url"].rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": "Bearer " + CONFIG["deepseek_api_key"],
        "Content-Type": "application/json",
    }
    payload = {
        "model": CARD.get("model", CONFIG.get("deepseek_model", "deepseek-chat")),
        "messages": messages,
        "temperature": temperature if temperature is not None else CONFIG.get("temperature", 1.1),
        "max_tokens": max_tokens if max_tokens is not None else CARD.get("max_tokens", CONFIG.get("max_tokens", 100)),
    }
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print("[DeepSeek 调用失败] " + str(e))
        return None


REMEMBER_MAX_CHARS = MEM_CFG.get("remember_max_chars", 4000)  # 单条手动记忆的字数上限（防滥用，给得很宽）


def remember_command(text, name, user_id, group_id=None):
    """有人对机器人说"记住xxx"时写入记忆。返回是否命中。
    群聊里说「群记住xxx」「记住群里/这个群/本群的xxx」→ 写入这个群的群记忆；
    普通「记住xxx」「别忘了xxx」→ 写入说话人的个人记忆。
    注意：用 re.S 让 . 跨行，且不再砍到 80 字——「记住」后面的多行原文【整段】存进去，
    这样粘贴一长段故事时不会只剩第一句。"""
    if not MEM_ENABLED:
        return False
    if group_id is not None:
        m = re.search(r"(?:群记住|记住(?:群里|这个群|本群)的?)[，,。:：!！\s]*(.{2,})", text, re.S)
        if m:
            content = m.group(1).strip()[:REMEMBER_MAX_CHARS]
            add_memories("groups", group_id, "", [name + "让你记住关于这个群的事：" + content])
            return True
    m = re.search(r"(?:记住|别忘了)[，,。:：!！\s]*(.{2,})", text, re.S)
    if not m:
        return False
    content = m.group(1).strip()[:REMEMBER_MAX_CHARS]
    add_memories("users", user_id, name, [name + "让你记住：" + content])
    return True


async def extract_memories(state, group_id, batch):
    """低频后台任务：让模型从最近聊天里提炼值得长期记住的事，写入记忆文件。"""
    if len(batch) < 6:
        return
    transcript = "\n".join(n + "：" + t for n, t in batch)
    system = (
        "你是记忆提炼器。从聊天记录中提炼【值得长期记住】的信息，分两类：\n"
        "- group：关于这个群整体的事——群里流行的梗和黑话、群规和约定、群友之间的关系、"
        "群里发生的重要事件、大家最近共同关心的话题等。只要对融入这个群有帮助就值得记。\n"
        "- users：关于某个人的事——喜好、身份、经历、近况、和别人的约定等。\n"
        "忽略寒暄、刷屏等没有长期价值的内容。每条记忆用一句简短的话。\n"
        "只输出 JSON，格式：{\"group\": [\"群相关的事\"], \"users\": {\"昵称\": [\"关于此人的事\"]}}\n"
        "没有值得记的就输出 {\"group\": [], \"users\": {}}。不要输出其他任何文字。"
    )
    raw = await _deepseek_call(
        [{"role": "system", "content": system},
         {"role": "user", "content": "聊天记录：\n" + transcript}],
        max_tokens=500, temperature=0.2)
    if not raw:
        return
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("[记忆提炼] 模型输出不是合法 JSON，本次跳过")
        return
    bot_name = CARD["name"]
    if group_id is not None:
        add_memories("groups", group_id, "", data.get("group", []) or [])
    users = data.get("users", {}) or {}
    for nick, items in users.items():
        if nick == bot_name:
            continue  # 不记机器人自己说的
        uid = state.user_ids.get(nick)
        if uid and items:
            add_memories("users", uid, nick, items)


def maybe_extract(state, group_id=None):
    """每攒够 MEM_EXTRACT_EVERY 条消息，触发一次后台提炼（不阻塞回复）。"""
    if not MEM_ENABLED:
        return
    if len(state.extract_buf) >= MEM_EXTRACT_EVERY:
        batch = state.extract_buf
        state.extract_buf = []
        asyncio.create_task(extract_memories(state, group_id, batch))


async def ask_deepseek(messages):
    """返回要发的话；模型输出 [skip]（判断不接话）或接口失败时返回 None。"""
    reply = await _deepseek_call(messages)
    if not reply:
        return None
    reply = reply.replace("[skip]", "").strip()
    if not reply:
        return None
    # 兜底：模型有时会把聊天记录里的时间戳/名字前缀也抄进回复，这里逐行剥掉（防它"说出"时间戳）
    reply = re.sub(r"(?m)^\s*[\[【]\s*(?:\d{2,4}[-/])?\d{1,2}[-/]\d{1,2}\s+\d{1,2}[:：]\d{2}\s*[\]】]\s*", "", reply)  # [06-14 01:06]
    reply = re.sub(r"(?m)^\s*[\[【]\s*\d{1,2}[:：]\d{2}\s*[\]】]\s*", "", reply)                                       # [01:06]
    reply = re.sub(r"(?m)^\s*" + re.escape(CARD["name"]) + r"\s*[:：]\s*", "", reply)                                  # 名字：前缀
    reply = reply.strip()
    if not reply:
        return None
    prefix = CARD["name"] + "："
    if reply.startswith(prefix):
        reply = reply[len(prefix):].strip()
    return reply


async def send_group_msg(ws, group_id, text):
    payload = {"action": "send_group_msg",
               "params": {"group_id": group_id, "message": text},
               "echo": "send_" + str(int(time.time() * 1000))}
    await ws.send(json.dumps(payload))


async def send_private_msg(ws, user_id, text):
    payload = {"action": "send_private_msg",
               "params": {"user_id": user_id, "message": text},
               "echo": "send_" + str(int(time.time() * 1000))}
    await ws.send(json.dumps(payload))


async def bubble_delay(text, first):
    cfg = CARD.get("typing_delay", {})
    per_char = cfg.get("per_char", 0.15)
    mx = cfg.get("max", 4.0)
    if first:
        pause = random.uniform(cfg.get("first_min", 0.1), cfg.get("first_max", 0.7))
    else:
        pause = random.uniform(cfg.get("gap_min", 0.5), cfg.get("gap_max", 1.3))
    typing = len(text) * per_char * random.uniform(0.75, 1.25)
    await asyncio.sleep(min(pause + typing, mx))


_PAREN_RE = re.compile(r"（[^（）]*）|\([^()]*\)")


def _dialogue_only(text):
    """去掉括号里的动作/旁白（全角（）和半角()），只留对话；整行都是动作就丢掉。"""
    if not text:
        return ""
    out = []
    for line in text.split("\n"):
        cleaned = line
        while _PAREN_RE.search(cleaned):     # 反复清，处理一行多个/嵌套括号
            cleaned = _PAREN_RE.sub("", cleaned)
        cleaned = cleaned.strip()
        if cleaned:
            out.append(cleaned)
    return "\n".join(out)


def _store_precious(state, speaker_id, speaker_name, notes, reply=""):
    """她打了 [铭记] 标记：把这段珍贵的话存进长期记忆。
    会同时收录【对方最近一条原话】和【她自己这条回复】——因为珍贵往事既可能是对方讲的，
    也可能是她自己回忆出来的。默认只存对话、去掉括号里的动作（MEM_DIALOGUE_ONLY）。"""
    if not MEM_ENABLED or speaker_id is None:
        return
    last_user = ""
    for h in reversed(state.history):   # 取最近一条「不是机器人自己」的原话
        if h[0] != CARD["name"] and h[1] and not h[1].startswith("[表情:"):
            last_user = h[1]
            break
    her = MEME_TAG_RE.sub("", MEMO_TAG_RE.sub("", reply or ""))  # 她这条回复（去掉各种标记）
    if MEM_DIALOGUE_ONLY:
        last_user = _dialogue_only(last_user)
        her = _dialogue_only(her)
    body = "\n".join(p for p in (last_user.strip(), her.strip()) if p).strip()
    note = " ".join(n.strip() for n in notes if n and n.strip()).strip()
    if not body and not note:
        return
    head = ("【珍贵回忆·" + note[:30] + "】") if note else "【珍贵回忆】"
    item = head + (body if body else note)[:REMEMBER_MAX_CHARS]
    add_memories("users", speaker_id, speaker_name, [item])
    print("[记忆] 她主动铭记了一段珍贵回忆 -> " + str(speaker_id))


def _archive_add(ident, name, items):
    """写入「全程记录」专用存档：独立文件 archive_u/<id>.json，上限是 ARCHIVE_MAX（远大于普通记忆的 40）。"""
    items = [str(x).strip() for x in items if str(x).strip()]
    if not items:
        return
    data = load_memories("archive_u", ident)
    if name:
        data["name"] = name
    for it in items:
        bare = _DATE_PREFIX_RE.sub("", it)
        data["items"] = [x for x in data["items"] if _DATE_PREFIX_RE.sub("", x) != bare]
        data["items"].append(time.strftime("[%Y-%m-%d] ") + bare)
    data["items"] = data["items"][-ARCHIVE_MAX:]
    _save_json(_mem_file("archive_u", ident), data)


def _archive_exchange(state, speaker_id, speaker_name, reply):
    """全程记录开启时：把这一轮（对方原话 + 她的回复，默认去掉括号动作）作为一条存进存档。"""
    if not (MEM_ENABLED and ARCHIVE_ALL) or speaker_id is None:
        return
    last_user = ""
    for h in reversed(state.history):
        if h[0] != CARD["name"] and h[1] and not h[1].startswith("[表情:"):
            last_user = h[1]
            break
    her = MEME_TAG_RE.sub("", MEMO_TAG_RE.sub("", reply or ""))
    if MEM_DIALOGUE_ONLY:
        last_user = _dialogue_only(last_user)
        her = _dialogue_only(her)
    parts = []
    if last_user.strip():
        parts.append(speaker_name + "：" + last_user.strip())
    if her.strip():
        parts.append(CARD["name"] + "：" + her.strip())
    if not parts:
        return
    _archive_add(speaker_id, speaker_name, ["\n".join(parts)])


async def _send_reply(ws, reply, sender_func, target, state, speaker_id=None, speaker_name=""):
    max_bubbles = max(1, CARD.get("max_bubbles", 3))
    max_memes = MEME_CFG.get("max_per_reply", 1)
    ctx = CARD.get("context_size", 16)
    # 她自己标记的「珍贵回忆」：存下对方刚才的原话，并把 [铭记] 标记从可见消息里去掉
    memo_hits = MEMO_TAG_RE.findall(reply)
    if memo_hits:
        _store_precious(state, speaker_id, speaker_name, memo_hits, reply=reply)
        reply = MEMO_TAG_RE.sub("", reply)
    # 全程记录：把这一轮对话（对方原话 + 她的回复，去动作）自动存进专用存档（开关关时此调用直接返回）
    _archive_exchange(state, speaker_id, speaker_name, reply)
    # 先把回复拆成「文字气泡」和「表情标记」两份
    text_bubbles = []
    meme_tags = []
    for line in (b.strip() for b in reply.split("\n") if b.strip()):
        meme_tags += MEME_TAG_RE.findall(line)
        t = MEME_TAG_RE.sub("", line).strip()
        if t:
            text_bubbles.append(t)
    # 超过 max_bubbles 时：把多出来的部分【合并进最后一条】（用换行连起来），而不是丢弃——
    # 这样消息条数受控（防风控），但内容一条不丢（长故事完整送达）。
    if len(text_bubbles) > max_bubbles:
        merged_tail = "\n".join(text_bubbles[max_bubbles - 1:])
        text_bubbles = text_bubbles[:max_bubbles - 1] + [merged_tail]
    # 逐条发送文字
    for i, text in enumerate(text_bubbles):
        await bubble_delay(text, first=(i == 0))
        await sender_func(ws, target, text)
        state.add(CARD["name"], text, ctx)
    # 表情包：纯附加，不占文字条数，受 max_memes 和最小间隔闸门约束
    memes_sent = 0
    for tag in meme_tags:
        if memes_sent >= max_memes:
            break
        # 频率闸门②：就算模型写了标记，距上次发表情太近也强制不发
        if time.time() - state.last_meme_ts < MEME_MIN_INTERVAL:
            break
        seg = meme_segment(tag)
        if seg is None:
            print("[表情] 找不到表情包「" + tag + "」，已跳过")
            continue
        await asyncio.sleep(random.uniform(0.4, 1.2))
        await sender_func(ws, target, seg)
        state.add(CARD["name"], "[表情:" + tag.strip() + "]", ctx)
        state.last_meme_ts = time.time()
        memes_sent += 1


async def handle_group_message(ws, event):
    group_id = event["group_id"]
    enabled = CARD.get("enabled_groups")
    if enabled and group_id not in enabled:
        return
    text, at_me = extract_text_and_at(event.get("message", ""))
    if not text and not at_me:
        return
    name = sender_name(event)
    user_id = event.get("user_id")
    state = get_state(group_id)
    state.user_ids[name] = user_id
    state.add(name, text, CARD.get("context_size", 16))
    if await handle_mute_command(ws, text, state, send_group_msg, group_id,
                                 "[群" + str(group_id) + "]"):
        return
    if await handle_capture(ws, text, name, user_id, state, send_group_msg, group_id,
                            "[群" + str(group_id) + "]"):
        return
    if await handle_persona_command(ws, text, state, send_group_msg, group_id, "[群" + str(group_id) + "]"):
        return
    maybe_extract(state, group_id)
    trigger = decide_reply(text, at_me, state)
    if trigger is None:
        return
    if trigger in ("at", "name") and remember_command(text, name, user_id, group_id):
        print("[群" + str(group_id) + "] " + name + " 让我记住了一件事")
    gap = time.time() - state.last_reply_ts
    print("[群" + str(group_id) + "] 触发(" + trigger + ") <- " + name + ": " + text)
    reply = await ask_deepseek(build_prompt(state, trigger, gap=gap,
                                            group_id=group_id,
                                            speaker_id=user_id, speaker_name=name))
    if reply is None:
        if trigger == "judge":
            print("[群" + str(group_id) + "] 判断后选择不接话")
        else:
            print("[群" + str(group_id) + "] 接口无返回(检查key/余额/网络)")
        return
    await _send_reply(ws, reply, send_group_msg, group_id, state,
                      speaker_id=user_id, speaker_name=name)
    state.last_reply_ts = time.time()
    print("[群" + str(group_id) + "] " + CARD["name"] + " 已回复: " + reply)


async def handle_private_message(ws, event):
    if not CARD.get("reply_private", True):
        return
    user_id = event["user_id"]
    text, _ = extract_text_and_at(event.get("message", ""))
    if not text:
        return
    name = sender_name(event)
    state = get_private_state(user_id)
    state.user_ids[name] = user_id
    state.add(name, text, CARD.get("context_size", 16))
    if await handle_mute_command(ws, text, state, send_private_msg, user_id,
                                 "[私聊 " + str(user_id) + "]"):
        return
    if await handle_capture(ws, text, name, user_id, state, send_private_msg, user_id,
                            "[私聊 " + str(user_id) + "]"):
        return
    if await handle_persona_command(ws, text, state, send_private_msg, user_id, "[私聊 " + str(user_id) + "]"):
        return
    maybe_extract(state)
    if remember_command(text, name, user_id):
        print("[私聊 " + str(user_id) + "] " + name + " 让我记住了一件事")
    print("[私聊 " + str(user_id) + "] <- " + name + ": " + text)
    reply = await ask_deepseek(build_prompt(state, "private", private=True,
                                            speaker_id=user_id, speaker_name=name))
    if reply is None:
        print("[私聊 " + str(user_id) + "] 接口无返回(检查key/余额/网络)")
        return
    await _send_reply(ws, reply, send_private_msg, user_id, state,
                      speaker_id=user_id, speaker_name=name)
    print("[私聊 " + str(user_id) + "] " + CARD["name"] + " 已回复: " + reply)


async def run():
    ws_url = CARD["napcat"]["ws_url"]
    token = CARD["napcat"].get("access_token", "")
    headers = {"Authorization": "Bearer " + token} if token else {}
    print("机器人「" + CARD["name"] + "」启动，正在连接 NapCat: " + ws_url)
    _load_active_persona_on_start()
    while True:
        try:
            async with websockets.connect(ws_url, additional_headers=headers, ping_interval=30) as ws:
                print("长期记忆版已连上 NapCat，开始监听消息……")
                async for raw in ws:
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if event.get("post_type") == "message":
                        if str(event.get("user_id")) == str(CARD["self_id"]):
                            continue
                        if event.get("message_type") == "group":
                            asyncio.create_task(handle_group_message(ws, event))
                        elif event.get("message_type") == "private":
                            asyncio.create_task(handle_private_message(ws, event))
        except Exception as e:
            print("[连接断开] " + str(e) + "，5 秒后重连……")
            await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n机器人已手动停止。")
