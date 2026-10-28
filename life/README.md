# 生活层 Life Loop（通用版）

让角色在你不找她的时候，也有一点点自己的生活：
学习、看书、刷视频、写日记、发呆、午睡，再把看到的东西记成笔记。

**独立进程，不是 AstrBot 插件。** 聊天主进程挂了不影响它，它挂了也不影响聊天。

## 依赖

```bash
pip install beautifulsoup4
# 可选：刷手机视频（需要 adb + 手机）
# 可选：视频转写  pip install faster-whisper pilk
# 可选：ffmpeg 在 PATH 或设置 LIFE_FFMPEG
```

## 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
LIFE_LLM_URL | 本地模型接口 | http://127.0.0.1:8080/v1/chat/completions |
LIFE_FALLBACK_URL / MODEL | 备用模型 | DeepSeek |
LIFE_ASTRBOT_CONFIG | 读 AstrBot key | 空（不读） |
LIFE_MOOD_FILE | 读角色的心情/精力 | 空（不读） |
LIFE_ADB / LIFE_SCRCPY / LIFE_PHONE | 手机控制 | 空（跳过手机功能） |
LIFE_FFMPEG | ffmpeg 路径 | ffmpeg |
LIFE_WHISPER_DIR | whisper 模型目录 | ./whisper |
LIFE_NAPCAT / LIFE_NAPCAT_TOKEN | 发 QQ 消息 | 空（不发） |
LIFE_GROUP_ID | 群号 | 0（不发群） |
LIFE_SHARE_TO | 主动私聊对象 QQ | 空（不私聊） |

## 运行

```bash
python life_loop.py --once                 # 干跑一次，不发消息
python life_loop.py --once --force-action nap   # 强制午睡
python life_loop.py --send                 # 常驻，每 35–70 分钟一轮
```

## 数据

全部写在脚本旁边，首次运行自动创建：

```
state.json / goals.json / soul.md / diary.jsonl / actions.jsonl
watched.jsonl / notes.jsonl / interests.json / usage.jsonl
books/  watched/  phone/
```

不含任何个人数据，装完是白纸。

## 接入记忆系统

`notes.jsonl` 和 `watched.jsonl` 会被 `astrbot_plugin_memory_brain` 的每日维护导入。
把本目录路径通过 `ASTRBOT_LIFE_DIR` 告诉那个插件即可。
