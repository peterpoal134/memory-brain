# 记忆系统插件包（通用版）

四个插件，配合 AstrBot 使用。**不含任何个人数据、聊天记录、人格设定**。

## 包含

| 插件 | 作用 |
|---|---|
| astrbot_plugin_memory_brain | 记忆库：写入 / 检索 / 遗忘 / 多模态 / 体检 / 报表 |
| astrbot_plugin_persona_state | 情绪 PAD + 好感 + 识海 + 旧账 |
| astrbot_plugin_human_style_guard | 发送前清洗：客服腔、句长、分段、表情节奏 |
| astrbot_plugin_self_style | 自我档案 + 语气样例注入 |

## 安装

1. 把四个目录放进 `AstrBot/data/plugins/`；
2. 重启 AstrBot；
3. 面板 → 插件 → 对应插件里改配置（每个插件都有 `_conf_schema.json`）。

## 依赖（按需）

```bash
pip install faster-whisper pilk        # 语音转写（本地）
# 可选：pip install edge-tts           # 视频配音用
```

ffmpeg 需要系统里有（或设置环境变量 `ASTRBOT_FFMPEG=/path/to/ffmpeg`）。

## 路径约定

默认从 AstrBot 数据目录推导，不写死用户名：

- 数据目录：插件文件向上两级（`data/`）
- 自我档案：`data/self/`
- 生活记录：`data/../life/`（可用 `ASTRBOT_LIFE_DIR` 覆盖）

## 重要

- 人格**不会**被修改：所有增强都是往 system_prompt 追加“注入块”，每个块带 marker，可单独关闭。
- 记忆库是独立 SQLite，不动你原有的记忆插件。
- 没有附带任何数据库、日志、聊天记录、人物名字。

## 建议顺序

1. 先只开写入，不开注入，跑一天看摘要质量；
2. 私聊开注入，再开群聊；
3. 最后接情绪层；
4. 建 20–40 条评估集，改一次跑一次。
