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

默认监听 `127.0.0.1:8780`。如果要 `MEMORY_HOST=0.0.0.0` 对外，务必同时设置 `MEMORY_TOKEN`。

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

默认只加记忆。要在同一个入口上叠加人格/情绪，启动时加：

```bash
MEMORY_AGENT=1 AGENT_CONFIG=./config.example.json ...
```

或者直接用专用路径：

```
POST /v1/agent/chat/completions
```

它接受 OpenAI 格式，返回里多一个 `agent_state`。

每次请求会：

1. 取最后一条用户消息召回记忆
2. 把记忆作为 system 拼到最前面
3. 转发给上游模型
4. 默认把用户消息和助手回复写回记忆库；`MEMORY_AUTO_REMEMBER=0` 关闭

注意：这一层只加记忆，不带人格、情绪和表达层。要完整角色，还是用 AstrBot 插件版。

## 从旧记忆导入

不会修改原库，只读源文件：

```bash
python -m memory_core.import_data \
  --to ./data/brain.db \
  --from-brain /path/to/AstrBot/brain.db
```

也支持 JSONL：

```bash
python -m memory_core.import_data --to ./data/brain.db --from-jsonl chat.jsonl
```

会导入记忆、可见性、向量、实体和关系；重复文本跳过。

## 已有角色的接法

如果网友已经有一只养好的鱼，不想换人设：

```bash
MEMORY_AGENT=1 \
MEMORY_AGENT_KEEP_SYSTEM=1 \
AGENT_CONFIG=./config.example.json \
python -m memory_core.server
```

这样：

- 他们原来的 `system` 保留
- 只追加情绪状态和记忆
- 原来的对话历史也保留
- 他们的人设不会被配置里的“助手”覆盖

不设 `MEMORY_AGENT_KEEP_SYSTEM` 时，才会用配置里的 `system_prompt`。

## 说明

- 这是从 AstrBot 插件版抽出的核心，不包含人格、情绪、表达层
- 人格/情绪仍在原插件里；这里只负责记忆

## 评估

数据集是 JSONL，一行一条：

```json
{"query":"用户喜欢什么玩具","expect_any":["玩具","鲸鱼"],"type":"fact"}
{"query":"完全没提过的话题","expect_any":[],"type":"negative"}
```

`expect_any` 为空表示负例：没有命中或最高分很低才算对。

```bash
MEMORY_EVAL=./eval/queries.example.jsonl python -m memory_core.server
```

接口：

```
GET  /eval           最近一次结果
POST /eval/run       跑一次
GET  /eval/queries   当前评估集
POST /eval/query     追加一条
```

面板右上角「评估」可以直接看指标、跑一次、加查询。

## 生活层（可选）

不绑定平台，只产出“她做了什么”的事件，由你的桥决定发不发。

```bash
LIFE_AUTO=1 LIFE_CONFIG=./config.example.json python -m memory_core.server
```

接口：

```bash
# 看状态
curl http://127.0.0.1:8780/life/state

# 手动让她过一个动作（调试用）
curl -X POST http://127.0.0.1:8780/life/step -H 'Content-Type: application/json' -d '{"force":"book"}'

# 拉事件
curl 'http://127.0.0.1:8780/life/events?after=0&pending=1'

# 标记已处理
curl -X POST http://127.0.0.1:8780/life/ack -H 'Content-Type: application/json' -d '{"id":1}'
```

动作：发呆、休息、看书、刷手机、反思、午睡。看书需要在配置里给 `books_dir`。

和 AstrBot 生活层的区别：这里不直接发 QQ，事件交给客户端。
