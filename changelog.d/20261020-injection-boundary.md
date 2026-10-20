- **新增离线注入边界套件 `tests/test_injection_boundary.py`**（审计意见 §1.3 / §4.2 里
  **离线可判**的那一半）。矩阵形状：同一组 **25 条**注入 payload × 四个**用户可达**的通道，
  每条通道调**生产函数**本身（不是抄一份逻辑），各配一个反控证明那些绿不是恒真：
  ① 客户端字段 → `[System: key=value; …]`（`server._ctx_field` 剥 `\r\n[];=`；
  25 条里 9 条本来就带结构字符 ⇒ 不是空跑）；② 工具参数值 → 计划文本的 `;` 分隔符
  （`plan_codec.plan_encode`→`parse_plan` 往返后仍是 1 条调用；反控：把 `;` 还原成裸
  `;` ⇒ 一条裂成 3 条，复现 20260929 那场"屏幕画了、台账没记"）；③ fact 行标记 `〔系统〕 `；
  ④ 参数引用 `$tool[N].field` 的锚定。
- ③ **判的是"守卫是行首字面前缀"这件事本身，而不是"用户文本一律不被剥"**（后者是假的：
  一行字面以标记开头的用户文本确实会被剥）。所以断言写成"被剥的**只有**字面以标记开头的
  那一条、其余 24 条原样"——判据一旦松成"含标记就剥"，**行中间**那条（`用户说：〔系统〕 …`）
  会跟着被剥 ⇒ 红；松成"长得像事实就剥" ⇒ 正常文本那条也红。那条字面前缀的弱点**当前不可达**
  （`strip_fact_lines` 只在 assistant 通道上被调用，角色守卫由 `tests/test_factblock.py` 判），
  这一点在头注里写明，免得读者以为漏了。
- ⚠️ **这份套件判不了审计意见 §4.2.2 想要的那一半**："模型会不会泄露系统提示词"要真打一次
  LLM（它给的样例就是 `await client.post("/chat/stream")` + 收流 + 断言输出里没有特征串），
  与"离线套件无网络无 LLM"冲突。射程写在头注与 `docs/eval-observability.md` 里：
  **别把它读成"注入已经被测过了"**。刻意不重复的四处也一一指明（`test_confirm.py` 的伪造
  签名、`test_factblock.py` 的接线、`test_plan_channel.py` ⑤b 的"文本通道驱动不了执行"、
  `test_hardening.py` 的长度与体积闸）。
- **补上两个此前一个套件都没提过的限额**（`tests/test_hardening.py::_test_request_limits`）：
  `MAX_CONFIRM_TOKEN_CHARS`（唯一凭据，`confirm.verify` 之前先被这里挡一道）与
  `MAX_RECENT_TOOLS`（"上一轮读过审核队列吗"的判据输入）——此前
  `grep MAX_CONFIRM_TOKEN_CHARS tests/` 与 `grep MAX_RECENT_TOOLS tests/` **都是 0 命中**。
  限额写了却没判据 = 改小或改没都不会有人发现。
- 顺带记一条 ruff 的形态坑：**E402 只放行紧跟在裸 `sys.path` 语句后的 import**，
  套进 `if str(ROOT) not in sys.path:` 里照报；而新文件按纪律 1 必须零 error，所以生产模块
  改成**在函数里** import（同 `tests/test_hardening.py`）。另外 ruff 会把**注释里**出现的
  noqa 指令也当指令解析（"所以这里不能拿 `# noqa: E402` 糊过去"这句说明本身会触发
  `Invalid noqa directive` 警告），说明文字也得绕开那个形状。
