# -*- coding: utf-8 -*-
"""`utils` 包的**门面**——20261019 起是空的（不再再导出任何名字）。

## 为什么这里一个名字都不再导出

规则本是「包口只再导出**有调用方**的名字」（20261017 收窄）。20261019 补上后半句：
**再导出还不许把包变重**——包 `__init__` 先于子模块执行，谁 `import utils.<任何子模块>`
都要替它那一行付代价。

  · `format_message`（`utils/helpers.py`，9 行「Miscellaneous helper utilities」）——
    全仓**零调用**（含非 .py 文件）。真正的消息文本塑形住在 `agent/prompt_context.py`
    （`_msg_text` / `_msg_texts`），另起一个同名概念只会让人以为有第二条通路。
    模块连同这一行一起删了。
  · `speak`（`utils/tts.py`）——`speak` 自己那句注释就写着「全仓没有任何调用方」。
    更贵的是它 `import edge_tts`，而 `edge-tts==7.2.8` 是 `pyproject.toml` 里一条
    **生产依赖** ⇒ 任何人 `import utils` 都在为一段没人走到的代码付加载成本与依赖面。
    **模块本身留着**（`settings.tts_enabled` 那档开关也留着，那是主人的决定），
    只是不再从包口再导出：要用就 `from utils.tts import speak`，显式付这个代价。
  · `setup_logging`（`utils/logging.py`）——它有调用方（`server.py`），但那一行会连带
    `config.settings`（pydantic-settings 读 `.env`）。而 `utils/` 从 20261019 起住着
    **零依赖的 trace 枚举/读取**（`utils/trace_files.py` / `utils/trace_io.py`），它们的
    立身之本正是"离线读取端只吃路径参数、不拉应用配置"。⇒ 那一处调用方改写为
    `from utils.logging import setup_logging`，包口清空。

⇒ 规则：**包口只再导出「有调用方、且不把包变重」的名字**。再导出一个没人用的符号，等于
给一段死代码发一张"它还活着"的通行证——本仓最贵的失效就是「看着在跑、其实没跑」。
"""
