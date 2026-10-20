# -*- coding: utf-8 -*-
"""技能注册表的**配置层**：一条技能一个 TOML 文件，加载成 `Workflow`。

**为什么要有这一层**（20261021）。注册表字面量此前是 Python——六个域模块、50 条
`Workflow(...)`、2554 行。那个形状有两个代价：

① **加一条技能要在一个几百行的 `.py` 里找位置**，与主人 20261017 / 20261021 两次点名的
   「改动不断叠进同一个文件」是同一件事；
② 技能是**数据**（触发条件、参数说明、完成判定、回复契约——全是发给模型的话），写成
   Python 字面量换来的是"能塞表达式"这个**没人用**的自由，付出的是"改一句话要过一遍
   代码的形状"。

配置层把每条技能变成**一个文件**：加技能 = 加文件（不撞别人的 diff），一条技能的正文
与它自己的注记住在一起。

**为什么是 TOML**（不是 YAML / JSON）：

· `tomllib` 是 3.11+ 的**标准库**（本仓已在 3.12）⇒ **零新依赖**；
· 本仓纪律「依赖名单 = 代码里真的 import 了什么」——`yaml` 在盘上存在、但既没被声明也
  没被任何一行 import，用它等于给运行时塞一份没人复核的依赖；
· TOML **没有 `null`**。这在别处是缺陷，在这里是净收益：实测 50 条技能的 `plan` 实参
  一共 83 个值，**只有 str 与 bool、零个 None**（`{"ids": null}` 那种是展开器在运行时
  造的，不在字面量里）⇒ 换掉的是"缺席 vs 显式空"这一族歧义（本仓「缺键绝不编 0」那
  条纪律的同一件事）。

**散文怎么放**：多行基本字符串（`\"\"\"`）＋**行尾 `\\` 续行**。TOML 的续行会吃掉换行与
下一行的前导空白 ⇒ 每一行的断点与原来 Python 相邻字符串字面量的断点**一一对应**，
读起来就是那份源码本身（下面每条技能的散文都是照着原字面量逐段搬的）。
⚠️ **换行必须是显式 `\\n`**：`description` 的第一行是"触发条件"、其余是"必须怎么做"
（`Workflow.contract` 按**第一个 `\\n`** 切），靠缩进或空行表达换行会让那条切法当场切错，
而它是静默的（切错了也照样渲染出一段话）。

**顺序是一份契约**：`order` 是注册表序位，`load_dir` 按它排。改它 = planner 菜单的排列
与 `visible_workflows` 的产出顺序变了 = **提示词字节变了**，要 golden A/B 才敢做。

**这一层不碰的东西**（各留原处，理由在各自文件里）：导航别名表 `nav_map.py`（它同时被
`tools/` 读、且是**匹配器**不是话术）、共用散文片段 `capability.py`、以及 `__init__.py`
里那几张**派生表**（写靶通道 / 可见性判据）——它们是"注册表 → 视图"的推导，不是技能本体。

⚠️ **一处没验过的事，写在这里别当结论**：这些 `*.toml` 是本仓**第一次**往打包目录里放
非 `.py` 文件（`pyproject.toml` 的 `[tool.hatch.build.targets.wheel] packages` 那六个目录里
此前一个数据文件都没有）⇒ "它们会不会进 wheel"**依赖 hatchling 的整包默认行为，本机没有
验证过**（本机不 build）。不过**漏了也不是静默的**：`load_dir` 在目录里找不到任何 `*.toml`
时是**带着路径响亮抛**的（下面那条判据）⇒ agent 起不来、错误信息直接指出缺哪个目录，
而不是"注册表少了几条技能、模型少看见几个本领"。
"""

from __future__ import annotations

import pathlib
import tomllib
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent.workflow_model import Workflow

__all__ = ["PlanStep", "WorkflowConfig", "load_dir", "load_file"]


class PlanStep(BaseModel):
    """`plan` 的一步：工具名 + 参数模板（`$名字` 是上一轮工具返回的引用）。"""
    model_config = ConfigDict(extra="forbid")

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class WorkflowConfig(BaseModel):
    """一条技能 TOML 的**形状**。

    `extra="forbid"`：打错一个键名**当场响**。这不是洁癖——配置文件的失效形态天生是
    静默的（写错 `complete_wen` ⇒ 完成判定没了，模型照跑、判据照绿），所以"多一个不认识的
    键"必须比"少一个键"更响。
    """

    model_config = ConfigDict(extra="forbid")

    # 注册表序位（保序契约，见文件头注）。**必填**：缺了就没法排，而"排错了"是静默的。
    # 取值是**稀疏排序键**（现按 10 的倍数编号：位次 6 → `order = 60`），不是"第几名"：
    # 留出空档 ⇒ 插一条技能不用给后面几十个文件逐个重编号。整表转过来之后它就是全局序。
    order: int
    name: str
    description: str
    # 下面这些与 `Workflow` 的同名字段一一对应，缺省值也照抄它 —— 两处唯一允许的
    # "重复"是**同一件事的两种形状**（配置 / 运行时对象），不是两份事实源。
    capability: str = ""
    inputs: dict[str, str] = Field(default_factory=dict)
    plan: list[PlanStep] = Field(default_factory=list)
    extra_plan_tools: list[str] = Field(default_factory=list)
    required_params: list[str] = Field(default_factory=list)
    optional_params: list[str] = Field(default_factory=list)
    complete_when: str = ""
    reply_contract: str = ""
    chat: bool = False
    roles: list[str] = Field(default_factory=list)

    def to_workflow(self) -> Workflow:
        """配置 → 运行时对象（**生产路径上唯一的方向**）。"""
        return Workflow(
            name=self.name,
            description=self.description,
            inputs=dict(self.inputs),
            plan=[(s.tool, dict(s.args)) for s in self.plan],
            extra_plan_tools=tuple(self.extra_plan_tools),
            required_params=tuple(self.required_params),
            optional_params=tuple(self.optional_params),
            complete_when=self.complete_when,
            reply_contract=self.reply_contract,
            chat=self.chat,
            roles=frozenset(self.roles),
            capability=self.capability,
        )

    @classmethod
    def from_workflow(cls, workflow: Workflow, order: int) -> "WorkflowConfig":
        """逆方向（`Workflow` → 配置）。**只给判据用**。

        `tests/test_workflow_config.py` 拿它做"格式表达力"的往返：还没转成 TOML 的那
        四十多条技能，逐字段过一遍这里再回来，必须**原样相等**。那是"格式够用"的机器
        凭据——没有它，就只能靠"转过的那两条恰好没撞上死角"来相信这件事。
        """
        return cls(
            order=order,
            name=workflow.name,
            description=workflow.description,
            capability=workflow.capability,
            inputs=dict(workflow.inputs),
            plan=[PlanStep(tool=t, args=dict(a)) for t, a in workflow.plan],
            extra_plan_tools=list(workflow.extra_plan_tools),
            required_params=list(workflow.required_params),
            optional_params=list(workflow.optional_params),
            complete_when=workflow.complete_when,
            reply_contract=workflow.reply_contract,
            chat=workflow.chat,
            roles=sorted(workflow.roles),
        )


def load_file(path: pathlib.Path) -> WorkflowConfig:
    """读一个技能文件。**三种坏法都当场抛、且都带上文件名**。

    `tomllib` 的报错只说行列、不说路径；`pydantic` 的报错也不说。这两样在"50 个长得
    差不多的文件"里等于没有信息 —— 所以包一层，把路径拼进消息。
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path}：TOML 解析失败 —— {exc}") from exc
    try:
        cfg = WorkflowConfig.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"{path}：技能配置不合形状 —— {exc}") from exc
    if cfg.name != path.stem:
        raise ValueError(
            f"{path}：`name = {cfg.name!r}` 与文件名对不上。"
            "一条技能一个文件 ⇒ 名字必须等于文件名，否则「这条技能住哪」又得靠搜。")
    return cfg


def load_dir(directory: pathlib.Path) -> list[Workflow]:
    """一个目录 → 保序的技能列表。目录里**只有 `*.toml` 是技能**（`.py` 是这一层的代码）。

    两条硬判据都在这里，因为它们的失效都是静默的：
      · 目录空 ⇒ 抛（"注册表为空"从来不成立，那是路径写错了）；
      · `order` 撞 ⇒ 抛（两个技能抢一个位次，排序结果取决于 glob 的字典序）。

    ⚠️ **刻意不写"`name` 撞"那一条**：`load_file` 已经要求 `name == 文件名`，而一个目录里
    两个文件不会同名 ⇒ 重名**按构造不可能**。写了它就是一条永远走不到的分支——判据的
    价值在于"坏了会响"，够不到的分支只会让下一个读的人以为这里有一道防线。
    """
    paths = sorted(directory.glob("*.toml"))
    if not paths:
        raise ValueError(f"{directory}：一个技能配置文件都没有 —— 注册表为空是从来不成立的")
    cfgs = [load_file(p) for p in paths]
    seen: dict[int, str] = {}
    for path, cfg in zip(paths, cfgs):
        if cfg.order in seen:
            raise ValueError(f"{path}：order={cfg.order} 与 {seen[cfg.order]} 撞了（必须唯一）")
        seen[cfg.order] = path.name
    return [c.to_workflow() for c in sorted(cfgs, key=lambda c: c.order)]
