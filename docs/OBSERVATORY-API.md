# 忆核大脑 · Memory Brain：本地脑区概览数据契约

`GET /v1/observatory` 是本机管理登录后的只读统计投影。HTTP层沿用已有
`Authorization: Bearer ...`、同源与Host校验、`Cache-Control: no-store`和
`{ok:true,data:...}`外层。本模块仅提供`observatory.snapshot(app)`；路由和页面由宿主接入。
接口没有owner/user参数，始终使用`app.owner`，不触发整理、模型、网络、维护或写库。

公开界面品牌固定为 **忆核大脑 / Memory Brain**。包名、数据库、认证和DSH路由不因显示名称改变。

## 返回结构

- `schema_version: 1`、`observed_at`：本次投影的Unix秒时间。
- `brand: {en,zh}`、`service: {version,build_id}`：当前版本信息。
- `storage`：`messages/documents/memories/vectors/original_messages/imported_notes/owner_notes`，
  `by_kind: {raw,fact,summary}`，`by_status: {active,archived,deleted}`。这些是现有Store按owner统计的
  数量；`raw`是文档切块数，`original_messages`才是原始消息数。导入旧笔记和用户手写笔记单列。
  另有`conversations`（同owner不同会话数）和`latest_message_at`（同owner的消息最大recorded_at，空库为null）。
  后者是记录所载时间，不是服务心跳、当前在线状态或最近一次模型活动证明。
- `pipeline.features`：接收、召回、语义、整理、同所有者跨会话、情绪、角色笔记、生活、生活带入聊天和保留策略的开关。
- `pipeline.policy`：实际安全调度设置`token_budget/derive_batch_messages/derive_max_wait_seconds/derive_interval/life_interval_seconds`。
  分别是检索证据token预算、每批整理的消息数门槛、稀疏消息最长积累秒数、整理检查间隔秒数和生活动作间隔秒数。
  读取值仅在当前设置允许范围内返回，未知/无效时为null；消息数不是对话轮数，积累时间不是故障下的完成保证。
- `pipeline.jobs: {total,by_status,by_type}`：SQL先按owner过滤，再按`kind/status`分组。
  `by_type`含`embed/derive`，状态含`pending/running/retry/waiting_provider/blocked_policy/done/failed`。
  任务数不是API调用次数；没有返回任务正文、会话名或私人错误详情。
- `pipeline.embedding/derivation`：仅`configured/model/last_error`；整理另有`metering_paused`。
  配置存在不等于刚做过连通性探测，`last_error`仅安全类别。
- `usage`：已持久化的**后台整理**调用账本投影，不含DSH主聊天、工具步或旧版本未记录用量。
  包含`attempts/reported_responses/unknown_usage_attempts/since/by_outcome/missing_fields`、
  `input_tokens/output_tokens/cache_hit_tokens/cache_miss_tokens/total_tokens`及明确范围标签。
  未知token量保留`null`，不以0表示免费；`legacy_usage_included`固定false。
  仅当cache命中和未命中量均已知且合计大于0时，`cache_hit_ratio=hit/(hit+miss)`，否则为null。
  该比例只对应**已报告的后台输入样本**，不能显示成全部聊天的缓存命中率。
- `life`：仅`enabled/context_enabled/timezone/rhythm/needs/needs_kind/last_tick_at/next_tick_at/error/counts`。
  不返回`current`、书摘、书名、事件文字或待办标题。`needs_kind=rule_simulation`；四项needs
  是0到1的规则投影，不是真实感受。生活不可读取时给出安全错误类别及明确空值。
  读取故障时`status=unavailable`，四个counts与生活节点count均为null，不能误显示成实际0条。
- `pad`：仅`enabled/pleasure/arousal/dominance/signal_only`，三个信号范围-1到1；无notes或tags。
- `self`：仅`enabled/notes_count/style_configured`，无角色笔记和表达偏好正文。
- `scene`：下面说明的固定聚合节点与架构示意边。
- `activity`：下面定义的6秒记忆任务活动投影，另可由`GET /v1/activity`单独获取。

## 真实任务活动投影

`activity_snapshot(app)`只查询当前owner的作业聚合，并在锁内读取召回记录的owner和完成时间at。
不读取召回query/完整trace、消息正文或模型推理；不新增模型调用、原话写入或DSH钩子。

返回结构固定为：

```json
{
  "kind": "memory_task_activity",
  "observed_at": 0,
  "window_seconds": 6,
  "state": "idle",
  "running": {"derive": 0, "embed": 0},
  "recent": {"recalls": 0, "completed_jobs": 0},
  "waiting": 0,
  "expired_running": 0,
  "last_event_at": null,
  "chat_model_state": "unobserved",
  "representation": "decorative_work_signal"
}
```

示例中的0是空队列的真实零；发生读取错误时`state=unavailable`，全部数量为null，不能当作空闲。
有效running必须`status=running`、`lease_until>now`且`updated_at<=now`；过期running独立计数，
同时加入waiting。waiting另包括pending/retry/waiting_provider/blocked_policy，已终止failed不算待执行。
completed_jobs只统计updated_at在`[now-6,now]`的done作业，不能当成API调用或完成对话轮数。
recalls仅统计同owner、at落在同一窗口内的已完成召回；不包含当前进行中的召回或其它用户。
future时间不参与任何活动计数，last_event_at为该窗口内有效作业更新或召回完成的最大时间，
窗口内没有事件则为null。状态优先级为busy → recent → waiting → idle。

界面每2秒轮询轻量活动接口，以真实运行任务或最近完成事件驱动波形，隐藏标签时暂停轮询。
波形是任务活动的装饰性表达，不是脑电、心率、思考强度或神经活动。没有DSH主聊天遥测，
因此chat_model_state明确为unobserved，不能把记忆worker空闲解释为主聊天空闲。

## 场景是可视化示意

`scene.metaphor=brain_regions`采用功能脑区聚合，不再使用星系隐喻。
`anatomy=illustrative_not_medical`明确这是软件功能示意，不是医学脑区或真实神经解剖。
保持兼容字段`scene.layout=illustrative_not_semantic`，`particles=decorative_not_one_per_memory`，
另有`activity=decorative_not_neural_activity`。装饰粒子不对应逐条真实记忆，坐标不表示语义距离；
动画不表示真实神经活动、情绪或思考。

九个聚合节点固定为`raw/facts/summaries/semantic/self/emotion/life/queue/imports`。每项为
`{id,label,count,unit,enabled,panel_page,panel_kind?,description}`，数量分别来自原话、派生索引、摘要、
向量、角色笔记、三维PAD、生活事件、未完成或异常任务、导入笔记。三维PAD的计数是信号维度，
不能和记忆条数相加。`facts`必须显示“派生事实索引”，不能显示“已验证事实”。关闭整理只暂停新增派生，
已有索引仍然保留；各节点的`enabled`是对应功能开关，不代表已删除其数量。

`edges_kind=architecture_illustration`，边仅为固定处理架构：原话派生、向量索引、队列处理和导入保存。
它们不是抽取出的实体关系或实际每条记忆的关联。不返回任何私人记忆样本、全文、向量数组、
source metadata、人物名、书名、凭据、配置路径或提供商URL。

节点的`panel_page/panel_kind`是前端导航提示。跳转同源控制面板，由用户主动选择记录，再调用
已有认证的来源接口查看原话；脑体概览初始化和动画循环不得自动抓取原话全文。控制面板需提供返回大脑首页入口。

## 运行与隐私边界

页面与控制面板复用同源、当前浏览器标签会话的管理登录。长期管理口令不得放URL；一次性登录票据
仍在消费前立即从地址清除。未登录只展示空壳或明确标为合成的数据，不展示真实统计。

所有图形库、字体和资源本地打包，不使用CDN、远端分析或遥测。前台10到15秒刷新一次统计即可；
隐藏标签暂停轮询，WebGL帧动画与API抓取解耦，控制粒子/DPR上限并提供减少动态效果。接口不应
每帧运行、不调用检索或嵌入来制造活动、不执行真实性能或完整记忆能力评分。

本次仅增加只读投影，无数据库schema迁移，不改变原话、记忆算法、模型路由、生活规则或工作队列。
现有SQLite在线备份包含全部持久数据；升级仍应保留当前代码/静态文件及在线快照，不能用运行中
单独复制的主db文件覆盖WAL数据。页面不自行恢复旧库，不修改现有所有者作用域。
