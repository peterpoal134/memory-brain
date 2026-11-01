# 数据格式

3D 大脑只认一种输入：一张图。

```js
window.CortexBrain3D.setData({
  nodes: [...],
  edges: [...],
  memories: [...]
})
```

三个数组，缺一个也能跑，但效果会差。

---

## `nodes` — 实体节点

```json
{
  "id": 50,
  "name": "用户",
  "type": "person",
  "degree": 417,
  "iso": false
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | number | ✅ | 唯一。跟 `edges` 和 `memories.entities` 里的 id 对应 |
| `name` | string | ✅ | 显示名。点击时显示 |
| `type` | string | | 决定颜色，见下表。缺省 `thing` |
| `degree` | number | | 连接数。越大节点越大 |
| `iso` | boolean | | 孤立节点（没有边）。会画得暗一些 |

**`type` 取值与颜色：**

| type | 颜色 | 用于 |
|---|---|---|
| `person` | `#54e7ff` 青 | 人、角色 |
| `thing` | `#ffc66c` 金 | 物件、概念 |
| `topic` | `#bd96ff` 紫 | 话题 |
| `place` | `#79f3c8` 绿 | 地点 |
| `skill` | `#f9a0df` 粉 | 技能 |
| `memory` | `#92caff` 蓝 | 记忆本身 |

---

## `edges` — 实体之间的关系

```json
{
  "src": 50,
  "dst": 55,
  "kind": "relation",
  "label": "喜欢",
  "weight": 1.0
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `src` | number | ✅ | 起点 node id |
| `dst` | number | ✅ | 终点 node id |
| `kind` | string | | `relation` / `cooccur` / `similar`。决定线的样式 |
| `label` | string | | 关系词，2-4 个字（「喜欢」「住在」） |
| `weight` | number | | 0-1。越大线越亮 |

**`kind` 的视觉区别：**

- `relation` — 实线，粗
- `cooccur` — 细线（共同出现）
- `similar` — 虚线（语义相近）

---

## `memories` — 记忆条目

```json
{
  "id": 939,
  "text": "他反复念叨同一句话，我一遍遍改，倒也不烦。",
  "kind": "relation",
  "importance": 0.6,
  "created_at": 1793466224.06,
  "entities": [50, 161]
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | number | ✅ | 唯一 |
| `text` | string | ✅ | 记忆正文。点击时显示 |
| `kind` | string | | 决定颜色，见下表 |
| `importance` | number | | 0-1。影响节点亮度 |
| `created_at` | number | | Unix 时间戳（秒）。影响位置分布 |
| `entities` | number[] | | 这条记忆挂了哪些实体。**决定它落在皮层哪个位置** |

**`kind` 取值与颜色：**

| kind | 中文 | 颜色 |
|---|---|---|
| `episode` | 经历 | `#7fd4ff` |
| `fact` | 事实 | `#ffd166` |
| `preference` | 偏好 | `#ff9ad5` |
| `relation` | 关系 | `#b967ff` |
| `skill` | 技能记忆 | `#fca2ee` |
| `self` | 自我记忆 | `#39ff8f` |
| `selfview` | 自我认知 | `#9dff8f` |
| `review` | 回顾 | `#79f3c8` |
| `reflection` | 反思 | `#7dffcf` |

---

## 位置怎么算的

**不是随机撒点，是**挂在皮层上**。

`placeOnCortex` 的逻辑：

1. 拿记忆的 `entities` 数组，哈希成一个数
2. 用这个数在皮层网格（144490 + 145071 个顶点）里挑一个顶点
3. 记忆就画在那个顶点上

**所以：同一条记忆，只要 `entities` 不变，位置就不变。**

**这是刻意设计的——每次刷新位置乱跳，就看不出「这块区域在长东西」。**

`seeded(s)` 是 FNV-1a 哈希，确定性。

---

## 最小可用示例

**只有 nodes，没有 edges 和 memories：**

```js
window.CortexBrain3D.setData({
  nodes: [
    {id: 1, name: "我", type: "person", degree: 5},
    {id: 2, name: "你", type: "person", degree: 3}
  ],
  edges: [
    {src: 1, dst: 2, kind: "relation", label: "认识", weight: 0.8}
  ],
  memories: []
})
```

**这样能看到节点和一条线，但没有记忆点。**

**要有记忆点，`memories` 里每条都得有 `entities`。**

---

## 从你的库导出

**如果你用 SQLite，大概是这个形状：**

```python
import json, sqlite3

conn = sqlite3.connect("your.db")
conn.row_factory = sqlite3.Row

nodes = [dict(r) for r in conn.execute(
    "SELECT id, name, type, 0 AS degree FROM entities")]
edges = [dict(r) for r in conn.execute(
    "SELECT src, dst, 'relation' AS kind, relation AS label, weight FROM relations")]

memories = []
for r in conn.execute("SELECT id, text, kind, importance, created_at FROM memories"):
    ents = [e[0] for e in conn.execute(
        "SELECT entity_id FROM memory_entities WHERE memory_id=?", (r["id"],))]
    memories.append({**dict(r), "entities": ents})

print(json.dumps({"nodes": nodes, "edges": edges, "memories": memories},
                 ensure_ascii=False))
```

**字段名不一样就改 SQL 的别名，前端只认上面那张表。**

---

## 性能参考

| 规模 | 表现 |
|---|---|
| 100 节点 / 300 边 / 200 记忆 | 很流畅 |
| 500 节点 / 1500 边 / 1000 记忆 | 流畅，首次加载 2-3 秒 |
| 2000 节点 / 6000 边 / 5000 记忆 | 能跑，但掉帧 |

**瓶颈在皮层网格的顶点数（29 万），不在你的数据量。**

**数据量大了先考虑分页或按时间窗过滤，别一次全喂。**
