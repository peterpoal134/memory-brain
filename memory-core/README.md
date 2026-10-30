# memory-core

不依赖 AstrBot 的独立记忆核心：SQLite + 五路混合检索 + 衰减/归档 + HTTP 接口。

适合接：

- qqbridge、NapCat 之类只负责收发消息的桥
- DSH 窗口、桌面宠物、其它 Agent
- 自己的脚本

## 安装

```bash
cd memory-core
python3 -m venv .venv && . .venv/bin/activate
pip install .
```

装完可以直接用命令 `memory-core`，或者 `python -m memory_core.server`。

## 启动

```bash
MEMORY_DB=./data/brain.db \
MEMORY_TOKEN=local-token \
MEMORY_EMBED_URL=http://127.0.0.1:11434/api/embed \
python -m memory_core.server
```

默认监听 `127.0.0.1:8780`。

没配 `MEMORY_EMBED_URL` 就只用关键词 + 图谱 + 重要性，启动和召回都是毫秒级；配了 Ollama bge-m3 才走向量。

## 接口

写一条：

```bash
curl -s http://127.0.0.1:8780/remember \
  -H 'Authorization: Bearer local-token' -H 'Content-Type: application/json' \
  -d '{"text":"用户喜欢蓝灰色的毛绒鲸鱼","kind":"fact","importance":0.8,"visibility":"public"}'
```

召回：

```bash
curl -s http://127.0.0.1:8780/recall \
  -H 'Authorization: Bearer local-token' -H 'Content-Type: application/json' \
  -d '{"query":"鲸鱼","k":3,"visibility":["public","private"]}'
```

返回：

```json
{"status":"ok","data":{"hits":[...],"block":"1. [2026-10-30] ..."}}
```

`block` 可以直接拼到 system prompt；`hits` 带分数、重要性、可见性。

其它：

```
GET  /health   体检
GET  /stats    统计
POST /decay    衰减
POST /archive  归档
```

## 可见性

- `public`：群里、私聊都能召回
- `private`：只在私聊召回
- `group`：只在群聊召回

调用方按场景传 `visibility`。

## 接 qqbridge / DSH

在每次调模型前：

1. `/recall` 拿 `block`
2. 拼到 system prompt
3. 模型回复后，把重要内容 `/remember` 回去

不需要安装 AstrBot，也不需要它的插件 API。

## 直接当 OpenAI 接口用

qqbridge、DSH 或任何支持 OpenAI 格式的客户端，可以直接把 base_url 指过来：

```bash
MEMORY_LLM_BASE=https://api.deepseek.com/v1 \
MEMORY_LLM_KEY=sk-xxx \
MEMORY_LLM_MODEL=deepseek-chat \
MEMORY_TOKEN=local-token \
MEMORY_DB=./data/brain.db \
python -m memory_core.server
```

客户端配置：

```
base_url = http://127.0.0.1:8780/v1
api_key  = local-token
model    = memory-core
```

每次请求会：

1. 取最后一条用户消息召回记忆
2. 把记忆作为 system 拼到最前面
3. 转发给上游模型
4. 可选：`MEMORY_AUTO_REMEMBER=1` 时把用户消息写回记忆库

注意：这一层只加记忆，不带人格、情绪和表达层。要完整角色，还是用 AstrBot 插件版。

## 说明

- 这是从 AstrBot 插件版抽出的核心，不包含人格、情绪、表达层
- 人格/情绪仍在原插件里；这里只负责记忆
