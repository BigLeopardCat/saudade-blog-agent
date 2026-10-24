# 原生运行时与旧图兼容边界

当前项目是迁移中的双运行时，而不是已经完成的单一运行时。`HybridArm` 是生产迁移入口；
它负责原生工具循环、会话存储、确认生命周期和资源控制。旧 planner/execute/gate 图仍是
兼容运行时，由未迁移能力、旧 checkpoint 状态和部分维护入口使用。

## 允许的依赖方向

```text
server / transport
        ↓
runtime / domains ──→ shared auth, budget, checkpoint, idempotency
        │
        └──────────→ legacy compatibility bridge (temporary)
                         ↓
                    legacy graph
```

原生 `agent/runtime/` 和 `agent/domains/` 不应直接导入旧 graph、旧 planner、旧 workflow
或计划文本 DSL。仍需读取旧 checkpoint 形状时，必须经过一个有名字的兼容适配器；适配器应
只负责数据转换，不重新引入旧图的模型决策、关键词路由或执行语义。

## 目前仍保留的兼容职责

- 未迁移请求和无法由原生会话接管的状态可以交给旧图；
- 旧图的 checkpoint 结构仍被会话存储适配器读取和写回；
- 旧确认、维护接口、评测夹具和部分工具注册仍需要兼容导入；
- 项目默认配置和生产配置是两个事实：代码/示例默认仍可能是 `graph`，生产可以显式选择
  `hybrid`，不能据此声称旧图已经删除。

## 旧图退役条件

只有以下条件全部满足，才可以删除旧图和兼容桥：

1. 剩余业务能力都有原生领域入口，且失败、取消、确认、幂等和未知结果都有请求级验收；
2. 原生会话存储不再依赖 graph state、旧节点名或 `graph_input`；
3. server、维护接口和评测入口不再构造或调用旧图；
4. 事件、工具结果和回复持久化协议完成一次收敛，不再需要旧字符串计划/回执解析；
5. 真实任务集证明切换默认运行时不会把请求静默交回旧图；
6. 只保护旧实现的测试、提示词和 workflow 注册表已经删除或转成原生契约测试。

在这些条件满足前，新增能力应进入 `agent/domains/` 和 `agent/runtime/`，不得继续扩大旧图
的职责。迁移 PR 应同时说明：原生入口、允许的兼容调用点、旧入口的删除条件和对应验证。
