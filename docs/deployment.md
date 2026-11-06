# 生产部署

本文支持 Linux + systemd 的博客集成部署，参考环境为 Ubuntu 24.04、Python 3.12 和 PostgreSQL 16。需要 Git、uv、curl、OpenSSL 和 util-linux 的 `flock`。浏览器通过 [Saudade Blog](https://github.com/BigLeopardCat/Saudade-Blog) 登录和转发聊天；业务 API 也由宿主提供。先按博客的[部署说明](https://github.com/BigLeopardCat/Saudade-Blog/blob/cn_sora_blog/docs/deployment-and-ops.md)部署 PostgreSQL 版宿主，不使用旧 MySQL 安装器或未支持的容器草案。

首次安装与升级是不同操作。下文初始化命令只用于新安装；已有站点不要重建数据库、覆盖配置或重新初始化 `current`。

## 版本与配置边界

生产运行 `/srv/saudade-agent/releases/<完整提交 SHA>`，每版有独立锁定 venv。启动脚本解析真实目录后执行 `agent.main:app`，不会在运行中导入开发 checkout 的变更。Agent 与博客分别发布；博客的 widget pin 只控制前端资源。

| 内容 | 来源及权限 |
| --- | --- |
| 开发配置 | 本仓 `.env.example` 复制为开发者自己的 `.env` |
| Agent HTTP、scheduler、维护任务 | `/etc/saudade/agent-runtime.env`，root:saudade-agent，0640 |
| 博客业务配置 | `/etc/saudade/blog-runtime.env`，不交给 Agent |
| 可选 embedding 进程 | `/etc/saudade/embedding-<profile>.env` 与独立认证文件 |
| 发布代码 | 部署账号拥有，Agent 组只读；运行账号没有 sudo |
| 私有状态与缓存 | `/var/lib/saudade-agent`、`/var/cache/saudade-agent` |

生产由 systemd `EnvironmentFile` 注入配置，并设置 `SAUDADE_IGNORE_ENV_FILE=1`。不把生产文件链接到开发 checkout，不用 `source` 执行配置，不输出密钥。完整可调参数以 [Settings](../config/settings.py) 为准；[生产模板](../deploy/agent-runtime.env.example) 只列首次安装的主要设置。

## 首次安装

### 1. 固定源码与运行账号

以非 root 的部署账号检出已通过必需 CI 的 Agent 完整提交，并保留实际部署的博客源码版本。设置下列变量，后续命令在同一 shell 执行；占位路径必须替换：

```sh
AGENT_SOURCE=/absolute/path/to/saudade-blog-agent
BLOG_SOURCE=/absolute/path/to/Saudade-Blog
APP_DIR=/absolute/path/to/blog-deployment
DEPLOY_USER="$(id -un)"
AGENT_REV="$(git -C "$AGENT_SOURCE" rev-parse HEAD)"
```

`APP_DIR` 是博客发布目录，包含 `target/release/saudade_blog_bin` 和 `logs`，不是任意源码父目录。博客服务生成器是集成部署的唯一模板入口。先阅读其[账号与沙箱说明](https://github.com/BigLeopardCat/Saudade-Blog/blob/cn_sora_blog/deploy/service-isolation.md)，由管理员创建账号和状态目录：

```sh
sudo systemd-sysusers "$BLOG_SOURCE/deploy/systemd/saudade.sysusers"
sudo systemd-tmpfiles --create "$BLOG_SOURCE/deploy/systemd/saudade.tmpfiles"
sudo install -d -o "$DEPLOY_USER" -g saudade-agent -m 2750 /srv/saudade-agent
sudo install -d -o root -g root -m 0755 /etc/saudade
sudo install -d -m 0755 "$APP_DIR/logs/agent"
sudo -u "$DEPLOY_USER" -g saudade-agent uv --version
```

部署账号需要读取 Agent 配置、写 release root，并能非交互重启 Agent。管理员通过 `visudo` 只授权 `systemctl restart saudade-agent`；启用 scheduler 后再加入该服务的 `restart`、`stop`、`is-active --quiet` 权限。命令中的 `sudo -u ... -g ...` 由安装管理员执行，不给运行账号提权能力。uv、Python 和源码路径必须在部署账号的执行环境可访问。

### 2. 建立独立 Agent 数据库

在 PostgreSQL 主机上创建非超级用户和专用数据库。以下命令仅用于尚不存在的新库，密码由交互提示输入；远程数据库另配受限网络与 TLS：

```sh
sudo -u postgres createuser --pwprompt saudade_agent
sudo -u postgres createdb --owner=saudade_agent saudade_agent
```

该角色需要管理自己库内的框架表和索引，不需要访问博客库。LangGraph 首次启动建立 checkpoint schema，DBOS 启用后管理自己的 schema，不向此库导入博客 SQL。`CHECKPOINT_FORBID_DB` 填实际博客库名，不能代替数据库授权隔离。DSN 密码中的保留字符必须 URL 编码。

### 3. 填写生产配置

仅在首次安装且目标文件不存在时复制模板；已有站点使用 `sudoedit` 增量修改。下列命令须在检查成功后执行：

```sh
if sudo test -e /etc/saudade/agent-runtime.env; then
  echo '配置已存在，请使用 sudoedit 修改，不要覆盖。' >&2
else
  sudo install -o root -g saudade-agent -m 0640 \
    "$AGENT_SOURCE/deploy/agent-runtime.env.example" /etc/saudade/agent-runtime.env
fi
sudoedit /etc/saudade/agent-runtime.env
```

必须填写当前供应商凭据、数据库密码、自己的公开来源及两个签名密钥。用 `openssl rand -hex 32` 分别生成独立随机密钥：

- `AGENT_ASSERTION_SECRET`：Rust 与 Agent 一致，用于宿主身份断言。
- `AGENT_CONFIRM_SECRET`：仅 Agent 持有，用于人工确认。
- Rust 的登录 `JWT_SECRET`、工具委托 `BLOG_DELEGATION_SECRET` 不放进 Agent。

OpenCode 示例使用 Go 端点；其他供应商配置对应前缀和 `LLM_PROVIDER`，key 必须匹配实际套餐端点。`BLOG_API_BASE` 必须是自己的公开来源，导航允许地址也由此确定；`AGENT_ADMIN_BASE` 是 Agent 可访问的 Rust 地址。宿主设置 `AGENT_URL=http://127.0.0.1:8010/chat` 和相同断言密钥。

### 4. 准备版本、安装服务并发布

先准备依赖和只读目录，不启动服务：

```sh
sudo -u "$DEPLOY_USER" -g saudade-agent env \
  DEPLOY_PREPARE_ONLY=1 DEPLOY_SERVICE_GROUP=saudade-agent \
  DEPLOY_RUNTIME_STATE=/var/lib/saudade-agent \
  bash "$AGENT_SOURCE/scripts/deploy_release.sh" "$AGENT_REV" /srv/saudade-agent \
  /etc/saudade/agent-runtime.env /var/lib/saudade-agent/data
```

生成单元到新的临时目录。以下 `current` 初始化仅用于没有现有链接的新安装，使 systemd 能检查真实执行入口；不会启动进程：

```sh
if [ ! -e /srv/saudade-agent/current ] && [ ! -L /srv/saudade-agent/current ]; then
  ln -s "/srv/saudade-agent/releases/$AGENT_REV" /srv/saudade-agent/current
fi
UNIT_STAGE="$(mktemp -d)"
python3 "$BLOG_SOURCE/scripts/deploy/render_service_isolation.py" \
  --app-dir "$APP_DIR" --release-root /srv/saudade-agent --output "$UNIT_STAGE"
systemd-analyze verify "$UNIT_STAGE/saudade-agent.service" \
  "$UNIT_STAGE/saudade-agent-maintenance.service" "$UNIT_STAGE/saudade-agent-maintenance.timer"
```

检查输出与 `systemd-analyze verify` 成功后，安装以下 Agent 单元，不替换已有 Rust 服务：

```sh
sudo install -m 0644 "$UNIT_STAGE/saudade-agent.service" \
  "$UNIT_STAGE/saudade-agent-maintenance.service" \
  "$UNIT_STAGE/saudade-agent-maintenance.timer" /etc/systemd/system/
for UNIT_NAME in saudade-agent saudade-agent-maintenance; do
  sudo install -d "/etc/systemd/system/$UNIT_NAME.service.d"
  sudo install -m 0644 "$UNIT_STAGE/$UNIT_NAME.service.d/90-sandbox.conf" \
    "/etc/systemd/system/$UNIT_NAME.service.d/"
done
sudo systemctl daemon-reload
systemctl cat saudade-agent
```

确认只有一个应用 `EnvironmentFile`，入口指向 release。然后执行发布与探活：

```sh
sudo -u "$DEPLOY_USER" -g saudade-agent env DEPLOY_SERVICE_GROUP=saudade-agent \
  DEPLOY_RUNTIME_STATE=/var/lib/saudade-agent \
  bash "$AGENT_SOURCE/scripts/deploy_release.sh" "$AGENT_REV" /srv/saudade-agent \
  /etc/saudade/agent-runtime.env /var/lib/saudade-agent/data
sudo systemctl enable saudade-agent
curl --fail http://127.0.0.1:8010/ready
curl --fail http://127.0.0.1:8010/version
```

`/version.sha` 必须等于 `AGENT_REV`。内部 8010、数据库和 embedding 端口不暴露公网；公网经过博客鉴权代理，不转发任意内部 Agent 路由。nginx 的 SSE 使用博客模板，关闭缓冲并保留足够读取超时。

### 5. 验证功能和可选能力

先用真实登录的测试账号检查闲聊、文章检索、未确认写入零副作用及暂停后继续。确认/回放使用可清理的夹具。`/ready` 证明应用依赖就绪，不证明模型供应商可用、缓存命中或任务通过。

| 可选能力 | 启用前的准备 |
| --- | --- |
| 后台计划 | 按[调度部署](scheduling.md#部署)安装独立 DBOS Worker，确认宿主迁移及授权接口，发布加 `DEPLOY_SCHEDULER=1` |
| 本地 embedding / 混合检索 | 按[embedding 输入](embedding-inputs.md)及固定资产清单准备模型、tokenizer 和服务；服务验证后再启用 `RAG_HYBRID_ENABLED` |
| trace 保留 | 先以运行账号、相同配置执行 `python -m eval.trace_retention` 查看候选，再启用 `saudade-agent-maintenance.timer` |
| 设备、语音 | 默认关闭，配置对应后端、凭据和网络后再启用 |

配置 `EMBEDDING_PROFILE` 后，发布脚本自动安装锁定 tokenizer 依赖并在激活前校验。混合检索需实际查询后检查 `/health` 中 `rag_state=active`、`vector_missing=0`；启动成功不代表索引已完成。

## 升级与回滚

Agent API 始终验证宿主断言。升级时删除退役的 `AGENT_REQUIRE_ASSERTION`、`AGENT_ARM` 配置项，
并保留有效的 `AGENT_ASSERTION_SECRET`；包含旧开关的配置会明确拒绝启动。
向量功能须单独配置 `EMBEDDING_MODEL` 与 `EMBEDDING_API_KEY`，不会借用聊天凭据。

固定新的完整提交，核对 CI、宿主兼容与数据库变更。备份配置、unit/drop-in、`current` 指向、Agent 数据库和私有授权数据。代码回滚不会回滚共享数据。

重复发布命令升级，不重复首次安装的建库、配置复制和链接初始化。脚本通过 flock 拒绝并行发布，用 `git archive` 排除工作区文件和凭据；依赖与预检通过后切换版本，重启并核对就绪和 SHA。失败恢复上一版本，启用调度时同步重启/回滚 Worker。首次安装没有可用旧版本，不能依赖自动回滚解决接线错误。

主动回滚时将 `AGENT_REV` 换成受当前数据库和配置支持的旧提交，再执行相同发布命令。不修改已发布目录或对正在服务的 venv 执行 `uv sync`，不删除未完成计划和执行账。发布脚本不执行业务数据库迁移；不兼容变更须另备迁移与回退步骤。

已有安装调整账号/配置来源时，按博客[账号隔离](https://github.com/BigLeopardCat/Saudade-Blog/blob/cn_sora_blog/deploy/service-isolation.md#已有安装更新)审阅旧覆盖文件，保留站点资源限制及 BGE 依赖，不叠加排序更靠后的环境覆盖。更换模型 key 只改 runtime 文件，重启 HTTP 与已启用的 scheduler，再做小批量验证。

## 日志、容量与故障处理

```sh
systemctl status saudade-agent
journalctl -u saudade-agent -n 100 --no-pager
systemctl show saudade-agent -p User -p Group -p EnvironmentFiles -p ExecStart
curl --fail http://127.0.0.1:8010/health
```

集成单元将 HTTP 标准输出追加到 `$APP_DIR/logs/agent/agent.log`；启动失败看 journal，scheduler 和维护任务默认使用 journal。trace 使用 `TRACE_DIR`，不上传公开 CI。`BLOG_ROOT` 只用于授权宿主日志诊断，独立服务没有宿主日志时留空；对账显式传 `--log-dir`、`--monitor-dir`，不把缺失数据计为零异常。

监控活动任务、排队/拒绝、数据库连接、磁盘和供应商错误/真实 usage。闲置网页不占运行许可。默认一个进程、最多 8 个活动聊天，需要按目标机器与供应商实测，不能据此宣称支持某个并发人数。`AGENT_PORT`、`AGENT_WORKERS`、`AGENT_WORKER_HEALTHCHECK_SECONDS` 由 systemd 注入；增加 worker 会放大并发和连接预算。

| 现象 | 优先检查 |
| --- | --- |
| `/ready` 报凭据/签名密钥缺失 | 当前供应商、两个签名密钥、实际 EnvironmentFile，不打印配置内容 |
| 启动或数据库失败 | journal、DSN、网络及专用库权限；不回退内存冒充恢复 |
| 登录正常但工具 401/403 | 当前身份/权限、两端断言密钥、工具委托有效期 |
| 429/5xx 或断连 | trace 中的状态、`Retry-After` 和安全请求 ID，不只凭回复文字归因 |
| 失败后任务已暂停 | 从 checkpoint 显式继续；未知写入先对账，不盲目重做 |
| 检索退回词法 | embedding 认证、profile、向量缺失与刷新状态 |

定期备份数据库和私有授权目录，在隔离环境演练还原。向量缓存可重建，确认/执行账和会话不能按缓存删除。保留可回退版本，审查日志轮转、trace 保留和磁盘余量。

正式版还需满足[发布验收](release-acceptance.md)：必需 CI、安装/升级/回滚、真实浏览器、小批量，以及供应商稳定后的冻结全量回归。供应商错误与功能失败分别统计，重跑使用新报告目录，不覆盖失败记录。CI 的发行包隔离检查不能代替全新环境的外部服务接线验收。
