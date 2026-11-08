# DSH 适配器

把 DSH 的可见原话交给独立的忆核大脑服务，并在本次模型请求中加入带出处的检索证据、显式角色准则和可选生活状态。大脑可以独立运行；本适配器不依赖 AstrBot，也不读取旧角色的库、安装路径或配置。

## 普通安装：使用公开包入口

1. 将完整公开包解压到固定、可写目录，先运行根目录 `install.cmd`，准备独立Python `.venv` 和本机 `private/config.json`。不要分享使用后的这些私人运行文件。
2. 在独立测试profile的原生插件管理页选择“安装已有插件”，输入完整解压目录的 `link:` 路径，例如 `link:<解压目录绝对路径>`。根 `package.json` 通过 `dsh.bundle.patch` 加载 **`adapters/dsh/memory-brain.mjs`**，这是UI安装的公开入口，不是下层 `xiaojiu-brain.mjs`。
3. 公开入口从本机配置读取服务地址和管理口令，并在需要时启动独立内核。不要把口令写进聊天、URL或公开的profile配置。普通npm/tgz复制安装不能带上原目录的 `.venv/private`；本地安装请link已经初始化的完整目录。
4. 关闭其它同用途的写入和召回插件，防止重复；保留角色本身。公开入口先核验宿主：已有快照支持走 `native_snapshot`；精确已知的stock构建可走插件内 `scoped_agent_bridge`；未知构建拒绝启动。该桥是受限进程内兼容适配，**不是官方原生hook**，不修改ASAR、宿主文件或全局原型。详见 [桌面兼容说明](../../docs/DSH-DESKTOP-COMPAT.md)。

公开包配置示意（根bundle已提供，不要重复加载）：

```yaml
enabled: true
autoStart: true
queueNamespace: default
```

管理器方式与根目录接入器方式二选一。安装、兼容检查和错误处理见 [安装说明](../../docs/DSH-INSTALL.md)。stock Desktop在已知构建匹配时不需要运行旧宿主补丁工具；不能对ASAR使用该工具，也不能删掉兼容guard。

## 高级程序接口：底层适配器

`xiaojiu-brain.mjs` 继续提供 `createAdapter(ctx, config)`、`getAdapter(ctx)` 及低层插件接口，供已有独立服务的程序化集成和合成测试使用。它不是给插件管理UI直接安装的单文件包；公开入口负责本机配置、运行时核验和受限桥的选择。高级调用方必须明确提供已验证的请求快照通道，不得绕开宿主兼容检查后冒充完整接入。

以下配置及参数表**仅针对底层接口**，不应复制到公开包的管理器配置；公开入口不接受内联 `baseUrl/token`。底层服务默认地址为 `http://127.0.0.1:18880`，管理口令可通过 `XIAOJIU_BRAIN_TOKEN` 环境变量提供。它是本机服务口令，不是模型API Key。

底层示意配置：

```yaml
enabled: true
baseUrl: http://127.0.0.1:18880
queueDir: ./data/xiaojiu-dsh-queue
captureEnabled: true
recallEnabled: true
timeoutMs: 3000
profileTimeoutMs: 800
lifeTimeoutMs: 800
intervalMs: 1000
tokenBudget: 1200
```

`sessionModule` 默认使用裸包 `@deepseek-ai/dsh-session`。当宿主的包解析范围不同，可以设为自己安装的模块文件绝对路径、`file:` URL 或可解析的包名。没有内置任何开发者机器路径。相对 `queueDir` 以 DSH 进程当前目录为准；正式使用建议指定明确的数据目录。

## 参数

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `enabled` | `false` | 只有显式 `true` 才安装钩子 |
| `baseUrl` | 环境变量 `XIAOJIU_BRAIN_URL`，否则本机 18880 | 新服务地址 |
| `token` | 环境变量 `XIAOJIU_BRAIN_TOKEN` | 本机服务管理口令；优先用环境变量 |
| `queueDir` | 用户目录 `.xiaojiu-brain/dsh-queue` | 原话持久队列，不能多个进程共用 |
| `captureEnabled` | `true` | 是否从会话日志采集；关闭后不采集新内容 |
| `captureAfter` | 不设置 | 显式切换边界 ISO 时间；只归档边界之后发生的持久输出事件，默认仍完整补采 |
| `recallEnabled` | `true` | 是否请求记忆检索；关闭后仍可归档，独立角色/生活通道由各自服务开关控制 |
| `timeoutMs` | `3000` | 每次检索前置过程总预算，包含当前消息确认及检索 |
| `profileTimeoutMs` | `800` | 独立角色/准则请求的等待上限，配置值最多800毫秒 |
| `lifeTimeoutMs` | `800` | 独立生活状态请求的等待上限，配置值最多800毫秒 |
| `intervalMs` | `1000` | 后台补采和投递间隔，最小 50 毫秒 |
| `catchupLimit` | `200` | 每扫描批的事件上限，每次最多推进五批 |
| `maxPending` | `20000` | 待送达上限；满后停止推进，不丢弃旧原话 |
| `tokenBudget` | `1200` | 请求服务返回的证据预算 |
| `sessionModule` | `@deepseek-ai/dsh-session` | 宿主的真实 SessionStore 模块 |
| `importantToolEnabled` | `true` | 在原生 ToolRuntime 可用时提供用户明确要求的重要记忆工具 |
| `importantTimeoutMs` | `5000` | 重点保存的原话确认与服务回执总预算，限制为 500–15000 毫秒 |
| `toolsModule` | 优先从显式 `sessionModule` 所在安装解析，否则 `@deepseek-ai/dsh-tools` | 可显式指定同一 DSH 安装中的工具模块 |

服务面板的开关与适配器开关是两层控制。关闭服务端接收时队列保留未确认消息，不假装已送达；需要停止本机采集，请关闭适配器 `captureEnabled` 或卸载适配器。已有队列不会因关闭而被删除。

## 用户明确要求的重点记忆

原生工具 `memory_remember_important` 由正常对话模型根据当轮真人的明确要求决定调用，例如用户要求“一定要记住”。没有正则触发器，也没有每轮额外的分类模型请求。普通聊天、情绪表达、检索内容和助手自己的建议不构成保存授权；工具也不能覆盖人格或行为准则。是否理解并调用工具仍取决于聊天模型，不能承诺每次自然语言表达都被正确识别。

工具只能选择当前会话的原始用户消息及其 Unicode 码点片段，不能提交改写后的事实、所有者或会话标识。省略来源时保存当轮原话全文。宿主从真实 `exec.agent.session` 绑定会话和最新用户请求，先等待所有相关原话的持久 ACK，再调用服务；服务再次校验归属、真人来源、时间顺序和片段范围。它不会把助手推测升级为用户事实。若消息缺失、超时或没有明确回执，工具返回可见失败，不允许模型宣称已经记住。

正常成功会创建带原话出处的字面事实，确认置顶和重要性 `0.95`；可选 `normal / only_if_asked / never_say` 只允许收紧来源限制。工具不创建秘密主题标签。重复调用同一请求走幂等回执；如果用户随后在面板归档、取消置顶或修改重要性，旧请求重放不会覆盖这些编辑，回执会如实显示当前状态。只有 `confirmed_important=true` 才能说已作为当前重点记忆保存。

工具服务尚未加载时，原话和召回继续工作；适配器通过可选的 Cordis 依赖等待 `tools`，就绪后注册，卸载时清理。状态中的 `important_tool` 区分等待、就绪和不可用。旧服务没有此端点时会明确报失败，不伪造成功。工具说明、调用结果和可能的正常工具续轮会占用聊天上下文；这里不承诺零 tokens。

验证原生 ToolRuntime、真实 Session 与 AgentLoop 的合成闭环：

```powershell
$env:DSH_TOOLS_MODULE = '<当前安装>/node_modules/@deepseek-ai/dsh-tools/lib/index.js'
# 同时提供下方验证段中的 DSH_SESSION_MODULE 和 DSH_CONTEXT_MODULE
node tests/test_important_tool.mjs
```

测试只用本地合成协议回应，不调用真实模型、不修改正式原话或配置；没有指定同一套运行时模块时会明确 SKIP。

## 写入和检索的边界

- 只接收 `user/message` 中 `role=user, source.kind=user` 的真人消息，以及 `assistant/message` 中 `role=assistant, source.kind=model` 的已定稿消息；带 `source.form` 的注入、缺失来源、被中断的助手事件不收录。
- 只拼接可见 `text` 块。推理、工具调用参数、工具输出、系统消息和注入内容不入原话库。助手同一结算事件中若既有可见文本又有工具块，只收可见文本；真实承诺、独立建议不会被当作用户复述删掉。
- 稳定身份为 `(conversation_id, message.id, role)`，不是容易冲突的 turn 号。同身份正文变化会告警并拒绝覆盖。
- 从宿主完整持久事件日志补采，用磁盘游标推进。实时追加和后台补采走同一通道，旧缺口未解决时新消息不能越过。
- 真实 AgentLoop 在 `pre-step` 之后才追加本轮 `user/message`。因此适配器会折叠持久 `agent/inbox/spliced`：只在输入被真实认领时归档，取消的待处理输入不归档；发言时间锚定其持久入箱时间。稍后的同 ID 可见事件走同一幂等键，不额外写一条。绝不从临时 payload 字符串直接绕过持久化。
- 先持久化原话，再记录 seen 和游标；投递为 at-least-once，由服务端幂等消除重放。只有明确 `ok=true` 且 `accepted=true` 或 `duplicate=true` 才算送达。
- 本轮用户问题先进入持久队列，在总预算内按顺序等待服务确认，再带 `current_message_id` 检索。服务能排除本条问题，避免把题干当成旧记忆。尚未确认就省略本轮记忆检索，不按文本猜测定位；独立角色和生活状态请求不等待此ACK。
- `agent/pre-step` 保留其他插件的决定，只把新证据放进返回的 `requestMessages`；不会调用 Session.append 写入证据。同一问题的工具步骤复用结果，避免重复调用和重复段落。
- 公开入口在stock兼容模式下由受限桥消费**最终**pre-step决议，不以较早监听器暂存的段落覆盖外层插件决定。桥只绑定经核验的当前agent实例、turn、step和取消信号，在本次冻结请求中追加消息；工具续步仍使用按真人消息ID缓存的同一批材料。它不修改宿主文件、全局AgentLoop原型或Session日志。
- 后端断服、超时或身份不明会留下可见降级代码，同时放行聊天。不会编造“已想起”。

补采依赖宿主仍保留原始日志，并且该 Session 曾在当前进程准备或恢复。已被宿主永久移除、且从未入队的事件不能恢复；从未重新打开的其他历史会话不被全盘扫描。本版不宣称在磁盘损坏或断电下达到 exactly-once。

迁移旧库但历史外部消息 ID 不完整时，可在 **DSH 已停止且最终旧库快照完成后** 固定 `captureAfter`。边界之前的可见输出不再重放，游标仍逐步扫描并保存；边界后认领的旧待处理输入仍归档，原始入箱时间保持不变。不能随意用“现在”替代停稳快照边界，否则可能漏掉尚未迁移的数据。新安装不要设置此项。

## 独立角色、准则和生活通道

当前适配器并行请求记忆召回、`POST /v1/context/profile` 和 `POST /v1/context/life`。角色/准则整段最多384个估算tokens，生活段最多160个估算tokens，均独立于 `tokenBudget`；关闭记忆召回、原话尚未确认或召回超时，不应连带抹掉已成功取得的独立段。各段关闭或为空时不注入，错误只报告类别。旧服务没有新端点时保留其已有兼容召回结果，不伪造独立通道已经启用。

角色段只读取所有者明确填写并启用的自我档案、表达偏好和适用准则，不读取宿主人格文件快照，也不从普通记忆推断行为指令。准则有显式版本、修订、用户层优先级和所有者/指定会话范围；优先级不能覆盖系统、开发者等更高层指令。整项装入，装不下时报告遗漏ID和数量。详情见 [角色与约定](../../docs/PROFILE-IMPORTANT.md)。

缓存键为会话ID和当轮真人消息ID。同一轮的工具续步复用第一次取得的证据、角色和生活段；下一条真人消息刷新。它减少同轮重复请求，不是实时订阅：正在运行的同一轮不会因面板编辑而立即重取。注入只进入本次模型请求快照，不写入Session历史；模型在最终回复中生成的内容仍可由宿主正常归档。

独立段请求各自默认最多800ms，记忆前置总预算默认3000ms；几路并行后仍等待有界结果，慢召回可能带来约3秒前置等待。没有新增逐轮分类模型，不等于零tokens：角色、准则、生活文字及工具说明都占聊天输入，后续工具步骤也有正常上下文成本。估算额度使用UTF-8字节估算，不是供应商实际计费usage。

检索返回的规范化claim仍是未核实提案；角色来自回读的来源记录，不自动等于事实主体。时间换算只说明日期指代，未核实的来源时间、模糊锚点和互斥说法继续存疑。关系功能仅为精确规范化名字的一跳候选导航，不承诺完整图谱或别名识别，见 [语义记忆边界](../../docs/SEMANTIC-MEMORY.md)。

## 队列与故障处理

队列包含 `pending/`、`delivered/`、`failed/`、`cursors/` 和去重日志。它们含明文原话，应放在用户受控目录，备份时同样保护。Windows 同一账号的任意进程可读取该账号可读文件；本版是个人本机应用，不承诺不同同账号会话之间的操作系统隔离。

只有已送达副本会按七天保留策略清理。待送达和失败消息不会因年龄或容量被自动删除。400/409/422 为明确永久拒绝，转入 `failed/` 等人工处理；401/403/429、服务错误、未知回应和网络异常保留待送达。告警只写类别和时间，不写正文或口令；每类日志一分钟节流，状态计数仍累加。

一个队列目录只允许一个写入进程。服务地址变化时须使用新队列目录，防止把旧原话误送到另一后端。不要在进程运行时手动删除 `writer.lock`。停止插件会还原自己安装的 Session 包装器，等待当前投递退出后释放自己的锁。

## 验证

基础队列与过滤检查不需要 DSH：

```powershell
node tests/test_plugin.mjs
```

运行真实 SessionStore 集成时，显式指定当前安装中的两个模块（不要把自己的机器路径写入源码）：

```powershell
$env:DSH_SESSION_MODULE = '<当前安装>/node_modules/@deepseek-ai/dsh-session/lib/index.js'
$env:DSH_CONTEXT_MODULE = '<当前安装>/node_modules/@deepseek-ai/cordis/lib/index.js'
node tests/test_plugin.mjs
```

测试使用新建临时队列、真实 SessionStore 的 prepare/append/持久日志恢复，以及本地 HTTP 协议服务，不访问正式库。覆盖严格 ACK、失败保留、容量缺口顺序、稳定 ID、源过滤、助手真实承诺、当前消息确认、请求证据不进历史、工具步复用和有界降级。没有提供模块时，集成部分会明确打印 SKIP，不能把它当成真实 DSH 全部通过。

这组测试验证适配器契约，不等于完整 AgentLoop 已消费证据，也不等于跨天记忆能力分数。发布验收还应使用独立宿主与新服务完成一次“写入→关闭历史窗口→新请求回想→追到原话”的实际闭环。

本仓库另有 `tests/integration/run.mjs`，通过 `DSH_RUNTIME` 指定只读安装目录，使用真实 AgentLoop、SessionStore、工具调度和新大脑随机端口服务，向本地假模型协议端点发送实际请求。它核对首次请求、工具后请求、当前问题排除、原话顺序、幂等、推理与工具过滤、历史无注入及运行时文件哈希。配套 `brain_fixture.py` 随测试源码交付，运行时新建临时合成数据；无需既有 artifacts。所有数据为合成，不调用真实模型，不代表记忆测评提分。

完整集成还通过真实 `ctx.plugin(module)` 加载并等待该版本 Cordis 的 `fiber.await()`，确认 ACTIVE；卸载时确认自身会话钩子恢复、队列锁释放。插件 `apply()` 不返回控制器对象（那不符合 Cordis Effect 契约）；`createAdapter()` 和 `getAdapter()` 是独立控制/测试接口。

## 来源说明

持久队列的设计借鉴已验证过的 `fish-rawqueue` 不变量：先落盘、严格 ACK、恢复序号、未送达不清理。代码在新协议下重新实现，没有导入旧插件、旧数据、旧运行路径或旧的题目特定检索规则。
