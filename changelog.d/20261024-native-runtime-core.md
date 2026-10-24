# 原生运行核心与旧规划图分离

- 增加独立 NativeRuntime：正文流与最终 TurnResult，不再委托旧引擎。
- 增加薄 NativeConversationStore：通过公开框架 checkpoint API 接续历史和确认卡。
- 工具目录复用同一授权组装，纯聊天身份不绑定工具；原生模型不再收到委托 schema。
- 明确关闭、取消、保存失败、过期卡和未知写入结果的恢复边界，并增加离线回归。
- 服务传输消费者和默认运行路径的切换单独接入；全量线上基线继续挂起。

设计与限制见 [ADR-0021](../docs/adr/adr-0021-native-runtime-core.md)。
