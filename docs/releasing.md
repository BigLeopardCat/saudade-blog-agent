# Agent 发行维护

适用于发布 Agent 包和源码附件。部署已有版本见[生产部署](deployment.md)。

先按[发布验收](release-acceptance.md#按改动选择验收范围)确定本次验证。若改动涉及模型主链，按[冻结回归](eval-observability.md#冻结回归与采集证据)记录 POC 或全量检查。离线补丁保留该候选的离线证据。

## 正式发行

发行前核对 `pyproject.toml` 的包版本、候选提交和最终附件 manifest 的 `version`、`source_revision`。发行说明记录实际验证过的宿主、组件、Compose Agent 与数据库组合。配套锁的归属、切换顺序和回退条件见[博客兼容与升级](https://github.com/BigLeopardCat/Saudade-Blog/blob/cn_sora_blog/docs/compatibility-and-upgrades.md)。

切换 Compose Agent 时验证安装、恢复及受影响的跨仓任务。发行证据按[发布验收](release-acceptance.md)登记。

使用干净检出和对应提交的 CI 构建附件。`Agent container` 工作流接受已有草稿的
`release_tag`，草稿目标必须等于构建提交且 tag 与包版本对应。候选变更后核对草稿目标、
manifest、附件校验和及验收报告，四者都来自新候选才可发布。发行说明列出适用数据库迁移、
已知限制和升级回滚入口。生产凭据、数据库备份和私有 trace 留在受控环境。
博客安装包由[宿主发布流程](https://github.com/BigLeopardCat/Saudade-Blog/blob/cn_sora_blog/deploy/releasing.md)生成。

发布后回读标签、manifest 和校验和，核对发行说明上的适配组合、生效范围、已知限制和未验证项。上线的实际版本由运维核对，发行证据按版本留在[发布证据索引](../eval/release-status.md)。
