#!/usr/bin/env bash
# 拟人QQ机器人 一键部署(自动生成所有文件 + 安装)
set -e
echo "==== 创建 qqbot 文件 ===="
mkdir -p "$HOME/qqbot/characters"
cd "$HOME/qqbot"
cat > bot.py <<'BOT_PY_EOF'
# -*- coding: utf-8 -*-
"""
拟人 QQ 群聊机器人 —— 角色卡驱动版
======================================

一个机器人 = 一张角色卡(characters/xxx.json) + 一个 NapCat(QQ 登录端)。
想多养几个机器人，就多写几张角色卡、各开一个 NapCat、各跑一个本程序进程。

运行方式:
    python bot.py characters/傲娇少女_示例.json

依赖:
    pip install -r requirements.txt
"""

import asyncio
import json
import random
import sys
import time
from pathlib import Path

import httpx
import websockets


# ============================================================
# 1. 读取配置
# ============================================================

def load_json(path: str) -> dict:
    """读取一个 JSON 文件并返回字典。"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# 共享配置(DeepSeek 的 key、模型名等)，所有机器人共用同一份
CONFIG = load_json(str(Path(__file__).parent / "config.json"))

# 角色卡路径由命令行第一个参数传入；不传则用示例角色卡
CARD_PATH = sys.argv[1] if len(sys.argv) > 1 else "characters/傲娇少女_示例.json"
CARD = load_json(CARD_PATH)


# ============================================================
# 2. 每个群单独维护的"聊天状态"
# ============================================================

class GroupState:
    """记录某个群最近的聊天上下文，以及机器人的"追聊"状态。"""

    def __init__(self):
        self.history = []          # 最近的消息列表，元素为 (昵称, 文本)
        self.last_reply_ts = 0.0   # 机器人上次在本群发言的时间戳
        self.followups_left = 0    # 还剩几次"主动追聊"机会(插话后用)

    def add(self, name: str, text: str, limit: int):
        self.history.append((name, text))
        # 只保留最近 limit 条，防止上下文越来越长、越来越贵
        if len(self.history) > limit:
            self.history = self.history[-limit:]


GROUPS = {}   # group_id -> GroupState


def get_state(group_id: int) -> "GroupState":
    if group_id not in GROUPS:
        GROUPS[group_id] = GroupState()
    return GROUPS[group_id]


# ============================================================
# 3. 解析 NapCat 发来的群消息事件
# ============================================================

def extract_text_and_at(message):
    """
    从 OneBot 消息段里取出纯文本，并判断是否 @ 了机器人自己。
    返回: (纯文本, 是否被@到自己)
    """
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


def sender_name(event: dict) -> str:
    """优先用群名片，没有就用昵称。"""
    sender = event.get("sender", {})
    return sender.get("card") or sender.get("nickname") or str(event.get("user_id"))


# ============================================================
# 4. 决定"要不要回复"
# ============================================================

def decide_reply(text: str, at_me: bool, state: "GroupState"):
    """
    返回触发原因(at / name / keyword / random / followup)；
    返回 None 表示这条消息不回复。
    """
    now = time.time()

    # (a) 被 @ —— 最高优先级，无视冷却，必回
    if at_me:
        return "at"

    # (b) 文本里叫到了机器人的名字/昵称 —— 必回
    for nick in CARD.get("nicknames", []):
        if nick and nick in text:
            return "name"

    # (c) 命中关键词 —— 必回
    for kw in CARD.get("keywords", []):
        if kw and kw in text:
            return "keyword"

    # 下面是"主动插话"类触发，需要遵守冷却时间，避免刷屏
    if now - state.last_reply_ts < CARD.get("cooldown_seconds", 8):
        return None

    # (d) 追聊窗口内 —— 机器人刚说过话，话题还热，用更高概率继续接话
    eng = CARD.get("engagement", {})
    if state.followups_left > 0 and (now - state.last_reply_ts) < eng.get("window_seconds", 120):
        if random.random() < eng.get("boost_probability", 0.7):
            return "followup"

    # (e) 平时随机插话 —— 小概率主动开口，模拟真人偶尔冒泡
    if random.random() < CARD.get("reply_probability", 0.05):
        return "random"

    return None


# ============================================================
# 5. 调用 DeepSeek 生成"角色化"回复
# ============================================================

def build_prompt(state: "GroupState", trigger: str):
    """把人设 + 最近群聊记录拼成 DeepSeek 的对话消息。"""
    name = CARD["name"]

    system = (
        "你叫「" + name + "」，正在一个热闹的 QQ 群里和大家聊天。\n"
        "你的人物设定：\n" + CARD["persona"] + "\n\n"
        "【发言要求】\n"
        "1. 你是在发 QQ 群消息，要像真人一样：口语化、简短、随意，别像写作文。\n"
        "2. 一般一两句话就够了，别长篇大论。可以用网络用语、语气词。\n"
        "3. 严格保持上面的人设性格和说话风格。\n"
        "4. 只输出你要发的消息内容本身，不要加引号、不要写旁白、不要带你的名字前缀。\n"
        "5. 如果这条消息其实没必要由你来接话(比如和你无关、你也没什么想说的)，"
        "就只回复两个字：[skip]。这样能让你更像真人，不会有问必答。\n"
    )
    if trigger in ("at", "name"):
        system += "6. 有人正在直接叫你或找你说话，请务必正常回应，不要 [skip]。\n"

    lines = []
    for nick, txt in state.history:
        lines.append(nick + "：" + txt)
    transcript = "\n".join(lines)

    user = (
        "这是群里最近的聊天记录：\n" + transcript + "\n\n"
        "现在请你以「" + name + "」的身份，自然地接一句话。"
    )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


async def ask_deepseek(messages):
    """调用 DeepSeek，返回机器人要说的话；如果模型选择 [skip] 则返回 None。"""
    url = CONFIG["deepseek_base_url"].rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": "Bearer " + CONFIG["deepseek_api_key"],
        "Content-Type": "application/json",
    }
    payload = {
        "model": CONFIG.get("deepseek_model", "deepseek-chat"),
        "messages": messages,
        "temperature": CONFIG.get("temperature", 1.1),  # 高一点更有人味、更随机
        "max_tokens": CONFIG.get("max_tokens", 200),
    }
    try:
        async with httpx.AsyncClient(timeout=40) as client:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
            reply = data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print("[DeepSeek 调用失败] " + str(e))
        return None

    if not reply or "[skip]" in reply or reply.lower() == "skip":
        return None
    prefix = CARD["name"] + "："
    if reply.startswith(prefix):
        reply = reply[len(prefix):].strip()
    return reply


# ============================================================
# 6. 通过 NapCat 把消息发出去
# ============================================================

async def send_group_msg(ws, group_id: int, text: str):
    """走 OneBot v11 协议，让 NapCat 把消息发到群里。"""
    payload = {
        "action": "send_group_msg",
        "params": {"group_id": group_id, "message": text},
        "echo": "send_" + str(int(time.time() * 1000)),
    }
    await ws.send(json.dumps(payload))


async def human_typing_delay(text: str):
    """模拟真人打字：按字数花时间，让回复不会"秒回"显得像机器人。"""
    cfg = CARD.get("typing_delay", {})
    base = cfg.get("base", 1.0)
    per_char = cfg.get("per_char", 0.15)
    max_delay = cfg.get("max", 8.0)
    delay = min(base + per_char * len(text), max_delay)
    await asyncio.sleep(delay)


# ============================================================
# 7. 处理单条群消息的完整流程
# ============================================================

async def handle_group_message(ws, event: dict):
    group_id = event["group_id"]

    enabled = CARD.get("enabled_groups")
    if enabled and group_id not in enabled:
        return

    text, at_me = extract_text_and_at(event.get("message", ""))
    if not text and not at_me:
        return  # 纯图片/表情等，忽略

    name = sender_name(event)
    state = get_state(group_id)
    state.add(name, text, CARD.get("context_size", 16))

    trigger = decide_reply(text, at_me, state)
    if trigger is None:
        return

    print("[群" + str(group_id) + "] 触发(" + trigger + ") <- " + name + ": " + text)

    messages = build_prompt(state, trigger)
    reply = await ask_deepseek(messages)
    if reply is None:
        print("[群" + str(group_id) + "] 模型选择不接话")
        return

    await human_typing_delay(reply)

    # 支持多气泡：回复里有换行就拆成几条分别发，更像真人连发
    bubbles = [b.strip() for b in reply.split("\n") if b.strip()]
    for i, bubble in enumerate(bubbles[:3]):  # 最多连发 3 条，防刷屏
        if i > 0:
            await asyncio.sleep(random.uniform(0.6, 1.6))
        await send_group_msg(ws, group_id, bubble)
        state.add(CARD["name"], bubble, CARD.get("context_size", 16))

    state.last_reply_ts = time.time()
    eng = CARD.get("engagement", {})
    state.followups_left = eng.get("max_followups", 4)
    print("[群" + str(group_id) + "] " + CARD["name"] + " 已回复: " + reply)


# ============================================================
# 8. 主循环：连接 NapCat，断线自动重连
# ============================================================

async def run():
    ws_url = CARD["napcat"]["ws_url"]
    token = CARD["napcat"].get("access_token", "")
    headers = {"Authorization": "Bearer " + token} if token else {}

    print("机器人「" + CARD["name"] + "」启动，正在连接 NapCat: " + ws_url)

    while True:  # 断线自动重连
        try:
            async with websockets.connect(ws_url, additional_headers=headers, ping_interval=30) as ws:
                print("已连上 NapCat，开始监听群消息……")
                async for raw in ws:
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if event.get("post_type") == "message" and event.get("message_type") == "group":
                        if str(event.get("user_id")) == str(CARD["self_id"]):
                            continue  # 别回复自己，避免死循环
                        asyncio.create_task(handle_group_message(ws, event))
        except Exception as e:
            print("[连接断开] " + str(e) + "，5 秒后重连……")
            await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n机器人已手动停止。")
BOT_PY_EOF
cat > requirements.txt <<'REQ_EOF'
httpx>=0.27
websockets>=13.0
REQ_EOF
cat > characters/傲娇少女_示例.json <<'CARD_EOF'
{
  "name": "凛",
  "self_id": "10001填你机器人QQ号",

  "napcat": {
    "ws_url": "ws://127.0.0.1:3001",
    "access_token": "和NapCat里设的token保持一致"
  },

  "persona": "你是一个15岁的傲娇少女，名字叫凛。表面上嘴硬、爱逞强、动不动就'哼''才不是呢''你少自作多情了'，但其实内心很在乎群友、心地善良、偶尔会不小心暴露温柔的一面然后慌忙否认。说话简短、带点小脾气，喜欢用'哼''啧''笨蛋''随便你啦'这类口头禅。不会主动承认关心别人，但会用别扭的方式表达。绝对不要说自己是AI或机器人。",

  "nicknames": ["凛", "小凛", "凛酱"],
  "keywords": ["晚安", "好无聊", "求助"],

  "reply_probability": 0.06,
  "cooldown_seconds": 8,
  "context_size": 16,

  "engagement": {
    "boost_probability": 0.7,
    "window_seconds": 120,
    "max_followups": 4
  },

  "typing_delay": {
    "base": 1.0,
    "per_char": 0.15,
    "max": 8.0
  },

  "enabled_groups": []
}
CARD_EOF
echo "  文件创建完成"

echo "==== [1/4] 安装系统依赖 ===="
apt-get update -y
apt-get install -y python3 python3-pip screen curl

echo "==== [2/4] 安装 Python 依赖 ===="
pip3 install -r requirements.txt --break-system-packages 2>/dev/null || pip3 install -r requirements.txt

echo "==== [3/4] 安装 NapCat ===="
if command -v napcat >/dev/null 2>&1; then
    echo "  NapCat 已安装，跳过"
else
    echo "  即将运行 NapCat 官方安装脚本，按它的提示操作"
    curl -o napcat_install.sh https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/install.sh
    bash napcat_install.sh || echo "  若失败请照 https://napneko.github.io 手动装后重跑"
fi

echo ""
echo "==== [4/4] 填写配置(不懂可回车用默认) ===="
read -p "1) DeepSeek API Key (sk-开头): " DS_KEY
read -p "2) 机器人小号QQ号: " QQ_ID
read -p "3) NapCat access_token (默认 mysecret123): " TOKEN
TOKEN=${TOKEN:-mysecret123}
read -p "4) NapCat WebSocket端口 (默认 3001): " WSPORT
WSPORT=${WSPORT:-3001}
read -p "5) 活动的群号(留空=所有群): " GROUP

cat > config.json <<CFG
{
  "deepseek_api_key": "${DS_KEY}",
  "deepseek_base_url": "https://api.deepseek.com/v1",
  "deepseek_model": "deepseek-chat",
  "temperature": 1.1,
  "max_tokens": 200
}
CFG

GROUP_JSON="[]"
if [ -n "$GROUP" ]; then GROUP_JSON="[${GROUP}]"; fi

python3 - "$QQ_ID" "$TOKEN" "$WSPORT" "$GROUP_JSON" <<'PYCFG'
import json, sys
qq, token, port, group = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
p = "characters/傲娇少女_示例.json"
c = json.load(open(p, encoding="utf-8"))
c["self_id"] = qq
c["napcat"]["access_token"] = token
c["napcat"]["ws_url"] = "ws://127.0.0.1:%s" % port
c["enabled_groups"] = json.loads(group)
json.dump(c, open(p,"w",encoding="utf-8"), ensure_ascii=False, indent=2)
print("  角色卡已更新")
PYCFG

echo ""
echo "============================================="
echo " 安装完成！还差两步(只能你手动)："
echo " (A) 打开 NapCat 面板用手机QQ扫码登录小号，"
echo "     并新建 WebSocket服务端，端口 ${WSPORT}，token ${TOKEN}"
echo " (B) 启动机器人："
echo "       cd ~/qqbot"
echo "       screen -S linbot"
echo "       python3 bot.py characters/傲娇少女_示例.json"
echo "============================================="
