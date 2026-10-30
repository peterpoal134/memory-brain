# AstrBot 接入

适用于想直接得到“完整角色”的人：记忆、情绪、表达、生活层、面板、提醒都在插件里。

## 1. 环境

- AstrBot 4.29.x
- Python 3.12
- 一个 QQ 适配器（如 NapCat / aiocqhttp）
- 可选：Ollama + bge-m3、ffmpeg、faster-whisper

## 2. 放插件

把仓库 `plugins/` 下四个目录复制到：

```
AstrBot/data/plugins/
  astrbot_plugin_memory_brain
  astrbot_plugin_persona_state
  astrbot_plugin_human_style_guard
  astrbot_plugin_self_style
```

重启 AstrBot。

## 3. 配置

在 AstrBot 插件配置里至少设置：

```
embedding_provider_id   有 bge-m3 就填，没有留空
owner_id                你自己的 QQ（送礼/好感用）
protect_names           想保护的人名，逗号分隔
owner_names             按时间回忆时哪些称呼算“你”
```

生活层是独立进程，可选；不装也能用记忆和面板。

## 4. 验证

```
面板 → 记忆大脑 → 家 / 体检 / 评估
```

能打开、能看到记忆数，就通了。

## 5. 和 memory-core 的区别

AstrBot 版是完整运行时；memory-core 是给非 AstrBot 平台的记忆核心。两者不要同时写同一个 `brain.db`。
