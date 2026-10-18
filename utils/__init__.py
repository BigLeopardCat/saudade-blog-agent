"""`utils` 包的**再导出面**（20261017 收窄）。

## 为什么这里只留一个名字

这个 `__init__` 此前再导出三件，其中两件是**死的**，而它每一件都在 `import utils.*`
时被拉进来（包 `__init__` 先于子模块执行）：

  · `format_message`（`utils/helpers.py`，9 行「Miscellaneous helper utilities」）——
    全仓**零调用**（含非 .py 文件）。真正的消息文本塑形住在 `agent/prompt_context.py`
    （`_msg_text` / `_msg_texts`），另起一个同名概念只会让人以为有第二条通路。
    模块连同这一行一起删了。
  · `speak`（`utils/tts.py`）——`speak` 自己那句注释就写着「全仓没有任何调用方」。
    更贵的是它 `import edge_tts`，而 `edge-tts==7.2.8` 是 `pyproject.toml` 里一条
    **生产依赖** ⇒ 任何人 `import utils` 都在为一段没人走到的代码付加载成本与依赖面。
    **模块本身留着**（`settings.tts_enabled` 那档开关也留着，那是主人的决定），
    只是不再从包口再导出：要用就 `from utils.tts import speak`，显式付这个代价。

⇒ 规则：**包口只再导出"有调用方"的名字**。再导出一个没人用的符号，等于给一段死代码
发一张"它还活着"的通行证——本仓最贵的失效就是「看着在跑、其实没跑」。
"""
from .logging import setup_logging

__all__ = ["setup_logging"]
