# DSH / 桌面窗口接入

DSH 或其它窗口只要能发 HTTP，就能接。

## 1. 起服务

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

## 2. 两种接法

**A. 当 OpenAI 模型**

```
base_url = http://127.0.0.1:8780/v1
api_key  = local-token
model    = memory-core
```

请求必须带 `user`，一个窗口一个用户：

```json
{"user":"dsh:window-1","messages":[{"role":"user","content":"..."}]}
```

**B. 自己调记忆接口**

```
POST /recall    拿 block，拼到 system
POST /remember  写回
```

适合不想把整个聊天交给 memory-core 的窗口。

## 3. 人格

```bash
MEMORY_AGENT=1 AGENT_CONFIG=./config.example.json memory-core
```

或者保留窗口原来的 system：

```bash
MEMORY_AGENT=1 MEMORY_AGENT_KEEP_SYSTEM=1 memory-core
```

## 4. 面板

```
http://127.0.0.1:8780/panel
```

拖拽旋转、滚轮缩放、评估页 `#eval`。
