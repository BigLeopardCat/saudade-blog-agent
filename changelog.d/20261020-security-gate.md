- **新增第六道棘轮：安全扫描（bandit）只许变小**（`eval/security_gate.py` +
  `eval/security_baseline.json`，首次读到 **142 条 / 80 个键**，原始 462 条）。
  **先说清楚这 142 条是什么**：`B105` 53（字典**键**叫 `pass` 被当成硬编码口令，纯误报，
  但这一族要留着——同一个插件也报真口令）、`B608` 29（f-string 拼 SQL，拼进去的是**模块级
  常量**表名列名）、`B108` 19（写 /tmp）、`B112`+`B110` 20（try/except/continue|pass，
  **与 `except_gate.py` 量的是同一族**）、`B310` 11（eval 探针 urlopen 打本站）、
  `B324` 8（sha1/md5 用于去重键与内容指纹，正解是补 `usedforsecurity=False`）、
  `B106` 2 ⇒ **没有一条是真的可利用漏洞**。这个闸买的是"以后也不许有"。
- **跳过集四条**（`B101` assert，257 条 = 原始 56%；`B404`/`B603`/`B607` subprocess 的
  记账三项）：本仓的既定形态就是 shell 出去调 git/systemctl/psql。⚠️ **不安全的变体没有
  跳**——`B602`（`shell=True`）与 `B609`（通配符注入）留在扫描面里。跳过 subprocess 三项
  还有个具体后果：**本闸自己的文件才是零条**（`subprocess.run` 是闸的必需品），否则
  "新文件必须零条"这条纪律一出生就是死的。
- **键是 `文件::规则` 而不是光文件**：同一个文件里把一条 B105 误报换成一条真 B608，按文件
  计数是"没变"——那正是棘轮最不该看不见的一种变化。判据五条：总数变大 / 某键变大 /
  新文件带 finding / `nosec` 变大 / 幽灵键退 3。
- **⚠️ 第三个量是「bandit 跑在哪个解释器上」（实测，不是推测）**：同一份源码、同一个
  bandit 版本，`uvx bandit`（自带 3.10）读 **205** 条、`uvx --python 3.12 bandit` 读 **211**
  条——同 `eval/type_gate.py` 的 `--python-executable` 那个洞。故 `bandit --version` 里那行
  `python version = X.Y.Z` 进账、对不上退 3。本机跑法必须带 `--python 3.12`。
- **两个"悄悄关闸"的口子也堵了**：① **配置自动发现**——bandit 不给 `-c` 时会自动读
  `.bandit` / `bandit.yaml` / `setup.cfg` / `tox.ini` / **`pyproject.toml` 的 `[tool.bandit]`**，
  谁加一句 `skips = ["B608"]` 本闸就静默少报还报绿（同 `except_gate.py` 那个 ruff `ignore`
  表的洞）⇒ 这些配置源在场直接退 3；② **`# nosec` 行内豁免**只进 `metrics._totals.nosec`、
  不进 `results` ⇒ `nosec` 进账且只许变小（现在 0 ⇒ 等于不许用）。
  实测反控：给 pyproject 加 `[tool.bandit]` ⇒ 退 3；加一行 `# nosec` ⇒ 报红。
- 工程坑记一笔：bandit 的 logging 与进度条打在 **stdout** 上，与 `-f json` 的输出混在一起
  ⇒ 必须用 `-o` 落文件再读（按"第一行是不是 `{`"去认会当场认错，`--update` 第一次跑就撞上）。
- 依赖：`bandit==1.9.4` 进 `[dependency-groups] dev` + `uv lock`（同 ruff/mypy 的形制；
  CI 的 `uv sync --frozen` 要锁里有它才装得上）。新套件 `tests/test_security_gate.py`（5 条）
  盯接线与账；CI 加一步 `uv run --frozen python eval/security_gate.py`；
  `docs/lint-baseline.md` 补「同族的第六道」一节。
