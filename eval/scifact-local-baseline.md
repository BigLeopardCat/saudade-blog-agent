# SciFact 与本地 embedding 首轮验证

2026-10-23，验证公开语料评测与本地量化模型的可运行性。本次没有修改生产模型配置。

## 完整公开语料

使用 [BEIR 提供的 SciFact](https://github.com/beir-cellar/beir/wiki/Datasets-available)，
保留完整 5,183 篇文档和 300 个 test 查询，不按相关标注裁剪语料，也不抽取前 N 条。
其内容是英文科学论断和摘要，不能代表中文博客、多篇必要证据或无答案场景。
按 [SciFact 原始许可](https://github.com/allenai/scifact/blob/master/LICENSE.md) 记录
claims/annotations 的 CC BY 4.0 与摘要的 ODC-By 1.0；不把本项目的 MIT 许可套在数据上。
语料压缩包及正文不入库，报告记录下载包 SHA-256、输入文件哈希和代码哈希。

在独立开发环境安装锁定 dev 依赖后运行：

```bash
.venv/bin/python eval/prepare_scifact.py --download --output eval/data/scifact
# 已有压缩包时，改用 --archive /path/to/scifact.zip，无网络操作。
.venv/bin/python eval/offline_rag_eval.py eval/data/scifact \
  --output eval/report/baseline_scifact_bm25_cliff.json
.venv/bin/python eval/offline_rag_eval.py eval/data/scifact --no-cliff \
  --output eval/report/baseline_scifact_bm25_no_cliff.json
```

下载是显式动作；准备过程核对固定校验和与完整基数，失败不留下可用数据目录，已有目录
拒绝覆盖。完整语料含未在 test 正相关 qrels 出现的背景文档，不能只保留正例。

| 生产 BM25 设置 | Success@10 | Recall@10 | RR@10 | nDCG@10 |
| --- | ---: | ---: | ---: | ---: |
| 默认断崖 | 0.7667 | 0.7505 | 0.5749 | 0.6131 |
| 关闭断崖 | 0.7667 | 0.7505 | 0.5749 | 0.6131 |

300 个查询均计入分母。断崖裁剪了 17 个查询的候选尾部，但当前 qrels 下逐查询指标
均未改变；未标注文档不能因此认作无关。不证明断崖对其他语料安全或有效，
不据此改参数。本项目包含自身 tokenizer、切片及文档聚合，不能把这组数当成 BEIR
官方 BM25 复现成绩。参数选择应使用单独的开发集，不能根据 test 分数反复调优。

完整逐查询结果见 [默认断崖报告](report/baseline_scifact_bm25_cliff.json) 和
[关闭断崖报告](report/baseline_scifact_bm25_no_cliff.json)。在当前开发机，建索引
分别约 2.27/2.55 秒，查询 P50 约 71/80ms、P95 约 140/169ms（最近秩，300 样本），
进程峰值 RSS 约 133/134MiB。这是一次运行的观测，不作性能门槛。

## Qwen3 0.6B Q8_0 服务探测

直接使用 [Qwen 官方 GGUF](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF)
和 [llama.cpp 官方服务](https://github.com/ggml-org/llama.cpp/tree/b11829/tools/server)，
不引入新的推理服务实现，不给 Agent 的每个 worker 加载模型。

- 权重 revision：`370f27d7550e0def9b39c1f16d3fbaa13aa67728`。
- 文件：`Qwen3-Embedding-0.6B-Q8_0.gguf`，639,150,592 字节。
- SHA-256：`06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439`。
- llama.cpp：`b11829` / `184f6536024668e8908ab8c86033d56708e62b40`。
- Ubuntu x64 官方包 SHA-256：
  `281df0193466d75022958c331697ba3d702bbecb62c5c82e09178facbc851f5c`。

固定版本下载后先核对校验和，再启动临时服务：

```bash
/path/to/llama-server -m /path/to/Qwen3-Embedding-0.6B-Q8_0.gguf \
  --embedding --pooling last --embd-normalize 2 \
  --ctx-size 1024 --batch-size 1024 --ubatch-size 1024 \
  --parallel 1 --threads 2 --threads-batch 2 \
  --host 127.0.0.1 --port 18081 --no-webui

# 在另一个终端运行；--pid 可选，传入上面服务的实际 PID 才能记录 Linux RSS/HWM。
.venv/bin/python eval/probe_embeddings.py \
  --base-url http://127.0.0.1:18081/v1 --model qwen3-embedding-0.6b-q8_0 \
  --input eval/fixtures/embedding_probe.jsonl \
  --info eval/profiles/qwen3_q8_ctx1024_dev.json \
  --expected-dimensions 1024 --repeat 3 \
  --output eval/report/qwen-probe.json
```

按官方建议使用 last pooling、L2 归一化和仅查询侧的检索指令。探测输入为固定的
中文、英文、中英混合短查询，以及重复文本构成的长度负载；这些不是语义质量用例。
探测通过现有 OpenAI SDK 请求完整文本，不自行截断、不自动重试；失败仍逐条留档，
存在失败时退出码为 1。API 服务可通过同一入口显式更换 base-url/model/info，
密钥从 `EMBEDDING_API_KEY` 或 `--api-key-env` 指定的环境变量读取；info 禁止填写凭据。
测其他服务/硬件时应创建相应 info，不能沿用当前开发机的硬件声明。

当前开发机是 **2 核、2,468MiB、无 AVX2 的 Xeon E5-2695 v2**，与规划的另一台
4 核 4GB 机器不同。探测期间有其他开发进程并发，观察到 CPU、内存压力和换页。
没有进行隔离性能测试，不据此推算 4 核性能、并发容量或部署余量。

| 输入 | tokenizer tokens | 成功/请求 | 观测耗时范围 |
| --- | ---: | ---: | ---: |
| 中文短查询（含指令） | 30 | 3/3 | 2.68–5.39 秒 |
| 英文短查询（含指令） | 29 | 3/3 | 2.61–2.92 秒 |
| 中英混合短查询（含指令） | 39 | 3/3 | 3.52–3.67 秒 |
| 中文 512 字符 | 338 | 3/3 | 14.78–25.97 秒 |
| 中文 1,800 字符 | 1,189 | 0/3 | HTTP 400：超过 1,024-token 上限 |
| 英文 1,800 字符 | 331 | 3/3 | 25.10–27.56 秒 |

成功请求返回 1024 维向量，范数约为 1。峰值 RSS 约 808MiB；结束时 VmSwap 约
115MiB，RSS 不能单独代表全部内存需求。三次样本不报告 P95。原始记录见
[服务探测报告](report/baseline_qwen3_q8_ctx1024_probe.json)。临时服务在探测后已停止。

1,800 字符并不等于固定 token 数。当前生产切片外还带有标题等前缀，接入小上下文
服务前必须按相同 tokenizer 计算完整输入预算；不能静默截掉正文，也不能仅把服务
上下文调大就认为延迟可接受。这个错误样本只对应本次 1,024-token 服务配置，
不表示 Qwen 模型的原生上下文仅有 1,024 tokens。

## 后续验收边界

本轮已得到公开语料上的生产词法基线与本地服务兼容性证据，尚无 Qwen 的完整检索
质量成绩。接下来优先处理模型相关的 token 预算，再接入中文固定语料，比较轻量 ONNX、
Qwen Q8 和用户选择的 API。向量 run 统一使用 TREC 格式与同一评分入口。
模型/指令/量化/tokenizer/池化等共同标识向量空间，不能将不同方案的向量混用。
目标 4 核 4GB 服务器还需独立测资源与延迟，再决定是否启用本地服务。
