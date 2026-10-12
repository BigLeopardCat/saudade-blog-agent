"""检索层（正文分节 / BM25 / 向量 / 词图）。

**刻意什么都不 re-export**（与 `agent/ config/ models/ tools/ utils/` 那五个不同）：
那五个是"一个包 = 一份对外 API"，这里是一个**并列的模块集合**（`sections` 给
`tools/base.py` 用、`search` 给 `server.py` 与评测用、`vector_index` 只被 `search`
和评测用），彼此没有共同的入口。写 `from .search import get_index` 会把
`tools.base → rag → rag.search` 接成一条**不必要**的导入链——它是可选的向量
依赖的宿主，谁 import 谁付代价，别让一个 `import rag` 就把整层拖起来。

**这份文件的唯一职责是让 `rag` 成为一个普通包**（20261013 补）：
  ① 在此之前它是 PEP 420 **隐式命名空间包**——所有 Python 侧的 import 都写
     `rag.xxx` 所以一直没坏，但 `mypy` 会直接拒绝整个目录（`Source file found
     twice under different module names: "search" and "rag.search"`）⇒
     `[tool.mypy]` 那份配置配上以来**第一次真跑**就当场卡在这里；
  ② `pyproject.toml` 的 `[tool.hatch.build.targets.wheel].packages` 里列着 `rag`
     ——列一个没有 `__init__.py` 的目录，构建期行为要看后端怎么解释命名空间，
     不留这种"从没验证过"的余地。
判据：`tests/test_packaging.py` ① 断言 `packages` 每一项都带 `__init__.py`。
"""
