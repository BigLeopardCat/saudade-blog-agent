# ADR-0019：本地 embedding 复用 llama.cpp，并保留 API

- **状态**：提议中（20261023）
- **影响范围**：deploy/embedding、rag/manage.py、评测与运维文档；不改变默认检索开关

## 背景

目标服务器为 4 核 4GB，中文和中英混合质量优先，同时需要无本地推理条件的用户继续用 API。
当前开发机没有 AVX2，Qwen 的长输入成本高。只做 HTTP 探活无法发现 tokenizer 和 pooling 漂移。

## 决策

BGE Q8/F16 与 Qwen Q8 共用固定版本 llama.cpp；使用其 OpenAI 兼容接口，不新增推理网关。
原计划的 BGE ONNX 路线暂不增加第二套运行时。官方 Qwen GGUF 与第三方 BGE 转换分别注明
来源；下载固定 revision/SHA，服务模板固定 pooling、归一化及 tokenizer normalizer 设置。
第三方 BGE 默认大小写/重音处理与官方 tokenizer 不一致，必须通过官方 runtime 元数据
覆盖配置修正，再逐 token 做预检。配置进入 profile 空间身份，避免复用修正前的缓存。

Qwen 官方 tokenizer 的 NFC 规范化也须应用到实际请求；profile 显式声明 NFC，
使用 Python 标准 Unicode 库，不改源文/引用偏移，并把这项配置纳入空间身份。

API 保留为显式配置选项，不自动回退收费 API。原生维度优先：512/1024 向量存储对博客很小，
这轮不引入降维、向量数据库或重排模型。模型与 API 各用单独缓存目录，切换需预检和建库。

## 取舍与复验

GGUF 转换可能与原始 PyTorch/ONNX 存在数值差异，当前成绩只属于固定转换和 runtime，
不能称为官方 BGE 原版成绩。若 token 对齐持续失败、官方兼容性无法保证，或目标机
延迟/质量不达标，重新评估官方 ONNX/成熟推理服务。不能靠放宽 token 上限绕过漂移。

公开中文固定子集用于开发回归；短文与长文桶分开报告，保留全部标注及未标注候选。
样本较小，不能作公开榜单或泛化质量保证。默认 RRF 可能弱于纯向量，不能先开再假定提升。

`tests/test_rag_operations.py` 验证零调用预览、增量执行、空间切换和无效向量；
`tests/test_rag_assets.py` 验证资产校验和不可变 runtime。
真实 tokenizer/模型对照见 [中文评测](../../eval/t2-chinese-baseline.md)，
目标机资源、认证与故障恢复按 [调试手册](../rag-production-debug.md) 验收。
