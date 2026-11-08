# 忆核大脑：真实记忆脑皮层视图

`memory_map.snapshot(app, *, days='30', kind='memories', limit=200, offset=0, until=None)`
为认证的本机图页提供真实文档记录；`detail(app, document_id)`提供用户明确点击后的详情。
宿主负责接入HTTP鉴权，本模块不接受owner/user作用域参数，固定使用`app.owner`。
不写数据库、不调用模型、不生成摘要、不连接新provider、不读取DSH私人会话日志。

## 筛选和分页

- `days`白名单为7/30/90/all，返回规范字符串；天数是截止时间向前的连续24小时窗口。
- `kind=memories`含fact和summary；另可选fact/summary/raw/all。
- `limit`须为1至300的整数，`offset`须为0至100000的整数；布尔值无效。
- 首请求不传until时使用服务器当前时间并回传。后续使用同一个until，不在加载更多时重取。
  until必须有限且不超过当前时间1秒；容许的时钟漂移会夹到当前时间，不能放出未来记录。
- 只选当前owner、active状态、recorded_at不超过until的文档，按recorded_at DESC、id DESC分页。
  同时要求created_at<=until，使本轮加载过程中生成/回填的旧日期记录不插入已有分页。
- 返回`schema_version/observed_at/until/days/kind/total/items/offset/next_offset/has_more`，
  `layout=illustrative_cortical_placement`、`timestamp_basis=recorded_at`。超出最大分页范围时
  truncated=true且不给出无效的下一页offset。

这不是数据库事务快照：并发删除、归档或披露设置变化仍立即生效，可能影响后续页。
刷新视图主动开启新的until。固定until不能作为永久审计快照；完整保留应使用已有在线备份。

## 安全返回字段

每个item只有：

`id/kind/recorded_at/preview/restricted/pinned/importance/source_ids/source_count/role/source_kind`。

id是真实document_id，可作为图中节点身份。正常preview是原存text的最多220字符片段，
不另调模型改写。source_ids至多8个当前owner、当前文档会话、合法且非未来的直接原话来源id；
source_count是其完整去重数量。importance范围0到1。role仅user/assistant/null，不把混合说话者
或导入旧笔记的未知原说话者强行归给某一人。

source_kind为original/imported_note/owner_note/derived。旧imported_memory及以它为来源的投影
标为imported_note，role=null；它是导入笔记，不代表已核实的用户或助手原话。用户手写笔记
标为owner_note。其它正常原话文档为original，模型摘要/事实投影为derived。

recorded_at保留现有数据含义：派生文档通常为直接所引原话时间的最大值，不是整理时间或事件发生时间；
导入旧笔记是旧记录时间，不自动等于原发言时间。原生未知legacy时间保留0，不拿created_at或导入时间
补成新的发生日期，所以默认近30天窗口不会误画这类未知时间记录。

## 披露保护

文档disclosure不是normal、来源更严、生成上下文更严或来源无效时，restricted=true且preview固定为：

“受限记忆，请在控制面板明确查看”

保护同时检查document_sources、整个document_dependencies生成上下文，以及导入笔记的
message_dependencies来源祖先。已归档的受限投影仍保护同一原话的重叠跨度；不把公开原话切块
当作绕开私密派生记忆的通道，非重叠公开跨度不会因此被一概封锁。来源缺失、跨owner/会话、
未来来源和非法跨度均保守隐藏预览。列表和详情不返回topics、tags、metadata、quote、向量或凭据。

用户点击“展开完整记忆”后，detail返回相同安全字段并增加full_text：正常记录是原存全文；
受限记录仍为null且保持占位，不能借详情自动解密。另有sources数组，正常记录按实际引用跨度返回
`{message_id,start,end,role,recorded_at,source_kind,text}`，最多涉及source_ids中的8个不同来源id。
同一来源有多个引用跨度时分别列出，不把跨度间的其它内容拼进来；text严格为原话的`[start:end]`
切片，不能直接返回整条原话及其未引用前后缀。受限记录的sources始终为空数组。
不存在、异owner、非active或未来记录以KeyError交由宿主映射404。受限内容只能由用户在既有控制面板明确查看。

脑皮层上的位置和连线属于视觉布局，不是医学脑区、语义距离或真实神经拓扑。
每个记忆节点有真实文档身份；绘制数量与可视化抽样/分页状态需明确展示，不能声称已经显示整库。
