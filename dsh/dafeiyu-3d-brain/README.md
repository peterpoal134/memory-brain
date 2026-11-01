# 3D 记忆大脑

把记忆库画成一个**真实的脑**。

不是抽象的力导向图，是真实 MRI 皮层网格（144490 + 145071 个顶点）——
记忆挂在脑沟上，光纤在皮层间流动，节点之间有线连着。

**纯浏览器 JS，没有构建步骤，不需要 React。**

---

## 它解决什么

普通的记忆可视化是「一堆球用线连着」，看完不知道记忆长什么样。

这个版本：**记忆有位置**。同一条记忆，只要它挂的实体不变，位置就不变。
所以你能看出「这块区域在长东西」——而不是每次刷新都乱跳。

---

## 快速开始

**一、把整个目录放到你的 web 服务能访问的地方**

**二、页面里加 importmap**（必须在任何 module script 之前）

```html
<script type="importmap">
{
  "imports": {
    "three": "/your-path/three/build/three.module.js",
    "three/addons/": "/your-path/three/examples/jsm/"
  }
}
</script>
```

**三、加画布和占位符**

```html
<canvas id="cortex-canvas" style="width:100%;height:100%"></canvas>
<div id="model-loading">正在加载皮层…</div>
<!--BRAIN3D-->
```

**四、服务端把 `<!--BRAIN3D-->` 换成 `brain3d.build()`**

```python
import sys
sys.path.insert(0, "/path/to/this/dir")
from brain3d import build

html = open("your-page.html", encoding="utf-8").read()
html = html.replace("<!--BRAIN3D-->", f"<script type='module'>{build()}</script>")
```

**五、喂数据**

```js
window.CortexBrain3D.setData({
  nodes: [...],     // 见 DATA-FORMAT.md
  edges: [...],
  memories: [...]
})
```

**懒人版**：直接打开 `demo-standalone.html`（需要 HTTP 服务，不能 file://）
能看到假数据跑起来的效果。

```bash
python -m http.server 8899
# 打开 http://127.0.0.1:8899/demo-standalone.html
```

---

## 为什么用本地 three.js

**因为 CDN 靠不住。**

实测首次加载：

| 来源 | 耗时 |
|---|---|
| 本地 | **~10 ms** |
| npmmirror（国内） | 420 ms |
| unpkg | **1891 ms** |
| jsdelivr | **20027 ms** |

unpkg 在国内要 1.9 秒，没 VPN 直接白屏，控制台只报
`Failed to fetch dynamically imported module`。

所以包里带了 `three/` 的完整副本（2.1 MB），从你自己的服务加载。
**离线也能跑。**

---

## 文件说明

```
dafeiyu-3d-brain/
├── brain3d.py              渲染核心 + 挂载接口（唯一要 import 的）
├── cortex/                 皮层数据 15 MB（CC BY-SA 3.0，必须保留署名）
├── three/                  three.js 0.187.0 本地副本 2.1 MB
│   ├── build/
│   │   ├── three.module.js      644 KB  主模块
│   │   └── three.core.js       1440 KB  ← 必须一起带，three.module.js 内部 import 它
│   └── examples/jsm/            14 个 addon 文件
├── demo-standalone.html    自包含示例，配好静态路由就能看
├── DATA-FORMAT.md          数据格式约定 ← 先看这个
├── API.md                  完整接口 + 哪些方法不存在
├── INTEGRATION.md          接入细节 + 已知的坑
└── LICENSE                 皮层数据署名
```

---

## 许可

**皮层数据**：Anderson Winkler，CC BY-SA 3.0。
详见 `cortex/LICENSE-ATTRIBUTION.md`，**开源必须保留**。

**three.js**：MIT。

**其余代码**：MIT，随便用。

---

## 已知限制

- **WebGL 必需**。不支持的话会在 `model-loading` 里显示提示。
- **首次加载 15 MB**，本地约 2-5 秒，网络慢的话更久。
- **移动端能跑但很热**，29 万个顶点对手机 GPU 不友好。
- **只负责画，不负责取数**。你的记忆库怎么查、怎么算 `degree`，得自己写。

---

## 相关

数据格式在 [DATA-FORMAT.md](DATA-FORMAT.md)。
接入的坑在 [INTEGRATION.md](INTEGRATION.md)——**建议先看，能省你半天。**
