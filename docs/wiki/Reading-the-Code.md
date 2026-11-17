# 从一条请求开始读代码

目标是解释真实行为和故障边界，不是背出每个文件。先按
[代码导读](https://github.com/BigLeopardCat/saudade-blog-agent/blob/main/docs/code-reading.md)打开入口。

## 练习一：一次文章问答

沿请求鉴权、产品工具目录、领域加载、文章读取、模型回复和 SSE 找到调用链。
回答：为什么候选片段不等于全文？为什么工具可见也不能提升账号权限？
在隔离回归中检查全文总结的覆盖和版本；重要结论仍需语义核对。

## 练习二：一次公告批准

沿 preparation、签名卡、transactions、writes、PostgresLedger 和宿主业务事务阅读。
分别假设认领后崩溃、HTTP 响应丢失、业务已提交而本地结果保存失败。
回答：什么状态可以恢复，什么必须保留未知？业务唯一键如何阻止同一批准产生第二次通知？

## 练习三：暂停与补充

追踪 Conversation、Task、HTTP Request 和 checkpoint 的关系。
回答：补充收到何时生效？暂停和断连有何区别？为什么接续不能重置任务预算？

用[trace 排障步骤](https://github.com/BigLeopardCat/saudade-blog-agent/blob/main/docs/troubleshooting.md)核对理解，
并找到对应行为测试。练习不读取生产用户内容、不调用付费模型，也不对生产执行写入。
