# 固定版本发布

生产进程使用固定提交目录，开发 checkout 不参与进程的后续导入。每个版本拥有自己的锁定虚拟环境，配置与可写检索数据放在版本目录之外。共享数据不因代码回滚而回滚。

`systemd` 的 `ExecStart` 指向 `<release-root>/current/scripts/serve.sh`。该入口先解析真实版本目录，再以绝对路径启动 `agent.main:app`，并从 `REVISION` 提供 `/version` 的构建标识。切换 current 符号链接不会改变旧进程的 Python 模块搜索路径。

```sh
scripts/deploy_release.sh COMMIT_SHA /srv/saudade-agent /etc/saudade/agent.env /var/lib/saudade-agent/data
```

使用本地 BGE profile 时，在发布命令前加 `DEPLOY_EMBEDDING_TOKENIZER=1`，安装锁定的 tokenizer 可选依赖。

服务账户需可写 release root、读取私有配置，以及重启 `saudade-agent` 的运维权限。服务单元继续设置既有嵌入模型环境、日志和停机超时；监听地址固定为 loopback，端口和 worker 数可用 `AGENT_PORT`、`AGENT_WORKERS` 指定。

脚本用 flock 串行化发布，只接收 Git 中存在的提交，使用 `git archive` 排除工作区文件和凭据。依赖安装、导入检查完成后才原子切换 current；探活同时核对 `/ready` 和预期构建 SHA。失败恢复上一版本并重启，未自动删除旧版本或用户数据。首次部署没有上一版本，需由运维处理失败。

公开 PR CI 不使用生产配置或凭据。部署应在该提交的必需检查通过后执行；发布探活不代替真实模型回归。需要数据库迁移的变更须单独核对前置条件，脚本不自动执行数据迁移。
