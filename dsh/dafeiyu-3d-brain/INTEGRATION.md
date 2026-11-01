# 接入说明

四步，加五个坑。

---

## 第 1 步：静态资源路由

`brain3d.py` 里的 `build()` 返回的脚本会请求：

```
{cortexBase}/cortex/lh.bin
{cortexBase}/cortex/rh.bin
{cortexBase}/cortex/lh.fibers.bin
{cortexBase}/cortex/rh.fibers.bin
{cortexBase}/cortex/lh.hologram.bin
{cortexBase}/cortex/rh.hologram.bin
{cortexBase}/cortex/metadata.json
{cortexBase}/cortex/hologram-metadata.json
```

默认前缀是 `/panel-assets`。

**改前缀的两种方式：**

```python
# 方式一：改模块级常量（影响所有调用）
import brain3d
brain3d.CORTEX_BASE = "/static/brain"

# 方式二：传给 build()（只影响这次）
html = html.replace("<!--BRAIN3D-->", brain3d.build("/static/brain"))
```

**两种都可以，方式二更安全**——不会污染全局状态。

**你需要让这个路径能访问到 `cortex/` 目录。**

FastAPI：

```python
from fastapi.staticfiles import StaticFiles
app.mount("/panel-assets/cortex", StaticFiles(directory="cortex"), name="cortex")
```

aiohttp：

```python
app.router.add_static("/panel-assets/cortex/", "cortex")
```

Flask：

```python
@app.route("/panel-assets/cortex/<path:fn>")
def cortex(fn):
    return send_from_directory("cortex", fn)
```

**别忘了 three/ 也要能访问**（如果用了本地副本）。

---

## 第 2 步：importmap

**不能省。**

three.js 的 addons 内部写的是 `import 'three'`，浏览器解析不了裸模块名。
没有 importmap 就整个模块静默失败——**控制台可能什么都不报**。

放在 `<head>` 里，**必须在任何 module script 之前**：

```html
<script type="importmap">
{
  "imports": {
    "three": "/panel-assets/three/build/three.module.js",
    "three/addons/": "/panel-assets/three/examples/jsm/"
  }
}
</script>
```

路径按你的实际情况改。

---

## 第 3 步：画布 + 占位符

```html
<canvas id="cortex-canvas"></canvas>
<div id="model-loading">正在加载皮层…</div>

<!--BRAIN3D-->
```

`cortex-canvas` 这个 id 是 `brain3d.py` 里自动挂载用的，**别改**（或者改 `brain3d.py` 里的 `mount('cortex-canvas')`）。

`model-loading` 会显示加载进度，出错也在里面显示。

---

## 第 4 步：服务端注入

```python
from brain3d import build

html = open("page.html", encoding="utf-8").read()
html = html.replace("<!--BRAIN3D-->", f'<script type="module">\n{build()}\n</script>')
```

`build()` 返回的是完整的模块代码（约 33 KB）。

---

## 七个坑

**前两个是我自己打包时踩的，你拿到这个包应该不会遇到——**
**但如果你自己从 npm 拷 three.js，一定会遇到。**

### 0a. `three.core.js` 没拷

**现象**：整个模块静默失败，`window.CortexBrain3D` 是 `undefined`，
Console 里**什么都没有**（连报错都没有）。

**原因**：three.js 0.160+ 把核心拆成了两个文件：

```js
// three.module.js 的第一行
import { ... } from './three.core.js';
```

只拷 `three.module.js` 不拷 `three.core.js`，模块链在第一步就断了。
**浏览器不会报「找不到文件」，它只会静默中止整个模块。**

**修**：`build/` 下两个文件都要。

```
three/build/three.module.js    644 KB
three/build/three.core.js     1440 KB   ← 别漏
```

**这个坑最坑**——没有任何错误信息，你只能看到「3D 场景没起来」。

---

### 0b. importmap 的值不能是裸模块名

**现象**：Console 报

```
Ignored an import map value of "three": Bare specifier: three/build/three.module.js
```

**原因**：importmap 的值必须以 `/`、`./`、`../` 或完整 URL 开头。
写成 `three/build/three.module.js`（没有 `./`）会被当成裸模块名，**整条映射被丢弃**。

**修**：

```json
// ✗ 错
"three": "three/build/three.module.js"

// ✓ 对
"three": "./three/build/three.module.js"

// ✓ 也对（绝对路径）
"three": "/panel-assets/three/build/three.module.js"
```

---

### 1. 缺 importmap

**现象**：3D 完全不出现，控制台可能什么都不报。

**原因**：addons 内部 `import 'three'` 解析失败，整个模块中止。

**修**：第 2 步不能省。

---

### 2. 动态 import + 顶层常量

**现象**：`Cannot read properties of undefined (reading 'Color')`，一行都不执行。

**原因**：用了 `await import('three')`，但模块顶层有
`const gold = new THREE.Color('#ffb43c')`——那时 `THREE` 还是 `undefined`。

**修**：静态 `import * as THREE from 'three'`，且**必须放在模块最顶部**，
在所有注释之前。ES 模块的 import 声明会被提升，但其他顶层代码按顺序执行。

---

### 3. setData 时序

**现象**：脑出来了，但**没有记忆点和线**。

**原因**：`setData` 比场景挂载先跑。3D 要加载 15 MB 皮层，是异步的。
原来的代码 `if (!this.scene) return` 直接返回，数据就丢了。

**修**：包里已经处理了——`setData` 会先存 `pendingGraph`，
等场景就绪再补上。**但你自己调的时候要注意，别在挂载前调完就以为没事了。**

---

### 4. 静态资源 404

**现象**：`/panel-assets/cortex/lh.bin` 404。

**原因**：路径拼接重复。比如 `parent / "cortex" / rel`，
但 `rel` 里已经带了 `cortex/`，结果变成 `cortex/cortex/lh.bin`。

**修**：先 `resolve()` 再检查前缀：

```python
from pathlib import Path

def safe_join(base_dir: Path, rel: str, allowed: tuple[str, ...]) -> Path | None:
    """把 rel 拼到 base_dir 下，并检查没跑出 allowed 里的子目录。

    返回 None 表示越界，调用方该返回 403。
    """
    fpath = (base_dir / rel).resolve()
    for sub in allowed:
        if str(fpath).startswith(str((base_dir / sub).resolve())):
            return fpath
    return None
```

---

### 5. 令牌/鉴权挡住了静态资源

**现象**：页面能打开，但模型 401。

**原因**：鉴权中间件把 `/panel-assets/` 也拦了。

**修**：静态资源要放行，或者带上令牌。

```python
# 放行面板和它的静态资源
if path.startswith("/panel") or path.startswith("/panel-assets/"):
    pass  # 不校验
else:
    check_token()
```

---

## 接口

**完整 API 见 [API.md](API.md)。** 这里只列最常用的：

```js
window.CortexBrain3D.setData(graph)      // 喂数据
window.CortexBrain3D.setOptions({...})   // 改渲染参数
window.CortexBrain3D.rotate(true)        // 自动旋转
window.CortexBrain3D.reset()             // 重置视角
window.CortexBrain3D.dispose()           // 释放 GPU 资源
window.__onSelect = function (node) {}   // 点节点时触发
```

**`setOptions` 真正生效的只有四个字段：**

| 字段 | 默认 | 说明 |
|---|---|---|
| `mode` | `'hologram'` | `'reference'` 会隐藏画布 |
| `running` | `true` | 是否推进动画 |
| `active` | 全部 | 哪些 `type` 可被点击 |
| `density` | `32` | 光纤密度 |

**⚠️ 这几个看起来该有，但实际不生效**：
`bloom`、`haze`、`mirror`、`fibers`、`rotate`（用 `rotate()` 方法）、`query`。
它们从原项目带过来了，但渲染逻辑没读。详见 [API.md](API.md) 最后一节。

---

## 浏览器兼容

| 要求 | 说明 |
|---|---|
| **WebGL** | 必需。WebGL1 或 WebGL2 都行 |
| **ES Modules** | 必需（`<script type="module">`） |
| **importmap** | 必需。Chrome 89+ / Edge 89+ / Safari 16.4+ / Firefox 108+ |
| **可选链 `?.`** | 用到了 21 处。Chrome 80+ / Safari 13.1+ |
| **`??`** | 用到了 3 处。Chrome 80+ / Safari 13.1+ |

**实测能跑**：Chrome 120、Edge 120、Safari 17。

**跑不了**：IE、Safari 15 及以下（没有 importmap）。

**WebGL 不支持时**，页面会显示：
> 这个浏览器/显卡不支持 WebGL，3D 跑不起来。试试 Chrome 或 Edge，或者检查显卡驱动。

**不会白屏，也不会只报一句看不懂的错。**

---

## 出错时怎么看

**所有错误都会写到 `#model-loading` 里，同时 `console.error` 一份。**

| 现象 | 原因 | 怎么查 |
|---|---|---|
| 一直停在「正在加载皮层模型」 | 静态资源 404 | 网络面板看 `.bin` 请求 |
| 「皮层数据加载失败：... 返回 404」 | 路径前缀不对 | 改 `CORTEX_BASE` |
| 「这个浏览器/显卡不支持 WebGL」 | 显卡/驱动 | 换浏览器或更新驱动 |
| 「三维场景创建失败」 | WebGL 上下文创建失败 | 看 Console 完整报错 |
| 脑出来了但没点没线 | `setData` 时序 | 见下面第 3 个坑 |
| 页面全白，Console 空 | 缺 importmap | 见下面第 1 个坑 |

---

## 性能

| 项 | 数值 |
|---|---|
| 皮层顶点 | 144490 + 145071 |
| 首次加载 | 15 MB，本地 2-5 秒 |
| 显存 | 约 200 MB |
| 帧率 | 60 fps（桌面独显/集显都行） |

**嫌重的话**，`cortex/*.fibers.bin` 和 `*.hologram.bin` 可以砍掉（省 5 MB），
但视觉效果会差一截——**我砍过一次，被要求还原了**。

---

## 许可

皮层数据来自 Anderson Winkler，CC BY-SA 3.0。
详见 `cortex/LICENSE-ATTRIBUTION.md`，**开源必须保留**。

three.js 是 MIT。
