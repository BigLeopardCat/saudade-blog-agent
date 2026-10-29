# ADR-0020：会话存储使用薄接口与框架 checkpoint

- **状态**：已采纳；旧图适配已由 [ADR-0024](adr-0024-framework-kernel-and-domain-extensions.md) 替换。
- **影响范围**：ConversationStore、NativeConversationStore 与会话维护接口。
- **判据**：会话存储、checkpoint 接线、取消、审批接续及普通任务恢复的行为回归。

## 决策

`agent/runtime/conversation_store.py` 定义会话快照及存储协议；
`agent/runtime/native_store.py` 通过 LangGraph 公开状态 API 实现读取、提交和维护。
模型—工具循环及审批图使用同一生产 PostgreSQL saver，没有旧规划图或内存降级入口。

快照包含标准消息、待确认项、审批绑定、当前任务与真实执行回执。
`unfinished` 表示存在尚未完成对话提交的任务；图节点已经运行结束也可能尚待这次提交。
`is_resuming` 仅为状态探针，不触发执行。恢复必须由有身份及会话绑定的显式控制请求发起。

- `load` 读取会话状态；历史可见性仍由当前身份和工具访问策略过滤。
- 持久任务的轮次提交已由 [ADR-0031](adr-0031-framework-turn-commit.md) 收敛到父图节点。
  `save_turn` 仅用于受控维护与既有卡片；不能结束活动执行。审批期间追加历史必须保留原绑定。
- `clear_pending` 只清除匹配操作及其任务；普通插话保留原审批，换卡先撤销旧签名。
- `guard` 保持整轮串行及删除屏障；`prune` 使用框架保留策略清理 checkpoint。
- `get_state/update_state` 保留必要维护入口；不暴露旧 plan、DSL 或持久工具游标。

## 恢复与副作用

普通任务使用框架子图 checkpoint 恢复，具体契约见
[ADR-0025](adr-0025-interrupted-task-recovery.md)。审批通过 `interrupt/Command` 恢复，
见 [ADR-0023](adr-0023-confirmation-continuation.md)。未持久化的读取可以重新执行。

业务写入由独立执行账、身份、签名和业务版本校验保障。会话保存失败不能回滚已经发生的
副作用；重复确认读取已有结果，未知结果不盲目重写。checkpoint 不承诺跨服务 exactly-once。

状态写入口核对取消、截止时间与删除屏障。无 saver 时不提供持久恢复或确认写入；
配置后的存储故障向调用方传播，不静默切换内存后端。
