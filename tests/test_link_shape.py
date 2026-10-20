"""正文地址的形状（20261018）：一律 `[文字](URL)`，且**必须**留住"文字就是地址本身"那一支。

**为什么单起一套。** 现场（trace `20261017T110235`，主人说「猫咪给我个B站链接，我去看凡人
修仙传」）：narrator 写了

    GitHub **https://github.com/BigLeopardCat**、QQ **1052636117**、邮箱 **sora.saudade@qq.com**

前端渲染成 `<a href="https://github.com/BigLeopardCat**、QQ">…</a>` —— **href 是垃圾**，
点过去 404。同一句里的 QQ 与邮箱是对的，只有 URL 坏。

**这不是渲染层的 bug。** GFM 的 autolink literal 规则就是"一直吃到空白或 `<` 为止，再从末尾
剥掉 ASCII 标点"。候选串 `…BigLeopardCat**、QQ` 末尾是 `Q`，没得剥 ⇒ 整段进 href。同一段文本
贴到 GitHub 上渲染结果一样。20261018 逐字符实测（真实前端管线，esbuild 打包
父仓 `frontend/src/utils/chatMarkdown.ts`）得到的确切边界是：

    裸 URL 后面**跟任何非空白字符**都会被吞 —— 不止中文标点，`,` `.` `:` `*` `?` 一样吞；
    **只有行尾 / 空格才安全**。

故**"别把裸 URL 加粗"不是正解**（不加粗一样坏，只差吞进去的尾巴少两个 `*`）——正解是让 URL
进**定界符**（`](…)` / 反引号 / 尖括号）。语料实测 4/705 轮命中（0.57%），其中两处是文章链接
（`**https://saudade.site/article/54?cid=7**，也就是…` 整句被吞成链接文字）。

**本套件锁什么、不锁什么。** 锁的是**接线**与**那一支的存在**：规矩还在人设里、真的进了
narrator 的 system prompt、并且**"要让访客看见或复制地址本身时文字就写地址本身"这一支没被
删掉**。最后一条是重点——把规矩改写成"不要输出裸地址、改成描述性文字链接"看起来更严，实际会
把「给我个链接」这个场景**改坏**（地址变成标题文字，复制不到）。措辞族在这儿是**松的**：只锁
语义锚点，不锁整句话。

**不锁**"模型会不会照做"——那是行为侧的事，归 golden（要真模型、不在 CI），本套件零网络零 LLM。
它红了只有一个含义：**有人动了那段契约文本，两边的读数都得重评**。

用法：.venv/bin/python tests/test_link_shape.py
"""


import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent  # 仓根


sys.path.insert(0, str(ROOT))


from agent import graph as g  # noqa: E402
from agent.prompts import BLOG_ASSISTANT_PROMPT  # noqa: E402

FAILS: list[str] = []
check = collector(FAILS)


def _has_rule(text: str) -> bool:
    """一条规矩的**完整形**：正形 + 取地址支 + 不给链接支，三件齐了才算在。

    刻意不判整句——句子会被重写，语义锚点不会。但三支**缺一即假**：只留正形会漏掉
    "文字就是地址本身"（「把地址发我」那个场景就没了出口），只留取地址支则指路时会退化成
    一大串地址糊在正文里。
    """
    return ("一律写成 [文字](URL)" in text
            and "文字就写地址本身" in text
            and "](https://<真实地址>)" in text
            and "放进反引号里" in text)


def test_link_shape():
    print("\n① 人设里有这条规矩（三支齐：正形 / 取地址支 / 不给链接支）")
    _core = BLOG_ASSISTANT_PROMPT
    check("地址一律写成 [文字](URL)", "一律写成 [文字](URL)" in _core)
    check("  取地址支在：文字就写地址本身（「给我个链接」的出口）",
          "文字就写地址本身" in _core and "](https://<真实地址>)" in _core)
    check("  不给链接支在：反引号兜住裸地址",
          "放进反引号里" in _core)
    check("  并点名了「既能点」与「地址串照旧可选可复制」两个好处（少了它这支读起来像降级）",
          "可选可复制" in _core and "既能点" in _core)
    print("\n①b 正控（判据不是恒真的）")
    for _cut in ("一律写成 [文字](URL)", "文字就写地址本身",
                 "](https://<真实地址>)", "放进反引号里"):
        check(f"  删掉「{_cut[:16]}」后判据变假",
              not _has_rule(_core.replace(_cut, "")))
    print("\n② 接线：规矩真的进了 narrator 的 system prompt（在常量里躺着不算）")
    _sys = g._EXECUTOR_PROMPT.format(
        persona=BLOG_ASSISTANT_PROMPT, audience="【对话者：访客】",
        plan="（本轮没有计划）", tool_frames="（本轮没有工具返回）",
        exec_receipts="（无）", page_ctx="（无）", sticker_guide="（无）",
        fact_block="（本轮没有动作族执行）")
    check("narrator 的 system prompt 里含这条规矩",
          "一律写成 [文字](URL)" in _sys and "文字就写地址本身" in _sys)
    check("  且是**格式化之后**仍在（`persona=` 槽没接错常量）",
          BLOG_ASSISTANT_PROMPT.strip()[:30] in _sys)
    check("  且 `.format` 的槽都填上了（漏槽会留一串花括号文本给模型看）",
          "{" not in _sys and "}" not in _sys)
    print("\n③ 旧规矩没被顶掉（是推广，不是替换）")
    check("引用站内文章附链接那条仍在（含绝不编造 id 或 URL）",
          "推荐/引用站内文章" in _core and "绝不编造 id 或 URL" in _core)
    check("  页面跳转仍由系统执行那条仍在（正文链接不许冒充已跳转）",
          "不得用链接冒充已跳转" in _core)
    print("\n④ 契约里不许出现**可抄的坏形状**（同「不许出现可抄的否认句」那条纪律）")
    check("人设里没有加粗裸 URL 的示范（`**https://`）", "**https://" not in _core)
    check("  也没有裸 URL 紧跟非空白的示范（`](…)`/反引号之外不留裸地址写法）",
          "看 https://" not in _core and "地址：https://" not in _core)
    print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_link_shape()
