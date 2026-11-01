# DSH 客户端模块

这两个包是给 **DSH 桌面版**用的，不是 AstrBot 插件。

**qqbridge 本次不适配，也没有测试环境。**

| 目录 | 作用 |
|---|---|
| `dafeiyu-3d-brain/` | 真实皮层网格的 3D 记忆大脑，纯浏览器 JS，可嵌进 DSH 面板 |
| `persistent-self/` | 跨会话开机包：身份层、标准层、事实层、原话层，只依赖 SQLite |

## 3D 大脑

```bash
cd dafeiyu-3d-brain
python -m http.server 8899
# 打开 http://127.0.0.1:8899/demo-standalone.html
```

接入自己的页面看 `INTEGRATION.md`；数据格式看 `DATA-FORMAT.md`。

## 跨会话记忆

```bash
cd persistent-self
python example.py
```

接进自己的库：

```python
import sqlite3
from self import SelfView

conn = sqlite3.connect("brain.db")
conn.row_factory = sqlite3.Row
conn.executescript(open("schema.sql").read())
print(SelfView(conn).render())
```

## 说明

- 不依赖 AstrBot
- 不包含任何个人记忆、QQ、路径或人格数据
- 皮层网格许可见 `cortex/LICENSE-ATTRIBUTION.md`
