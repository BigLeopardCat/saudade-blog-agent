"""golden 真身份通道的**前置在位检查**。
"""


import sys
import urllib.error
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/ 下）


sys.path.insert(0, str(ROOT))


sys.path.insert(0, str(ROOT / "eval"))


import identity_preflight as ip  # noqa: E402

FAILS: list[str] = []
check = collector(FAILS)


import tools.base as _tb  # noqa: E402

_calls: list[str] = []


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


def _code_of(src: str) -> str:
    """剥掉模块头注后的代码段（本模块只有一个 docstring，切一刀就够）。

    扫注释会自伤：头注里正写着「不自己实现签名」这些字，拿整份文本去扫等于在
    注释里判红，下一个人只会把注释删掉。
    """
    return src.split('"""')[-1]


def test_identity_preflight():
    print("① classify 全表：三个状态、两个角色")
    check(
        "admin 通道 + 200 → ok（活的管理员）",
        ip.classify(role_expected="admin", status=200)[0] == ip.OK,
    )
    check("admin 通道 + 403 → unusable（不是管理员）",
          ip.classify(role_expected="admin", status=403)[0] == ip.UNUSABLE)
    check("user 通道 + 403 → ok（活着的普通用户，这正是它的期望值）",
          ip.classify(role_expected="user", status=403)[0] == ip.OK)
    check("user 通道 + 200 → unusable（管理员跑用户通道，角色不符）",
          ip.classify(role_expected="user", status=200)[0] == ip.UNUSABLE)
    for _r in ("admin", "user"):
        _s, _d = ip.classify(role_expected=_r, status=401)
        check(f"{_r} 通道 + 401 → unusable（冻结/收回/账号不存在）", _s == ip.UNUSABLE)
        check(f"{_r} 通道 + 401 的说明里含「冻结」（读的人要知道可能是它）", "冻结" in _d, _d)
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
    _slept: list[float] = []
    _orig_urlopen, _orig_sign, _orig_sleep = (
        ip.urllib.request.urlopen, _tb.host_credential, ip.time.sleep)
    try:
        # 签名函数换成空壳：本节一次都不真签、更不发请求
        _tb.host_credential = lambda uid: "stub-token"
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
        _tb.host_credential = _orig_sign
        ip.time.sleep = _orig_sleep
    print("\n④ 必须用 agent 那条签名路径（与探针刻意相反）")
    check("引用 tools.base 的签名函数与后台地址",
          "from tools.base import" in _SRC and "host_credential" in _SRC and "ADMIN_BASE" in _SRC)
    _IP_CODE = _code_of(_SRC)
    check("不自己实现签名/解码（代码段里无 jwt/hmac/base64）",
          not any(p in _IP_CODE for p in ("import jwt", "hmac", "base64")),
          str([p for p in ("import jwt", "hmac", "base64") if p in _IP_CODE]))
    check("只读：代码段里无写方法、无写接口前缀",
          not any(p in _IP_CODE for p in (".post(", ".put(", ".delete(", "/protected/")),
          str([p for p in (".post(", ".put(", ".delete(", "/protected/") if p in _IP_CODE]))

    print()
    if FAILS:
        print(f"=== {len(FAILS)} 项失败 ===")
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("=== 全部通过 ===")

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_identity_preflight()
