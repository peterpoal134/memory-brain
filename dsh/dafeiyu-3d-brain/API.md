# API 参考

`window.CortexBrain3D` 上挂的所有东西。

---

## 数据

### `setData(graph)`

喂数据。可以在挂载前调——会缓存，等场景就绪自动补上。

```js
window.CortexBrain3D.setData({
  nodes: [...],
  edges: [...],
  memories: [...]
})
```

格式见 [DATA-FORMAT.md](DATA-FORMAT.md)。

**注意**：重复调同一个 `graph`（JSON 序列化后相同）会被跳过，不会重建。

---

## 渲染控制

### `setOptions(partial)`

改渲染参数。**只合并，不替换**。

```js
window.CortexBrain3D.setOptions({ running: false })
```

**真正生效的字段只有四个：**

| 字段 | 类型 | 默认 | 作用 |
|---|---|---|---|
| `mode` | `'hologram'` \| `'reference'` | `'hologram'` | `reference` 会隐藏画布（用于对比截图） |
| `running` | boolean | `true` | 是否推进动画时间。`false` 会冻结流动 |
| `active` | string[] | 全部 category | 哪些 `type` 的节点可被点击/悬停 |
| `density` | number | `32` | 光纤密度。越大越密，也越吃性能 |

**`active` 的用法**（按类型过滤交互）：

```js
// 只让 person 和 thing 可点
window.CortexBrain3D.setOptions({ active: ['person', 'thing'] })
```

**注意**：`active` 只影响**交互**，不影响**显示**。要隐藏某类节点得改数据。

---

### `rotate(on)`

开关自动旋转。

```js
window.CortexBrain3D.rotate(true)   // 开
window.CortexBrain3D.rotate(false)  // 关
```

---

### `reset()`

重置相机到初始视角。

```js
window.CortexBrain3D.reset()
```

---

### `dispose()`

释放 GPU 资源。**切页面或卸载时应该调**，不然显存不回收。

```js
window.CortexBrain3D.dispose()
```

调用后 `scene` 变 `null`，要重新用必须再 `mount()`。

---

## 挂载

### `mount(canvasId)`

手动挂载。**一般不需要调**——`build()` 生成的脚本会自动挂 `cortex-canvas`。

```js
await window.CortexBrain3D.mount('my-canvas')
```

返回 `Promise<boolean>`。

**什么时候需要手动调**：
- 你的画布 id 不是 `cortex-canvas`
- 你想延迟挂载（比如等用户点按钮）

---

## 回调

### `window.__onSelect(node)`

**点节点时触发。** 不是 `CortexBrain3D` 的属性，是全局函数。

```js
window.__onSelect = function (node) {
  console.log(node)  // { id, name, type, degree, iso, x, y, z, ... }
  // 在这里显示详情面板
}
```

**不设这个函数也不会报错**，只是点了没反应。

`node` 的字段：

| 字段 | 说明 |
|---|---|
| `id` | 原始 id |
| `name` | 显示名 |
| `type` | 实体类型 |
| `category` | 同 `type`，用于 `active` 过滤 |
| `degree` | 连接数 |
| `x`, `y`, `z` | 皮层上的坐标 |

---

## 底层

### `scene`

`HologramScene` 实例。想深度定制时用。

```js
window.CortexBrain3D.scene.camera      // THREE.PerspectiveCamera
window.CortexBrain3D.scene.controls    // OrbitControls
window.CortexBrain3D.scene.renderer    // THREE.WebGLRenderer
window.CortexBrain3D.scene.nodes       // 当前节点数组
```

**注意**：直接改 `scene` 里的东西可能跟内部状态不同步。优先用上面的公开方法。

---

## 只读状态

### `options`

当前选项的副本。**别直接改它**，用 `setOptions()`。

```js
console.log(window.CortexBrain3D.options)
// { mode: 'hologram', running: true, active: [...], density: 32 }
```

---

## 常见用法

### 搜索时高亮

```js
// 按名字过滤可交互的节点
const hits = graph.nodes.filter(n => n.name.includes('记忆')).map(n => n.type)
window.CortexBrain3D.setOptions({ active: [...new Set(hits)] })
```

### 暂停动画（省电）

```js
document.addEventListener('visibilitychange', () => {
  window.CortexBrain3D.setOptions({ running: !document.hidden })
})
```

### 点击显示详情

```js
window.__onSelect = function (node) {
  const panel = document.getElementById('detail')
  panel.innerHTML = `<h3>${node.name}</h3><p>${node.degree} 条连接</p>`
  panel.style.display = 'block'
}
```

### 定时刷新数据

```js
setInterval(async () => {
  const res = await fetch('/api/graph').then(r => r.json())
  window.CortexBrain3D.setData(res.data)
}, 30000)
```

**注意**：`setData` 会重建所有节点和线，频繁调会掉帧。30 秒以上比较合适。

---

## 没实现的东西

**这些名字看起来该有，但实际没有：**

| 你可能想调 | 现状 |
|---|---|
| `setOptions({ bloom })` | ❌ 不生效。泛光强度写死在 shader 里 |
| `setOptions({ haze })` | ❌ 不生效。雾面一直开着 |
| `setOptions({ mirror })` | ❌ 不生效。镜面地板一直开着 |
| `setOptions({ fibers })` | ❌ 不生效。光纤跟着数据走 |
| `setOptions({ rotate })` | ❌ 不生效。用 `rotate(true/false)` |
| `setOptions({ query })` | ❌ 不生效。搜索得自己过滤 `active` |
| `getNodeById(id)` | ❌ 没有。用 `scene.nodes.find(n => n.id === id)` |
| `highlight(id)` | ❌ 没有。改 `active` 或自己做 DOM 覆盖层 |

**想要这些功能的话，得改 `brain3d.py`。**
**`options` 对象里留着这些字段是因为从原项目带过来的，但渲染逻辑没读它们。**
