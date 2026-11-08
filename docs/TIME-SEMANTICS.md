# 时间语义：按有出处的发言时间换算

这层只把原话中的日历表达换算成可核对的时间范围，不把计划、条件、猜测或提到的日期认定为已经发生。没有额外模型调用，不改系统时间，不从当前查看时间猜历史日期，也不覆盖原话。

## 输入与来源

`normalize_time(expression, anchor_message, timezone='UTC', *, source_start=None, source_end=None)` 接收原话和可选的已引用 Unicode 字符跨度。表达必须逐字出现在该跨度，不能从同一条原话相邻的未引用内容借用日期。锚点来自这条原话的 `recorded_at`；原 `metadata.recorded_at_iso` 与该时间戳一致时保留其原始时区偏移作为出处。

区间按参数指定的服务时区解释，`anchor_recorded_at_iso` 保留原发言的偏移，两者职责不同。例如原发言是上海 6 月 10 日零点半，若明确要求按 UTC 换算，“今天”就是 UTC 的 6 月 9 日，不会暗中混用两套日历。

`source_kind=imported_memory` 的旧摘要或笔记时间只证明旧记录的时间，不能视为已核实的发言时间。此类来源即使带有形式正确的 `recorded_at_iso`，三个入口仍返回 `unsupported/unverified_source_time`，不生成日期区间；记录时间可单独留作追溯。模型缓存或重读时自报 `resolved` 也不能解除这条限制。

输出含 `schema_version`、原样 `expression`、`anchor_message_id`、`anchor_recorded_at_iso`、`timezone`、`start`、`end`、`precision`、`status`、`reason`、实际 `source_ref`。`status` 为 `resolved`、`ambiguous` 或 `unsupported`；不确定时区间为 `null`。固定 `semantics=date_reference_only_not_occurrence` 提醒调用方：这是日期指代，不是事件发生证明。

## 有限、通用的语法

- 严格 `YYYY-MM-DD`、明确年份的中文年月日、明确年份的英文月份日期。
- 今天、昨天、前天、明天、后天及对应英文表达。
- 上周、本周、下周加星期几，或者明确 `last/this/next week` 与英文星期。自然周从周一开始；单独的周表达返回整周范围。
- 整数天数、周数的前后偏移，例如“三天后”“两周前”“3 days ago”“in 3 weeks”。按日历相加，不按固定秒数推算。

英文裸 `N days before/after/later` 没有明确“从发言现在起”的锚点，保留 `ambiguous/external_event_anchor_required`；只有 `ago`、`from now`、`in N days` 等明确指向发言时点的形式才按原话日期换算。例如“发布会在 6 月 20 日，复核在它之前两天”不能把“两天前”误算成发言日减两天。中文“会后三天”“上线前2天”等事件相对形式同样保留未解决状态。这层不做事件日期推理，也不会从另一个日期擅自推导事件关系。

没有年份的“6 月 5 日”、`June 5`，未指定哪周的“星期五”，上下旬、改天，或有不同口语解释的 `last Friday`、`next Friday`，均保留歧义，不擅自选最近年份或某一星期。超出语法的表达明确标为不支持；这不是覆盖所有自然时间语言的承诺。

日期使用半开区间 `[start,end)`。两端分别取当地日历边界；夏令时切换日可以是 23 或 25 小时，不硬加 86400 秒。罕见的不存在的当地日期边界保留歧义，不伪造一个本地时刻。

## 派生元数据与旧原话

`time_metadata(metadata, checked_sources, timezone)` 只接受已验证来源里的表达。规范提议为 `event_time:{expression,anchor_message_id}`；旧字符串提议只有在能唯一确定一条已引用原话时才换算。锚点未引用、越界、无法定位或多来源歧义时保留未解决状态，不令整个派生批次失效。模型自报的 `start/end/status` 不可信，代码始终从原文与原时间重算。平铺与嵌套主张的时间声明冲突时不代选；同表达且一处未声明锚点、一处已声明同一来源可兼容。

`source_times(message,start,end,timezone='UTC',limit=3)` 在已选中的引用跨度内扫描相同的通用语法，按原文位置返回少量换算，并保留整条原话的 `expression_start/end`。它不扫描相邻未引私密段，不读取整个库，也不反写旧记忆。检索层可在既有预算内把这些有出处的换算作为辅助说明，不能把解析失败等同于“没有记录”。

保留 `recorded_at`、理解事件日期、判断旧说法是否被明确更正，是不同职责。此层不解决实体别名，不根据“最新”自动解除矛盾，也不把模型主张升级成经过核实的事实。
