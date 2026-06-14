# 拟人 QQ 群聊机器人

基于 NapCat (OneBot) + DeepSeek 的拟人化 QQ 群聊 / 私聊机器人，角色卡驱动。
支持：被动接话判断、活跃窗口省 token、长期记忆与相关度召回、连续记忆模式、
自动铭记珍贵回忆、全程记录、人设热切换、禁言指令、表情包、时间感知等。

## 快速开始

1. 安装依赖：`pip install -r requirements.txt`
2. 复制 `config.example.json` 为 `config.json`，填入你的 DeepSeek API Key。
3. 在 `characters/` 下创建你的角色卡（JSON），参考下方字段说明。
4. 启动：`python bot.py characters/你的角色.json`

> `config.json`、`characters/`、`memory/`、`memes/` 均不纳入版本库（含密钥与个人数据）。

## 角色卡关键字段

- `name` / `nicknames` / `keywords`：名字、昵称、触发关键词
- `persona`：人物设定（也可放到 `characters/personas/<角色名>/*.txt` 里热切换）
- `napcat.ws_url` / `napcat.access_token`：连接 NapCat 的地址与令牌
- `memory`：长期记忆相关开关（`archive_all`、`max_items`、`inject_items` 等）
- `max_bubbles` / `typing_delay` / `active_window`：发送节奏与活跃度

## 文档

- `部署教程_小白必读.md` —— 部署步骤
- `接话逻辑说明.md` —— 被动接话/省 token 逻辑
- `表情包功能说明.md` —— 表情包用法

## 说明

本项目仅供学习交流。请遵守相关平台规则，注意账号风控风险。
