#!/usr/bin/env bash
# Existing opt-in nightly job: deterministic tests, legacy golden, hybrid outcomes
# and diagnostic reports. Does not install a schedule; see enable_nightly.sh.
# Results append to ~/agent_regression.log. Any required check failure writes
# ~/agent_regression.failed; diagnostic/judge reports do not relax required checks.
set -u
# 20261017：路径从脚本自己的位置推，不再写死维护者的家目录 —— 写死的 `cd` 在别人机器上
# 会当场失败，而失败的样子是一个每晚静默退出的 cron（`>/dev/null 2>&1` 把话全吃了）。
# 本机推导结果与原来那一行逐字相同（脚本在 <仓根>/scripts/ 下）。
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=.venv/bin/python
LOG="$HOME/agent_regression.log"
MARK="$HOME/agent_regression.failed"
TS=$(date '+%Y-%m-%d %H:%M:%S')

echo "=== nightly regression $TS ===" >> "$LOG"

# The opt-in production job explicitly chooses the deployed host source. Public
# PR tests instead use the pinned checkout supplied by GitHub Actions.
export SAUDADE_PARENT_REPO="${SAUDADE_PARENT_REPO:-$(dirname "$PWD")}"
export SAUDADE_REQUIRE_PARENT=1

fail=0
# 20260928 起：这一节从"单跑 test_workflows.py"换成**按目录枚举整个 tests/**。
# 理由是审计实测出来的那个数：CI 的 eval.yml 手工维护 26 个 step，而磁盘上有 50+ 个套件
# ⇒ **一半以上的判据从不在任何自动化里运行**（其中就有前缀缓存稳定性的唯一哨兵
# test_prompt_prefix 与 test_slim_workflows）。夜间此前也只跑一个套件。名单制（三处各抄一份）
# 正是这个漏的来源 ⇒ 夜间也改成按目录收集，加套件不用改这里。
# 20260928 那次落点是自研 runner `tests/run_all.py`；**20261020 这 180 个裸脚本套件整体转成
# 原生 pytest**（过程与逐批读数见 changelog.d/20261020-pytest-migration-*.md），入口换成
# `pytest` 本身、run_all.py 删除。**夜间与 CI 走同一个入口**（都是 `pytest`，只是 CI 多传
# `-n 4`），所以夜间的绿与 CI 的绿是同一套环境下的绿。
# 出厂档钉子随入口一起搬进 `tests/conftest.py` 的 `_PINNED`（`AGENT_EVIDENCE=0` /
# 显式测试配置及 `SAUDADE_IGNORE_ENV_FILE=1`）：本机 `.env`
# 是产线那份，不钉的话离线判据会跟着运维取值变。（`PLANNER_ENGINE` 那一项 20261004 删了
# ——接口层只剩 native 一条，没有档可钉；`AGENT_TASK_STATE` / `AGENT_TASK_PLAN_EXEC` 两项
# 20261015 删了——任务状态整族撤档、常开。）
# 不加 `-n`：这一趟在本机跑、没人等它，而本机内存比 runner 紧（CI 那一份才按 4 路并行）。
echo "--- pytest (全部离线套件, 按目录收集, 秒级, 无网络无 LLM) ---" >> "$LOG"
$PY -m pytest -q >> "$LOG" 2>&1 || { fail=1; echo "[$TS] pytest FAILED" >> "$LOG"; }
# 20260924 起：检索基准（recall@k / MRR，直接测线上 rag/search.py，秒级、无网）。
# README 里一直写着 nightly 跑 L1/L2 两项，实际只有 L2——这一节把它补齐。
# 非门禁：已知 FAIL 是词法表征的局限（脚本自己在报告里点名），不该让夜间任务变红。
echo "--- 检索基准 recall@k / MRR (直接测线上 rag/search.py, 秒级, 非门禁) ---" >> "$LOG"
$PY eval/recall_eval.py >> "$LOG" 2>&1 || echo "[$TS] recall_eval 运行异常（不影响门禁）" >> "$LOG"
echo "--- 断崖证书探针 (真语料上跑有/无断崖两臂, 秒级, 非门禁) ---" >> "$LOG"
# 20261011 起。起因是一次**判据自身的失效**：相对断崖（α=0.25）的"无害"证明是 20260920
# 那次实跑签发的**一次性经验证书**（写在 rag/search.py 注释里），此后没人复验。语料
# 12→13 篇那天它就作废了——新增的 note:62 是一篇与多条 query 同主题的强势文档，把 top1
# 抬高 ⇒ `floor = top1×0.25` 跟着抬高 ⇒ 排在第 2 名的 gold 被自己的断层切掉（hits 由
# 三条收成一条，recall@3 0.92）。**没有一条判据指认了凶手**，是手工比对两轮报告才定位的。
#
# 现在保底 top-3（`_CLIFF_MIN_KEEP`）把那张证书变成**结构不变量**：断崖只裁第 K 名之后的
# 尾巴 ⇒「断崖改变 recall@1/@3」结构上不可能。本探针就是它在**真语料**上的验证：
# 同一次评测里对每条 query 跑两臂（线上臂 + `apply_cliff=False`），逐条比名次。
# 合成语料那一半在 `tests/test_cliff_min_keep.py`（进 CI），两半管的是不同的事——
# 离线那半证明"保底这段代码在"，这半证明"今天的语料没把它绕过去"（病灶是**语料形态**的
# 函数，每发一篇文章都该重跑一次）。
#
# **非门禁**（同语料漂移哨兵那族）：保底那段代码被改回去时，同一夜**第一步**的
# 第一步那套离线套件已经红了（那个是门禁）——本探针不是它的备份，是它的
# **两个不同前提**：离线那半的前提是"夹具还做得出病灶"，这半的前提是"今天的语料是今天的
# 语料"。两者都可能单独失守（夹具腐烂 / 语料形态变了），所以各自报各自的。
# 退出码：0 一致 / 1 有回归（断崖动了 top-3 名次）/ 2 读不到语料（**无法确认**，不是"没有"）。
$PY eval/cliff_probe.py >> "$LOG" 2>&1 || { rc=$?
  if [ "$rc" -eq 2 ]; then
    echo "[$TS] 断崖证书探针**未评估**（语料读不到——公开 API 没起来？不是「有回归」）" >> "$LOG"
  else
    echo "[$TS] 断崖证书破了：保底 top-3 之后仍动了 top-3 名次（见上面 ❌ 那行点名的 query；非门禁）" >> "$LOG"
  fi; }
echo "--- 语料漂移哨兵 (词表型断言 vs 语料, 秒级, 非门禁) ---" >> "$LOG"
# 20260925 起：扫 golden 里每个 text_contains 词——还在语料里吗（ORPHAN）/ 是不是满语料
# （GENERIC）/ 落点是不是该用例申报的那几篇（MISBOUND）；require_doc_terms 的派生集太小
# 报 THIN。动机就是 rag_ota_http：人抄的期望词随语料漂移，而漂移的表现是「用例继续红或
# 继续绿，没人知道判据已经不成立」。
# **非门禁**：退出码 1 只表示"有 ORPHAN/MISBOUND"，当前已知 9 条 ORPHAN（rag_fingerprint_*
# 的死支、rag_arch_check 的陈旧词、rag_python_copy 的深/浅拷贝）是**本轮范围外**的存量，
# 已进报告待点名——让它置 fail=1 只会让整夜门禁天天红（哨兵一响就没人看了）。
# 报告落 eval/report/corpus_drift_<ts>.md。
$PY eval/corpus_terms.py --drift >> "$LOG" 2>&1 || echo "[$TS] corpus_terms --drift 有 ORPHAN/MISBOUND 或运行异常（非门禁，看报告）" >> "$LOG"
echo "--- 真写夹具残留哨兵 (只读公开接口, 秒级, 非门禁) ---" >> "$LOG"
# 20260925 起：真写用例（golden_write_category_delete_exec）的目标是**夹具**
# （分类 agent_fixture_category_a，见 scripts/migration/golden_write_fixture_20260925.sql）。
# 那条用例跑完即把自己删掉，所以正常态是"公开分类列表里一行夹具都没有"。它没删掉
# （用例红在中途、或有人手工建了同族名字）时夹具会**留在生产库里，而且访客在分类页
# 看得见**——这件事不该靠"我记得跑过"来判断，要有只读的机械检查。
# 退出码：0 干净 / 1 有残留（[fixture-leftover] 逐行点名）/ 2 读不到接口（**无法确认**，
# 不是"没有"）。**非门禁**：真删不掉时金色的那条用例自己就是红的（fail=1 已经置位），
# 这里只是把"生产库里留了什么"讲清楚。
$PY eval/golden_fixture.py --verify >> "$LOG" 2>&1 || echo "[$TS] 真写夹具有残留或读不到公开接口（非门禁，见上面的 [fixture-leftover]/[fixture-check-failed] 行）" >> "$LOG"
echo "--- golden set (149 条用例；其中 3 条真写按设计不在此处跑 = 146 条真实对话，约 25 分钟) ---" >> "$LOG"
# 20260924 起：给「需要真身份」的那类用例一个 uid，治那 5 条常年 SKIP（moderation_report_admin /
# user_report_admin / 三条 *_unresolved_target_honest）。取的不是主人的 uid=1——那条写用例会真改
# 主人自己的数据。
# 未设该变量时 run_golden 会**响亮跳过并打印**（跳过关乎通过率分母，不静默豁免）。
#
# ⚠️ **20261017 换指专用夹具管理员 756**，两笔迁移（都在父仓 `scripts/migration/`）：
#
#   ① `fixture_admin_closeout_20261017.sql` 把 721 收口——口令换成新的不可知哈希 +
#      由 admin 降为 user。721 是 20260924 建的评测残留夹具，而那份文件自己写下的纪律是
#      「不要把这两行加进任何会真写业务数据的自动化」，`GOLDEN_ALLOW_REAL_WRITE=1` 正是
#      那种自动化，这条纪律一直没照办。降权之后它不再是 admin，而 `middleware.rs` 的准入
#      判据**从库里现读 role**（不信令牌 claims）⇒ 继续指它这几条用例会全线 403。
#   ② `golden_admin_fixture_20261017.sql` 新建 **756 `agent_golden_admin_20261017`**
#      （admin / status=0 / 口令 = 四个随机 UUID 的 SHA-256 ⇒ **结构上登录不进去**），
#      并把待办族夹具从 721 挪到它名下。
#
# **为什么是新建一个号，而不是就近指 748 `demo_admin`**（中途确实短暂指过它）：748 是
# **在用的号**（20261017 实测 3 会话 / 89 条消息 / 2 篇文章 / 一条真人待办），而后台首页
# 那张待办卡保存走的是 `PUT /api/protected/todos` —— **整份列表覆盖**
# （`src/routes/todos.rs::save_todos` 事务内先删该用户全部行再逐条插入）⇒ 谁在首页点一下
# 保存，夹具就没了。而待办列表是**按 uid 过滤**的（同文件每个 handler 都做
# `UserId.eq(uid)`）⇒ 夹具的主人**必须**等于这个变量。原设计挑一个「从不登录」的号，
# 要的就是"那份列表永远不会有第二个写者"这条性质。
#
# 令牌不依赖代次：`tools/base._sign_local_jwt` 刻意**不带 `ver`**，而 Rust 侧 `Claims.ver` 是
# `Option`，没有该声明就跳过代次比对、只判冻不冻结 ⇒ 换指任何未冻结的 admin 都能用，
# 与 `token_version` 无关（756 的是 0）。
export GOLDEN_ADMIN_UID=756
# 20260924 起：普通用户那条通道（722，role=user）。作用是把「被 role 闸拦住」与「谁调不动」
# 分开——不给 uid 时 uid=0 是 agent 侧哨兵，`admin_write_denied_user` 会因为"谁都调不动"而
# 通过，通过的理由是错的。接线刻意排在 gate 能力否定误伤修好**之后**：误伤在时，这条用例
# 约一半概率被换成兜底道歉 ⇒ 夜间门禁 1.000 会间歇性变红（哨兵一响就没人看了）。
# 未设该变量时同样**响亮跳过并打印**（跳过关乎通过率分母，不静默豁免）。
export GOLDEN_USER_UID=722
# 20260926 起：**账号夹具残留哨兵**（`agent_fixture_freeze_a`，见
# scripts/migration/golden_fixture_account_20260926.sql）。刻意**不**与分类族那条并排：
# 它要一个**只读管理员身份**去读后台账号名录（`/api/temp-users`），而身份就在上面刚 export
# ——并排放的话它每次都会报"读不到"（退出码 2），一条长鸣而没人看的哨兵等于没有。
# 与分类族有一处刻意的不对称：那边用例跑完夹具就该消失（前缀族里出现任何一行都是残留），
# 这边夹具是**常驻的**（用例只改它的 status，不复位就得重跑 SQL）⇒ 声明的那个放行，只报
# "没声明的"。它另有一层用意：那条用例做的是**解冻**，夹具若被谁弄成"正常"，用例照样跑完、
# 回执照样生成、断言照样过（后端那个方向有真 no-op 分支）——**一条静默的绿**。夹具闸那边靠
# `wrong_state` 直接不让它跑；这里只负责让"现在到底是什么状态"进日志。
# 退出码语义与分类族逐字相同：0 干净 / 1 有未声明的残留 / 2 读不到名录（**无法确认**）。
# **非门禁**，同分类族那条。
$PY eval/golden_fixture_account.py --verify >> "$LOG" 2>&1 || echo "[$TS] 账号夹具有残留或读不到后台账号名录（非门禁，见上面的 [fixture-leftover]/[fixture-check-failed] 行）" >> "$LOG"
# 20261003 起：**留言族与待办族两条哨兵也进夜间**——这两族此前是"写了没人跑"
# （`golden_fixture_board.py` 20261001 就位、`golden_fixture_todo.py` 20261003 就位，
# 而夜间只接了分类族与账号族两条）。一条没人执行的哨兵等于没有，而这两族的夹具恰恰
# 都是**常驻**的（用例只弹卡/只审一条，都不删它，见两边模块的 `leftovers` 头注）：
# 夹具被谁清掉、被改坏，都只能靠它讲。
# 位置同账号族那条（都要上面那个 `GOLDEN_ADMIN_UID` 去读管理员域只读接口，并排会恒报
# "读不到"）。退出码语义与另两族逐字相同：0 干净 / 1 有未声明的残留 / 2 读不到（**无法确认**，
# 不是"没有"）。**非门禁**，同前两条。
$PY eval/golden_fixture_board.py --verify >> "$LOG" 2>&1 || echo "[$TS] 留言夹具有残留或读不到后台留言清单（非门禁，见上面的 [fixture-leftover]/[fixture-check-failed] 行）" >> "$LOG"
$PY eval/golden_fixture_todo.py --verify >> "$LOG" 2>&1 || echo "[$TS] 待办夹具有残留或读不到后台待办列表（非门禁，见上面的 [fixture-leftover]/[fixture-check-failed] 行）" >> "$LOG"
# 20260926 起：**令牌收回/账号冻结探针每日进夜间（用户点名授权）**，位置刻意在 golden 之前。
#
# 为什么每日跑它：这一整套判据（冻结立即作废令牌、改密码作废旧代次、向量图谱与河灯两处
# 旁路收口）全是"库里的值与令牌里的值比一次"，而它的失效方式是**静默**的——列不存在 ⇒
# 全站 401；列在但判据读错列 ⇒ 收回不生效（一切看起来正常）。只有真发一遍请求才分辨得出。
#
# 为什么在 golden 之前：`GOLDEN_ADMIN_UID` 就是这个探针要验的那个身份（`--admin-uid`），
# 而 run_golden 现在开跑前也会做一次只读在位检查（eval/identity_preflight.py）。两者是
# 同一件事的两层——探针验**语义**（收回真的生效）、在位检查验**可用性**（这个 uid 今天
# 还活着吗）。先探针后 golden，坏掉时日志里先出现的是一行 ❌ 语义断言，而不是十几条
# 看着像"模型退化"的 golden 红。
#
# 它**真的写生产库**（本机即生产）：靶子是它自己建的一次性账号 `probe_revoke_<时间戳>`，
# 跑完在 finally 里删掉；不碰任何真实用户（冻结会顺手把代次 +1，落在真人账号上等于把人
# 踢下线）。**`--no-self-probe` 是刻意的**（20260926 用户拍板）：探针的【六】【七】是
# 负向断言——真去冻结/删**管理员自己的 uid**并期望后端拒绝；那两节手动跑（有人看着）要跑，
# 但无人值守的 cron 不该每晚对着一个真实账号发这两个写请求。摘掉之后夜间验的仍是它的本分：
# 冻结/改密码真的收回令牌、向量图谱与河灯两处旁路真的收口。
#
# **门禁**（与 golden 同级）：它红了说明这套安全判据当天不成立，比能力题红严重得多。
# 探针在父仓（不是本仓）、只用标准库 ⇒ 用系统 python3 跑，不引本仓 venv。
PROBE=/home/ubuntu/Saudade-Blog/scripts/probe_token_revoke.py
echo "--- 令牌收回/冻结 真链路探针 (写生产库: 只建删 probe_revoke_* 靶子账号, 门禁) ---" >> "$LOG"
if [ ! -f "$PROBE" ]; then
  fail=1
  echo "[$TS] 令牌收回探针不在位：$PROBE（父仓文件被移动/改名了？这一夜**没验**收回语义）" >> "$LOG"
else
  python3 "$PROBE" --admin-uid "$GOLDEN_ADMIN_UID" --no-self-probe >> "$LOG" 2>&1
  # 退出码分开措辞（20260929）：3 = **前置不可用**，这一夜没验（与 golden 的
  # `identity_preflight` 同源）；其余非 0 = 语义回归。两者都置 fail=1（"没评"不是
  # "通过"），但**日志里那句话必须说对**——此前一律印「冻结/改密码/旁路收口有回归」，
  # 而实测连续三夜红的是【零】的 401（`admin` 令牌写死 `ver=0`，而 721 的代次在
  # 20260924 的测试账号轮换里变成了 1）：一句假的安全警报，把人带去查闸门，而真问题
  # 只是仪器的形状。探针侧已改成不带 `ver`（与 identity_preflight 同形）。
  rc=$?
  if [ "$rc" -eq 3 ]; then
    fail=1
    echo "[$TS] 令牌收回探针**未评估**（前置不可用：管理员 uid 今天自己就不通——见上面 ⚠ 那行。不是「有回归」，别去查闸门）" >> "$LOG"
  elif [ "$rc" -ne 0 ]; then
    fail=1
    echo "[$TS] 令牌收回探针 FAILED（冻结/改密码/旁路收口有回归——先看上面逐条 ❌，再谈 golden）" >> "$LOG"
  fi
fi
# 20261001 起：夜间那道 golden 门禁**改口径**——不再传 `--min-pass-rate`（传了就走点
# 估计口径，20 条红会被读成"0.86 达标"），改走 `eval/landing_gate.py` 的**两层判据**：
# 硬层（回归组 / 离线套件 / 探针）0 红；采样层（能力题）**Wilson 95% 下界低于地板 0.78**
# 才置红。
#
# 20260929 那条纪律（"别在噪声上叫醒人"）**一格没松**，落点是"下界 0.78"：27 次全量实测
# 里，正常波动的红率在 0–11.7% 之间（最差 15 红/128，下界 0.8156），唯一一次真事故是
# 25 红/116（21.6%，下界 0.7012，20260927 的 provider 那次）。地板要落在**这两者中间
# 那条没人住的沟**里：0.78 在三种实测分母下都要 **≥15% 的红**（n=146 ≥23 条 / 128 ≥19 /
# 116 ≥17）才响，比最差的正常一夜整整高一档，真事故照样远远低于它。
#
# ⚠️ 地板的第一个取值是 0.82，当天就改了 —— 原因是**分母会漂**：它是拿 n=146 的历史
# 定的"15 红 ⇒ 下界 0.837 再退一档"，可用例集加了真写闸/身份前置/前提闸之后，同一批
# 全量的 n 从 146 掉到 116，同样 15 条红的下界变成 0.798。**下界不是分母无关的量**，
# 0.82 在 n=116 上 ≥13 红（11.2%）就响——正好落在实测噪声带里（最差一夜 11.7%）。
# 改这个数前先看一眼 `--red-rank` 当前的红率，别对着旧分母调。
#
# **没到档位（ENTRY 0.85）不置红**：档位是"爬到哪一级"的台阶，不是事故。夜间只把它和
# 距目标（TARGET 0.95）的差距打进日志；连续 3 夜达档时 `run_golden` 会打一行 ★ 提示抬档
# （改 `landing_gate.ENTRY`，唯一取值处）。**绝对目标今天达不到**（27 次全量里够到 0.95
# 的只有 2 次，都在 20260928 之前；此后最好的一夜下界 0.8917）⇒ "这次改动能不能上线"
# 由 A/B 回答：
#     .venv/bin/python eval/landing_gate.py --ab '<改动前报告 glob>' '<改动后报告 glob>'
# 优化工单（先清哪几条红）：`.venv/bin/python eval/landing_gate.py --red-rank`
#
# FAIL 逐条照旧打印、复审单照旧导出（与退出码无关；单子上每条红现在还印着**历史红率**，
# "慢性红还是首次"一眼可读）。退出码 3（前提不可用）与退出码 2（空分母）**不在任何放宽
# 之列**——"没评"永远不是"通过"。
$PY eval/run_golden.py >> "$LOG" 2>&1 || { fail=1; echo "[$TS] golden set FAILED (复审单 eval/report/review_*.md；回归组红 = 当天必修；采样层下界 < 地板 0.78 = 事故；未到档位不置红、只记；用户可见兜底轮次 ≥ 5% = 事故；退出码 3 = 前提不可用，不是通过率；企业落地三条见 landing_gate.py --readiness)" >> "$LOG"; }
# 20260925 起：回复出处评审（LLM-as-judge，非门禁）。确定性判据判"该出现的东西在不在"，
# 判不了"回复里有没有编出材料之外的事实"（工具只回了 3 条、回复写"共 5 条"）——这一格由它补。
# **刻意不进门禁**：同源模型评自己不构成 ground truth，"可疑"不等于"错了"（模块头注纪律 1），
# 置 fail=1 会让整夜门禁被一条观察性判据带红（哨兵一响就没人看了）。报告落
# eval/report/judge_<ts>.md，每条可疑条目都附材料原文供人核——**报告要人看**。
# 材料供给是它的前置条件：golden 跑法已把 trace 的工具返回上限放开（TRACE_TOOL_RESULT_LIMIT=40000，
# 生产是按工具分档），拿被截断的材料会把"文章里真有"的内容判成编造；判官认出截断会响亮警告。
# 跑在 golden 之后、不传 --traces（默认取最新一轮 = 刚那一轮）。
echo "--- 回复出处评审 (LLM-as-judge, 约 10 分钟, 非门禁) ---" >> "$LOG"
# PYTHONUNBUFFERED 必须留着（20260925 实测）：stdout 重定向到文件时 Python 默认**块缓冲**，
# 这一步要跑十分钟，块缓冲的表现是"日志里什么都没有、报告却已经写完"——中途想看一眼进度
# 只能看到空文件（实测过一次，误判成"跑了 0 字节"）。加它只影响缓冲、不改判据。
PYTHONUNBUFFERED=1 $PY eval/llm_judge.py >> "$LOG" 2>&1 || echo "[$TS] llm_judge 运行异常（非门禁，看 eval/report/judge_*.md）" >> "$LOG"
# Hybrid outcomes use real model calls and fake business APIs, separate from legacy scores.
echo "--- hybrid task outcomes (synthetic business API) ---" >> "$LOG"
$PY -m eval.native_contracts >> "$LOG" 2>&1 || {
  fail=1
  echo "[$TS] hybrid task outcomes FAILED; inspect _smoke_native_contracts reports" >> "$LOG"
}
$PY -m eval.daily_metrics --days 7 --json >> "$LOG" 2>&1 || \
  echo "[$TS] request metrics unavailable (diagnostic only)" >> "$LOG"

# 20260912 起：语义告警巡检（非门禁——只记录不置失败标记，避免与 golden 门禁混同）
echo "--- trace 语义告警 (近 7 天真实对话, 巡检非门禁) ---" >> "$LOG"
$PY eval/trace_alert.py --days 7 >> "$LOG" 2>&1 || echo "[$TS] trace_alert 运行异常（不影响门禁）" >> "$LOG"
# 20260921 起：trace_alert 命中现场 → golden 用例**草稿**（含真实用户文本，只写
# eval/report/ 下、不自动入库；人审后手抄进 eval/golden/basic.jsonl 并打标签）
echo "--- golden 草稿回灌 (非门禁, 只产草稿供人审) ---" >> "$LOG"
$PY eval/golden_draft.py --days 1 >> "$LOG" 2>&1 || echo "[$TS] golden_draft 运行异常（不影响门禁）" >> "$LOG"
# 20260924 起：跨源对账（非门禁）。默认窗口就是"刚过去这一天"——夜里跑，对的正是
# 刚过去的这一夜。异常时它自己写 health.log 的 WARN，这里只留一行结论在日志里。
echo "--- 跨源对账 (trace ↔ agent.log ↔ monitor.log, 巡检非门禁) ---" >> "$LOG"
$PY eval/trace_reconcile.py >> "$LOG" 2>&1 || echo "[$TS] trace_reconcile 运行异常（不影响门禁）" >> "$LOG"
# 20260925 起：单帧预算哨兵（`agent/prompt_context.py::_DETAIL_FRAME_PER`，非门禁）。
# 起因：那个常数的注释原写「覆盖站内全部文章正文长度」，而实测最长那篇去重后 26,847 字
# ——**这句话早就不成立**，且没有任何东西会告诉你它不成立：超预算的帧会走按节节选
# （保底正确、planner 少看一截、多花一轮），日志与 golden 里都看不出来。
# 非门禁的理由同语料漂移哨兵：它报的是"该维护常数了"，不是"今天的改动坏了"。
# 有超时它退出 1，这里只留一行结论 + 它自己打的逐篇明细（就在上一行日志里）。
# 20260925 批 D 起它同时量**第二把尺子**：判官的材料上限（`utils/trace.GOLDEN_MATERIAL_LIMIT`）
# ——`execute_node` 落进 trace 的就是这份帧文本，所以同一份帧长回答"判官手里的材料是不是
# 完整的"。超了同样是一行结论，不置 fail（"材料的尺子该调了"不是"今天的改动坏了"）。
echo "--- 单帧预算哨兵 (最长文章帧 vs _DETAIL_FRAME_PER / 判官材料上限, 秒级, 非门禁) ---" >> "$LOG"
$PY eval/frame_budget.py >> "$LOG" 2>&1 || echo "[$TS] frame_budget 报「有文章超过单帧预算 / 判官材料被截断」或运行异常（非门禁，看上面逐篇明细）" >> "$LOG"
# 20260925 起：trace 保留治理（压缩 + 按保留期删）。**放在最后**：上面三步都要读 trace，
# 保留期 30 天远大于它们的窗口（7 天/1 天），所以删不到它们要的东西。
# 为什么必须有人执行：logrotate 那块的 `rotate 14` 对"文件名唯一"的 trace **从来无效**
# （`.2.gz`/`.3.gz` 各 0 个、最老文件 26 天）——保留策略写进配置不等于会执行，
# 这里才是真的执行者。删了什么会逐条落进本日志（这就是审计轨迹）。
echo "--- trace 保留治理 (压缩 + 超 30 天删除; 明细即审计轨迹) ---" >> "$LOG"
$PY eval/trace_retention.py --apply >> "$LOG" 2>&1 || echo "[$TS] trace_retention 运行异常（不影响门禁）" >> "$LOG"
# 20260925 起：产物保留执行（表 = eval/retention_manifest.py）。它管的是 trace 以外那几类：
# `logs/archive/` 的保留期（90 天，审计留档与 .sql 快照由 frozen 登记项排除）、
# `eval/report/` 每族报告保留最近 20 份、词图构建中间产物保留最近 10 套。
# **这里不写任何路径与天数**——表是唯一事实源，改保留期改表里的常量，命令行不动。
# 为什么必须有这一步：`logs/archive/` 曾零引用、最老到 2026-06-10，而 `runs/` 576 份零策略
# ——同族坑第四次（前三次：R2 `--keep 3`、logrotate `rotate 14`、traces 保留期）。
# 放在最末：上面几步产出的报告（review_/reconcile_/corpus_drift_）都还在"每族 20 份"内，
# 一天落不了 20 份。删了什么逐条落进本日志 = 审计轨迹。
echo "--- 产物保留执行 (logs/archive + eval/report，明细即审计轨迹) ---" >> "$LOG"
$PY eval/artifact_retention.py --apply >> "$LOG" 2>&1 || echo "[$TS] artifact_retention 运行异常（不影响门禁）" >> "$LOG"
# 20261019 起：**PG 那一路的保留执行**（checkpoint 三张表 + 幂等账）。上一步管的是盘上的树，
# 而 20261018 采纳之后 agent 的上下文落进了 PG —— 那张登记表对它是瞎的。
# 为什么必须有这一步：`checkpoints` 每个 checkpoint 存整份快照（实测 messages 通道的 blob
# 随会话长度 1417→6646 字节单调长），checkpoint 数又随轮数线性长 ⇒ **按会话长度平方增长**，
# 而此前没有任何东西在裁它（同族第五次：R2 `--keep 3`、logrotate `rotate 14`、traces 保留期、
# `logs/archive`）。**没有配 `CHECKPOINT_DSN` 时它会印一行"这一层没量"并退 0**——那是
# 「前提不可用」，不是「跑过了、没有可删的」。删了什么逐条落进本日志 = 审计轨迹。
echo "--- PG 保留治理 (checkpoint 三张表 + 幂等账，超 30 天裁; 明细即审计轨迹) ---" >> "$LOG"
$PY eval/pg_retention.py --apply >> "$LOG" 2>&1 || echo "[$TS] pg_retention 运行异常（不影响门禁；明细见上方）" >> "$LOG"

if [ "$fail" -eq 0 ]; then
  echo "[$TS] ALL PASS" >> "$LOG"
  rm -f "$MARK"
else
  echo "[$TS] FAILED — 见上方输出" >> "$LOG"
  touch "$MARK"
fi
