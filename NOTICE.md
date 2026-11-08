# 来源与许可

`xiaojiu_brain/transport.py` 来自旧版 memory_core 项目 `memory_core/llm_http.py` 的已验证总超时实现。该旧项目的仓库许可证为 GNU Affero General Public License v3。保留该实现的原因是它已覆盖慢速滴流、响应头/正文停滞、连接关闭和绝对截止时间，而不是重复编写未经验证的网络层。

新包因此使用 AGPL-3.0。发布时请随附 `LICENSE` 和可取得的对应源码，保留原项目及后续贡献者的署名信息；此处不会把旧代码声称为全新原创。

新服务、证据检索、持久存储、迁移器、控制面板和 DSH 适配器在新目录编写，不导入旧题目分类器、旧检索正则或 AstrBot 插件运行时。

Python/Numpy/tzdata、DSH 宿主和模型服务各自遵循其许可。本发布不打包 DSH、模型权重、私人原话、旧数据库或模型密钥。

## 3D 大脑与前端资源

3D 首页沿用旧版全息脑形实现，保留金紫双半球、皮层网格、表面光纤和青色舞台；新增功能区导航只表示架构，不表示真实神经活动或逐条记忆的解剖位置。

- 皮层原网格作者为 Anderson Winkler，来自 [Brain for Blender](https://brainder.org/research/brain-for-blender/)。原网格及衍生几何遵循 **CC BY-SA 3.0**，不改称 AGPL 资产。署名、许可链接及修改说明见 `xiaojiu_brain/static/cortex/LICENSE-ATTRIBUTION.md`。
- Three.js 0.187.0 及本次打包的其附属模块遵循 MIT，完整许可见 `xiaojiu_brain/static/vendor/THREE-LICENSE.md`。浏览器运行时完全本地加载，不依赖 CDN。
- Lucide 图标的许可与署名见 `xiaojiu_brain/static/icons/LICENSE.md`。
- `brain-fallback.svg` 由 Memory Brain contributors 使用 `scripts/build_fallback.py`，从上述 Anderson Winkler 皮层网格生成：固定投影、顶点深度抽样、金紫点线与青色装饰环，不调用模型。该派生图明确按 **CC BY-SA 3.0** 提供，保留原作者与来源，并在 SVG 元数据中记录生成方式及输入文件哈希；仅作静态降级，不表示实时记忆或神经活动。旧 `brain-fallback.png` 不属于公开发布内容。

本项目代码的 AGPL-3.0 不覆盖上述第三方资产的独立许可；发布和再分发时须一并保留这些说明。
