# 产品与领域装配

`NativeRuntime` 默认装配博客产品；应用可通过 `application=` 显式替换产品配置。
执行仍由 LangChain `create_agent` 与 LangGraph 持久审批图负责。

| 位置 | 职责 |
| --- | --- |
| `agent/runtime` | 会话和框架接线、预算、取消、事实存储、顺序执行与审批恢复 |
| `agent/products/blog/application.py` | 博客产品的显式装配入口 |
| `agent/products/blog` | 人设、提示词、工具目录、历史访问、调用授权和文章证据 middleware |
| `agent/domains` | 业务接口、目标和版本核验、确认准备及执行回执 |
| `examples/inventory_agent.py` | 无博客工具、人设或站点 URL 的只读库存示例 |

## 添加能力

为现有博客添加领域时，定义标准工具并在产品目录显式注册，声明工具说明与相应权限。
新增业务规则优先使用 LangChain `AgentMiddleware`；领域状态由 middleware 的标准状态 schema
声明，持久子图保存并在确认后恢复。领域说明和参数只描述能力，不做关键词意图路由。

博客的 `ArticleMiddleware` 提供文章结果渲染、正文证据与引用校验。
`BlogToolMiddleware` 在每次新调用前核验身份和权限，并核验浏览器及设备回执。
通用顺序 middleware 包住整个处理链，确保拒绝也推进批次、回执在后续准备前被收集。

工具结果渲染接口用于保留现有结构化工具结果及有界显示。
原始媒体只在请求内使用；标准 Agent 状态保存引用。提示词、工具说明及其顺序保持稳定，
新增 middleware 本身不会发起模型调用。

## 替换产品

库存示例通过 `application=InventoryApplication()` 接入同一个运行时。
它显式提供提示词、工具目录、历史工具访问规则、调用者上下文及只读确认策略；
共享会话持久化、模型传输、工具发现和预算。示例工具使用合成数据。

```python
from langgraph.checkpoint.memory import InMemorySaver

from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from examples.inventory_agent import InventoryApplication

runtime = NativeRuntime(
    NativeConversationStore(InMemorySaver()),
    application=InventoryApplication(),
)
```

内存 saver 适合示例。生产部署沿用持久化配置、可信身份以及业务后端授权。
新增写入领域还须实现有效确认绑定、版本校验和幂等执行，不因接入框架而自动获得这些保证。

验证：`tests/test_product_assembly.py` 覆盖非博客读取、多轮来源事实与共享预算；
博客相关 suites 继续覆盖权限、卡片、回放、文章证据、缓存前缀和多模态行为。
