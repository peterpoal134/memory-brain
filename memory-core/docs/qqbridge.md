# qqbridge 接入

qqbridge 如果支持 OpenAI 兼容 provider，可以把 `memory-core` 当模型端点。

## 1. 启动

```bash
cd memory-core
pip install .
MEMORY_DB=./data/brain.db \
MEMORY_TOKEN=local-token \
MEMORY_LLM_BASE=https://api.deepseek.com/v1 \
MEMORY_LLM_KEY=sk-xxx \
MEMORY_LLM_MODEL=deepseek-chat \
memory-core
```

## 2. qqbridge 侧

```
base_url = http://127.0.0.1:8780/v1
api_key  = local-token
model    = memory-core
```

**必须让每条请求带用户标识**：

```json
{"user":"qq:123456","messages":[{"role":"user","content":"..."}]}
```

不带会 400，避免所有人的记忆混在一起。

如果 qqbridge 不能自定义 `user` 字段，有两个办法：

1. 在 qqbridge 的请求模板里加一个固定/变量字段
2. 前面加一层很小的转发，把 QQ 号写进 `user`

## 3. 想要人格和情绪

```bash
MEMORY_AGENT=1 \
MEMORY_AGENT_KEEP_SYSTEM=1 \
AGENT_CONFIG=./config.example.json \
memory-core
```

- `MEMORY_AGENT_KEEP_SYSTEM=1`：保留 qqbridge 原来的 system
- 不设：用 `config.example.json` 里的人设

## 4. 验证

```bash
curl http://127.0.0.1:8780/health
curl 'http://127.0.0.1:8780/life/state'
```

面板：

```
http://127.0.0.1:8780/panel
```
