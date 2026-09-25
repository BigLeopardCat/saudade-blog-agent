# -*- coding: utf-8 -*-
"""golden 真身份通道的**前置在位检查**（`eval/identity_preflight.py`，20260926）。

**为什么这条测试存在**：这个模块只有两个函数、几十行，但它判错的代价是**静默**的。
它管的是「`GOLDEN_ADMIN_UID=721` 还活着吗、角色对吗」——判错的两个方向各有一种坏法：

  · 该报 `unusable` 却报成 `ok` ⇒ 十几条真身份用例集体变红，而复审单上只有模型说的话，
    读的人会去改 prompt / 改判据（**现象与「模型退化」完全同形**，这正是它要治的病）；
  · 该报 `unknown` 却报成 `unusable` ⇒ 后端重启那 10 秒里把十几条用例摘掉，真信号
    一起被吞（「不知道就不动」在这里会变成「不知道就闭嘴」）。

所以本节①逐格锁死 `classify` 的全表（2 个角色 × 5 种状态），②锁**不对称性**本身
（`unknown` 与 `unusable` 是两个答案，不能合并），③锁 `probe` 的重试纪律
（后端明确回答不重试、「读不到」才重试），④锁它**必须用 agent 那条签名路径**
（这里与探针刻意相反：探针独立、本模块必须同源，因为它检的就是那条链路），
⑤把 `run_golden.py` 里「退出码 3 排在通过之前」这条顺序当成结构锁钉住——
顺序反了就会退 0，「没评」被读成「通过」。

无网络（`urlopen` 全程被替换）、无 LLM、秒级。
"""
import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/ 下）
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import identity_preflight as ip  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILS.append(name)


print("① classify 全表：三个状态、两个角色")
# 后端三种形态 × 两条通道期望，逐格钉死（`src/middleware.rs::auth_guard` 的三态）
check("admin 通道 + 200 → ok（活的管理员）", ip.classify(role_expected="admin", status=200)[0] == ip.OK)
check("admin 通道 + 403 → unusable（不是管理员）",
      ip.classify(role_expected="admin", status=403)[0] == ip.UNUSABLE)
check("user 通道 + 403 → ok（活着的普通用户，这正是它的期望值）",
      ip.classify(role_expected="user", status=403)[0] == ip.OK)
check("user 通道 + 200 → unusable（管理员跑用户通道，角色不符）",
      ip.classify(role_expected="user", status=200)[0] == ip.UNUSABLE)
# 401 对两条通道都是「令牌被拒」，不因角色而变
for _r in ("admin", "user"):
    _s, _d = ip.classify(role_expected=_r, status=401)
    check(f"{_r} 通道 + 401 → unusable（冻结/收回/账号不存在）", _s == ip.UNUSABLE)
    check(f"{_r} 通道 + 401 的说明里含「冻结」（读的人要知道可能是它）", "冻结" in _d, _d)
# 三种形态之外的码：既不是 ok 也不是 unusable 的**证据**，按「不知道」处理
for _code in (404, 500, 502, 302):
    check(f"HTTP {_code} → unknown（不是 auth_guard 的三种形态）",
          ip.classify(role_expected="admin", status=_code)[0] == ip.UNKNOWN)
check("status=None → unknown（连接失败/超时）",
      ip.classify(role_expected="admin", status=None)[0] == ip.UNKNOWN)
check("每个答案都带一句人话说明（不是空串）",
      all(ip.classify(role_expected="admin", status=s)[1] for s in (200, 401, 403, 500, None)))

print("\n② 不对称性是刻意的（别改成对称的）")
_unknown = ip.classify(role_expected="admin", status=None)[0]
_unusable = ip.classify(role_expected="admin", status=401)[0]
check("unknown ≠ unusable（前者照跑、后者摘掉并退 3）", _unknown != _unusable)
check("两个常量名不相等且都不是 ok", _unknown != ip.OK and _unusable != ip.OK)
_SRC = (ROOT / "eval/identity_preflight.py").read_text(encoding="utf-8")
check("头注里写着「不知道≠不可用」这条理由（下一个人要读得到）",
      "不知道" in _SRC and "照跑" in _SRC)

print("\n③ probe 的探测与重试纪律（urlopen 全程被替换）")
import tools.base as _tb  # noqa: E402

_calls: list[str] = []
_slept: list[float] = []
_orig_urlopen, _orig_sign, _orig_sleep = (
    ip.urllib.request.urlopen, _tb._sign_local_jwt, ip.time.sleep)


class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _stub_urlopen(behavior):
    def _f(req, timeout=None):
        _calls.append(req.full_url)
        return behavior()
    return _f


try:
    # 签名函数换成空壳：本节一次都不真签、更不发请求
    _tb._sign_local_jwt = lambda uid, role: "stub-token"
    ip.time.sleep = lambda s: _slept.append(s)

    def _run(behavior, **kw):
        _calls.clear()
        _slept.clear()
        ip.urllib.request.urlopen = _stub_urlopen(behavior)
        return ip.probe(721, **kw)

    _st, _dt = _run(lambda: _Resp(200), role_expected="admin")
    check("200 → ok", _st == ip.OK, _dt)
    check("打的是管理员域只读接口 " + ip.PROBE_PATH,
          _calls == [f"{_tb.ADMIN_BASE}{ip.PROBE_PATH}"], str(_calls))

    _st, _ = _run(lambda: _Resp(403), role_expected="user")
    check("403 + user → ok（两条通道共用一条判据，只换期望值）", _st == ip.OK)

    # 后端**明确回答**（401/403）⇒ 一次就够，不重试
    _st, _ = _run(lambda: (_ for _ in ()).throw(
        urllib.error.HTTPError("u", 401, "denied", {}, None)), role_expected="admin")
    check("401 → unusable", _st == ip.UNUSABLE)
    check("401 不重试（后端已经回答了，重试只会拖时间）", len(_calls) == 1, str(len(_calls)))

    # 读不到 ⇒ 重试（部署/重启那几秒的瞬时不可达不该把十几条用例摘掉）
    _st, _ = _run(lambda: (_ for _ in ()).throw(urllib.error.URLError("boom")),
                  role_expected="admin")
    check("连接失败 → unknown（不是 unusable）", _st == ip.UNKNOWN)
    check("连接失败会重试到 retries+1 次", len(_calls) == 3, str(len(_calls)))
    check("重试之间有退避等待", len(_slept) == 2, str(_slept))
    # 先失败后成功：以成功那次为准（这就是重试的意义）
    _seq = iter([urllib.error.URLError("boom"), _Resp(200)])

    def _flaky():
        _v = next(_seq)
        if isinstance(_v, Exception):
            raise _v
        return _v
    _st, _ = _run(_flaky, role_expected="admin")
    check("第一次失败、第二次成功 → ok（重试真的有用，不只是重试了）", _st == ip.OK)

    # 取不到签名能力（import 崩了）⇒ 不知道，不是不可用；且不抛
    _saved = dict(sys.modules)
    sys.modules["tools.base"] = None  # `from tools.base import …` 会抛 ImportError
    try:
        _st, _dt = ip.probe(721, role_expected="admin")
        check("拿不到签名函数 → unknown（不是抛出去、也不是 unusable）", _st == ip.UNKNOWN, _dt)
    finally:
        sys.modules.clear()
        sys.modules.update(_saved)
finally:
    ip.urllib.request.urlopen = _orig_urlopen
    _tb._sign_local_jwt = _orig_sign
    ip.time.sleep = _orig_sleep


def _code_of(src: str) -> str:
    """剥掉模块头注后的代码段（本模块只有一个 docstring，切一刀就够）。

    扫注释会自伤：头注里正写着「不自己实现签名」这些字，拿整份文本去扫等于在
    注释里判红，下一个人只会把注释删掉。
    """
    return src.split('"""')[-1]


print("\n④ 必须用 agent 那条签名路径（与探针刻意相反）")
# 本模块检的就是 agent 代调那条链路，所以它**必须**同源；自己另写一份签名等于
# 检了一条不存在的路（探针 scripts/probe_token_revoke.py 是刻意独立的，别照抄那边）。
check("引用 tools.base 的签名函数与后台地址",
      "from tools.base import" in _SRC and "_sign_local_jwt" in _SRC and "ADMIN_BASE" in _SRC)
_IP_CODE = _code_of(_SRC)
check("不自己实现签名/解码（代码段里无 jwt/hmac/base64）",
      not any(p in _IP_CODE for p in ("import jwt", "hmac", "base64")),
      str([p for p in ("import jwt", "hmac", "base64") if p in _IP_CODE]))
check("只读：代码段里无写方法、无写接口前缀",
      not any(p in _IP_CODE for p in (".post(", ".put(", ".delete(", "/protected/")),
      str([p for p in (".post(", ".put(", ".delete(", "/protected/") if p in _IP_CODE]))

print("\n⑤ run_golden 接线：退出码 3 排在「通过」之前（结构锁）")
_G = (ROOT / "eval/run_golden.py").read_text(encoding="utf-8")
check("run_golden 里 import 了 identity_preflight",
      "import identity_preflight" in _G)
check("两条身份通道都带上了期望角色（admin/user 各一条）",
      '("needs_admin_uid", "GOLDEN_ADMIN_UID", "admin")' in _G
      and '("needs_user_uid", "GOLDEN_USER_UID", "user")' in _G)
check("配置了 uid 也要探测（不再「设了就用」）",
      "identity_preflight.probe(" in _G and "role_expected=_role" in _G)
check("不可用 → 摘掉并计入 skipped_ids（分母随之变小 ⇒ full_run 自动为假）",
      "_identity_skipped += _need_uid" in _G and "skip_ids += _need_uid" in _G)
_i_bad = _G.index("if _precondition_bad:")
_i_pass = _G.index("    if failed == 0:\n        sys.exit(0)")
check("退出码 3 的判定在 `if failed == 0` 之前（反了就会把「没评」读成「通过」）",
      _i_bad < _i_pass, f"{_i_bad} < {_i_pass}")
check("退出码 3 在报告字段里也留了痕（skipped_identity_ids / identity_preflight）",
      '"skipped_identity_ids": _identity_skipped' in _G
      and '"identity_preflight": _preflight_rows' in _G)
check("复审单里身份前置排在最前（否则每条红都像在说模型坏了）",
      _G.index("身份前置不可用") < _G.index("回归组（regression）FAIL，本轮不得放行"))
check("退出码 3 不受 --min-pass-rate 影响（在通过率判定之前就退）",
      _G.index("sys.exit(3)") < _G.index("if pass_rate >= args.min_pass_rate:"))

print()
if FAILS:
    print(f"=== {len(FAILS)} 项失败 ===")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("=== 全部通过 ===")
