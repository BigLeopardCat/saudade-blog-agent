# 固定版本发布

生产进程使用固定提交目录，开发 checkout 不参与进程的后续导入。每个版本拥有自己的锁定虚拟环境，配置与可写检索数据放在版本目录之外。共享数据不因代码回滚而回滚。

`systemd` 的 `ExecStart` 指向 `<release-root>/current/scripts/serve.sh`。该入口先解析真实版本目录，再以绝对路径启动 `agent.main:app`，并从 `REVISION` 提供 `/version` 的构建标识。切换 current 符号链接不会改变旧进程的 Python 模块搜索路径。

```sh
scripts/deploy_release.sh COMMIT_SHA /srv/saudade-agent /etc/saudade/agent.env /var/lib/saudade-agent/data
```

使用本地 BGE profile 时，在发布命令前加 `DEPLOY_EMBEDDING_TOKENIZER=1`，安装锁定的 tokenizer 可选依赖。

服务账户需可写 release root、读取私有配置，以及重启 `saudade-agent` 的运维权限。服务单元继续设置既有嵌入模型环境、日志和停机超时；监听地址固定为 loopback，端口和 worker 数可用进程环境变量 `AGENT_PORT`、`AGENT_WORKERS` 指定。默认单进程，由应用自身限制并发请求；增加进程前按内存、数据库连接和模型额度测量容量。多进程健康检查等待默认30秒，可由 `AGENT_WORKER_HEALTHCHECK_SECONDS` 调整。这些启动参数需由服务管理器注入，应用的 `.env` 不负责配置 Uvicorn。

脚本用 flock 串行化发布，只接收 Git 中存在的提交，使用 `git archive` 排除工作区文件和凭据。依赖安装、导入检查完成后才原子切换 current；探活同时核对 `/ready` 和预期构建 SHA。失败恢复上一版本并重启，未自动删除旧版本或用户数据。首次部署没有上一版本，需由运维处理失败。

公开 PR CI 不使用生产配置或凭据。部署应在该提交的必需检查通过后执行；发布探活不代替真实模型回归。需要数据库迁移的变更须单独核对前置条件，脚本不自动执行数据迁移。

发行包由 `bash scripts/dev/package_smoke.sh` 验证。脚本构建 sdist 和 wheel，在源码目录外的
隔离环境中按 `uv.lock` 和哈希安装运行依赖及 wheel，核对包路径、应用版本和工具注册；此检查加入公开 PR CI。
它不启动模型、数据库或生产服务，不能代替全新部署的外部服务接线验收。

Trace 默认写入应用工作目录的 `data/traces`，该目录不进入 Git。生产可在私有配置中显式设置
`TRACE_DIR=/var/lib/saudade-agent/traces`，使日志位置独立于版本目录。保留治理与活跃 metrics、
告警、草稿及对账 CLI 使用同一配置默认根；显式 `--dir`（对账为 `--trace-dir`）优先。
保留治理默认只列清单，确认后才加 `--apply`。已有部署升级前应将原 trace 位置显式登记为
`TRACE_DIR`，不会自动搬迁文件。

`BLOG_ROOT` 是可选的本地博客部署根；设置后，服务健康工具读取其中的 `logs/health.log`
及日志体积。独立部署没有博客文件时留空，报表如实显示读不到宿主日志；不推测 Agent
release 目录的父级就是博客。Trace 统计始终使用 `TRACE_DIR`，与 `BLOG_ROOT` 解耦。
跨源对账需要宿主日志；未设置 `BLOG_ROOT` 时应显式传入 `--log-dir`、`--monitor-dir`，
否则 CLI 报前置条件缺失，不将缺失数据计为零异常。

可选 nightly 的父仓探针使用 `SAUDADE_PARENT_REPO`，其次使用既有 `PROJECT_DIR`，未设时按
源码 checkout 的相邻父仓布局解析。父仓探针不存在时显式记录 `SKIP / 未评估`，不计为通过。
这项源码测试坐标不作为生产宿主日志配置。

启用[管理员后台计划](scheduling.md)后，安装独立 scheduler 单元，发布时设置
`DEPLOY_SCHEDULER=1`。脚本将一并重启和回滚 Worker，未启用该选项的部署行为不变。
