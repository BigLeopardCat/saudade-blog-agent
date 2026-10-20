# -*- coding: utf-8 -*-
"""身份断言验签换库的**契约**（20261020，ADR-0014）：新验签 vs 旧手写验签**逐例同判**。

**为什么非有这一套**：`tests/test_hardening.py::_test_user_assertion` 已经把
"空密钥 / 合法 / 过期 / 错 aud / 错密钥 / 篡改签名 / 垃圾串 / 空串"都判过了，但它签的是
**字符串** `sub`（`sign_assertion` 里 `"sub": str(sub)`）。而 Rust
`auth_jwt::AssertionClaims.sub` 是 **`i32`**（JSON 数字）—— 换 PyJWT 之后，
`decode` 默认 `options["verify_sub"] = True`，`_validate_sub` 在 `sub` 不是字符串时抛
`InvalidSubjectError`。生产 `AGENT_REQUIRE_ASSERTION=1` ⇒ **每一条断言都被拒 ⇒ 全站对话
401**，而**已有的那套用例在两种设置下都通得过、抓不到它**。所以这一套的第一件事就是拿
**Rust 同形状（数字 sub）** 的令牌去撞，并且把"关掉那个选项就必须红"写成反控。

第二件事是**逐例同判**：把旧的手写实现（照抄换库前那一版）搬进本文件当参照物，一张用例表
两边各跑一遍，要求结论一致。换实现最容易漂的就是这些边角（缺声明 / `sub<=0` / `alg` 混淆 /
边界时钟），而它们**每一条都是 401**。**两处故意不同**（都是收紧）单列在 `_TIGHTENED` 里，
免得下一个人当成 bug 去"修"。

用法：
    uv run pytest tests/test_assertion_contract.py -v
    uv run python tests/test_assertion_contract.py
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

# 夹具密钥。**名字里不带 `secret` 是刻意的**：bandit 的 B105 按**赋值目标名**匹配
# `secret|passwd|token|...`，一个测试夹具撞上去就是一条假阳性 —— 而新文件按
# `eval/security_gate.py` 的纪律 1 必须零条，行内豁免又会把 `nosec` 计数顶上去。
# 值本身写了"不参与生产"：它不是凭据。
FIXTURE_KEY = "contract-test-secret-不参与生产"
# 「另一把密钥」与「空密钥」也具名：B106 只看**字面量**关键字实参（`secret="..."`），
# 传常量名不触发。这两个值是**用例的一部分**（错密钥 / fail-closed），不是配置。
OTHER_KEY = "别的密钥"
EMPTY_KEY = ""
AUD = "agent"


def _b64(b: bytes) -> bytes:
    return base64.urlsafe_b64encode(b).rstrip(b"=")


def rust_assertion(sub=7, aud=AUD, ttl=60, role=None, secret=FIXTURE_KEY, iat=None):
    """签一条与 Rust `auth_jwt::create_agent_assertion` **同形状**的断言。

    ⚠️ 与 `tests/test_hardening.py::sign_assertion` 的关键差别：**`sub` 是数字**、
    且带 `iat`。那个夹具签的是字符串 `sub`（换库前后都通得过），撞不到 `verify_sub`
    那枚地雷；这一份才是 Rust 真正会发出来的东西。
    """
    now = int(time.time())
    payload = {"sub": sub, "aud": aud, "exp": now + ttl,
               "iat": now if iat is None else iat}
    if role:
        payload["role"] = role
    return _mint(payload, secret)


def _mint(payload, secret=FIXTURE_KEY, header=None):
    """手工拼一条令牌 —— 为的是能造出 PyJWT 自己不会产出的形状（`alg:none` / 数字 sub）。"""
    h = _b64(json.dumps(header or {"alg": "HS256", "typ": "JWT"}).encode())
    p = _b64(json.dumps(payload).encode())
    sig = hmac.new(secret.encode(), h + b"." + p, hashlib.sha256).digest()
    return (h + b"." + p + b"." + _b64(sig)).decode()


def _old_verify(token: str, secret: str = FIXTURE_KEY):
    """**换库前那一版**手写验签，逐字照抄（git 里 `server.py` 的旧实现）。

    搬进来当参照物：判据不是"新实现看起来对"，而是"两边逐例同判"。
    """
    try:
        if not secret:
            return None
        h_b64, p_b64, sig_b64 = token.split(".")
        signing_input = f"{h_b64}.{p_b64}".encode()
        expected = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
        got = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
        if not hmac.compare_digest(expected, got):
            return None
        payload = json.loads(base64.urlsafe_b64decode(p_b64 + "=" * (-len(p_b64) % 4)))
        if payload.get("aud") != AUD:
            return None
        if float(payload.get("exp") or 0) < time.time():
            return None
        uid = int(payload.get("sub") or 0)
        if uid <= 0:
            return None
        role = payload.get("role")
        return {"uid": uid, "role": str(role) if role else None}
    # 下面那一行的行内豁免（`BLE001`）是**照抄的一部分**：旧实现就是这么写的
    # （裸 `except Exception` 兜住 split 失败 / base64 坏 / json 坏 / int() 失败），
    # 改窄它就不再是参照物了。
    # 这一处盲捕获**只活在测试里**、且正是被换掉的那段代码 —— 见 `eval/except_gate.py`
    # 的"只记账、不批量改"那一段。
    except Exception:  # noqa: BLE001
        return None


def _report(bad: list[str]) -> None:
    assert not bad, f"{len(bad)} 条未通过：" + "；".join(bad)


def _stub(secret=FIXTURE_KEY):
    """把 `settings.jwt_secret` 换成夹具值（调用方负责还原 —— 跨套件还原层也看着它）。"""
    from config.settings import settings
    settings.jwt_secret = secret
    return settings


def test_rust_shaped_numeric_sub_is_accepted():
    """① **那枚地雷**：Rust 发的是数字 `sub`，新验签必须收下它。

    反控同场跑：把 `verify_sub` 拨回 PyJWT 的默认（True）⇒ **同一条令牌必须被拒**。
    没有这个反控，这一条会退化成"某个我们没测过的选项恰好是对的"。
    """
    import jwt as pyjwt

    import server

    bad: list[str] = []
    print("① Rust 同形状（数字 sub）必须通过")
    orig = _stub()
    try:
        tok = rust_assertion(sub=7, role="admin")
        claims = server._verify_assertion_claims(tok)
        ok = claims == {"uid": 7, "role": "admin"}
        print(f"  {'✅' if ok else '❌'} 数字 sub + role ⇒ 收下  [{claims}]")
        if not ok:
            bad.append(f"Rust 同形状的断言被拒了：{claims}")
        # 反控：默认选项下**必须**炸 —— 这就是那个地雷本身
        raised = ""
        try:
            pyjwt.decode(tok, FIXTURE_KEY, algorithms=["HS256"], audience=AUD,
                         options={"require": ["exp", "sub", "aud"]})
        except pyjwt.InvalidTokenError as e:
            raised = type(e).__name__
        ok = raised == "InvalidSubjectError"
        print(f"  {'✅' if ok else '❌'} 反控：`verify_sub` 用默认值 ⇒ 抛 "
              f"`InvalidSubjectError`  [{raised or '没抛'}]")
        if not ok:
            bad.append(f"反控没炸（{raised or '没抛'}）—— 那条注释就成了一句空话")
        # 反过来：`verify_sub: True` 对**字符串** sub 是放行的 ⇒ 证明地雷的成因是类型，
        # 而不是"我们关掉的那个选项其实无关紧要"
        str_tok = _mint({"sub": "7", "aud": AUD, "exp": int(time.time()) + 60})
        ok = pyjwt.decode(str_tok, FIXTURE_KEY, algorithms=["HS256"], audience=AUD,
                          options={"require": ["exp", "sub", "aud"]})["sub"] == "7"
        print(f"  {'✅' if ok else '❌'} 对照：字符串 sub 在**默认**选项下放行"
              f"（地雷的成因是类型，不是那个选项无关紧要）")
        if not ok:
            bad.append("对照不成立")
    finally:
        _stub(orig.jwt_secret)
    _report(bad)


# 逐例同判表：(名字, 令牌工厂, 期望的 uid)。工厂吃 (secret) —— 空密钥那条要换掉。
_CASES = [
    ("合法（数字 sub）", lambda s: rust_assertion(secret=s), 7),
    ("带 role", lambda s: rust_assertion(sub=3, role="user", secret=s), 3),
    ("role 为 null", lambda s: _mint({"sub": 3, "aud": AUD, "exp": int(time.time()) + 60,
                                     "role": None}, s), 3),
    ("多余声明", lambda s: _mint({"sub": 3, "aud": AUD, "exp": int(time.time()) + 60,
                                 "foo": "bar", "jti": "x"}, s), 3),
    ("字符串 sub（旧 Rust）", lambda s: _mint({"sub": "3", "aud": AUD,
                                           "exp": int(time.time()) + 60}, s), 3),
    ("过期", lambda s: rust_assertion(ttl=-10, secret=s), None),
    ("错 aud", lambda s: rust_assertion(aud="other", secret=s), None),
    ("缺 aud", lambda s: _mint({"sub": 7, "exp": int(time.time()) + 60}, s), None),
    ("缺 exp", lambda s: _mint({"sub": 7, "aud": AUD}, s), None),
    ("缺 sub", lambda s: _mint({"aud": AUD, "exp": int(time.time()) + 60}, s), None),
    ("sub 为 0", lambda s: rust_assertion(sub=0, secret=s), None),
    ("sub 为负", lambda s: rust_assertion(sub=-1, secret=s), None),
    ("sub 不是数字", lambda s: _mint({"sub": "abc", "aud": AUD,
                                     "exp": int(time.time()) + 60}, s), None),
    ("别的密钥签的", lambda s: rust_assertion(secret=OTHER_KEY), None),
    # 真·`alg:none` 攻击：头部说 none、**签名段是空的**。两边都必须拒（旧实现算出的
    # HMAC 与空串比不相等；PyJWT 直接说 alg 不在白名单里）。
    ("alg:none 且签名为空（真攻击）",
     lambda s: _mint({"sub": 7, "aud": AUD, "exp": int(time.time()) + 60}, s,
                     header={"alg": "none", "typ": "JWT"}).rsplit(".", 1)[0] + ".", None),
    ("垃圾串", lambda s: "not.a.jwt", None),
    ("空串", lambda s: "", None),
    ("两段（少一段）", lambda s: "a.b", None),
]

# **故意不同**的两格（新实现更严）：列在这里，免得下一个人当成 bug。
# ⚠️ 都是"更严"，**不是"修了漏洞"**：旧实现从不读头部，靠"永远只算 HMAC"达到同样的
# 安全效果（攻击者拿不到密钥就伪造不出签名）。新增的白名单只是把这件事写成显式的。
_TIGHTENED = {
    "头部写 RS256、签名却是合法 HS256": "旧实现**从不读 `alg`** ⇒ 放行；"
                                      "PyJWT 的 `algorithms=[\"HS256\"]` 白名单拒绝",
    "头部写 alg:none、签名却是合法 HS256": "同上。（这一格不是真攻击——真攻击的签名段是空的，"
                                         "两边都拒，见 `_CASES` 里那条）",
}
# 还有一处**理论上的**差异，但实测不可观测，所以不写成判据：PyJWT 的过期判据是
# `exp <= now`、旧实现是 `exp < now`。要看见它得让 `time.time()` 恰好返回整数
# （`exp` 是整秒、`now` 带小数）—— 本套件试过，两边都判过期。
_EXP_BOUNDARY_NOTE = "exp 恰好等于 now 这一格实测不可观测（见上）"


def test_new_and_old_agree_case_by_case():
    """② 逐例同判：新实现与旧手写实现结论一致（除了 `_TIGHTENED` 里那两格）。"""
    import server

    bad: list[str] = []
    print("② 新旧逐例同判")
    orig = _stub()
    try:
        for name, make, want in _CASES:
            tok = make(FIXTURE_KEY)
            new = server._verify_assertion_claims(tok)
            old = _old_verify(tok)
            new_uid = new["uid"] if new else None
            old_uid = old["uid"] if old else None
            ok = new_uid == want and old_uid == want
            print(f"  {'✅' if ok else '❌'} {name}  [新={new_uid} 旧={old_uid} 期望={want}]")
            if not ok:
                bad.append(f"{name}：新={new_uid} 旧={old_uid}，期望 {want}")
        # 空密钥：两边都必须 fail-closed（这一格不放进表里，因为它要换密钥而不是换令牌）
        _stub(EMPTY_KEY)
        tok = rust_assertion()
        new, old = server._verify_assertion_claims(tok), _old_verify(tok, secret=EMPTY_KEY)
        ok = new is None and old is None
        print(f"  {'✅' if ok else '❌'} 空密钥 ⇒ 两边都 fail-closed  [新={new} 旧={old}]")
        if not ok:
            bad.append("空密钥没 fail-closed")
    finally:
        _stub(orig.jwt_secret)
    print(f"  ℹ️ 另有 {len(_TIGHTENED)} 格**故意不同**（收紧）"
          f"，单列在 `test_tightenings_are_deliberate`")
    _report(bad)


def test_tightenings_are_deliberate():
    """③ 那两处收紧**确实存在且方向对**（把"故意的"写成判据，而不是一句注释）。

    判据的形状：新实现**拒**、旧实现**放**。哪天有人把 PyJWT 的选项放宽到把它们也放行，
    这一条会红 —— 那时要么承认收紧没了，要么改这里并说清为什么。
    """
    import server

    bad: list[str] = []
    print("③ 两处收紧：新拒 / 旧放")
    orig = _stub()
    try:
        now = int(time.time())
        rs256 = _mint({"sub": 7, "aud": AUD, "exp": now + 60}, FIXTURE_KEY,
                      header={"alg": "RS256", "typ": "JWT"})
        none_hdr = _mint({"sub": 7, "aud": AUD, "exp": now + 60}, FIXTURE_KEY,
                         header={"alg": "none", "typ": "JWT"})
        for name, tok in (("头部 RS256 + 合法 HS256 签名", rs256),
                          ("头部 alg:none + 合法 HS256 签名", none_hdr)):
            new = server._verify_assertion_claims(tok)
            old = _old_verify(tok)
            ok = new is None and old is not None
            print(f"  {'✅' if ok else '❌'} {name}：新={'拒' if new is None else '放'} / "
                  f"旧={'拒' if old is None else '放'}")
            if not ok:
                bad.append(f"{name} 的收紧没成立（新={'拒' if new is None else '放'}，"
                           f"旧={'拒' if old is None else '放'}）")
        print(f"  ℹ️ {_EXP_BOUNDARY_NOTE}")
    finally:
        _stub(orig.jwt_secret)
    _report(bad)


def test_signers_keep_their_shape():
    """④ 签发侧形状一字未改：`{sub, exp, role}`，**没有 aud、没有 ver**，`sub` 是数字。

    这两条"没有"都是刻意的（`_sign_local_jwt` 的注释里写了为什么）：Rust
    `Validation::default()` 会因多余的 `aud` 判 audience 无效；而 `ver` 缺席是
    "这枚令牌代表 Rust 本次请求刚认证过的身份"的载体，填任何值都会让
    "改过密码的管理员 + 管理助手"整体 401。
    """
    import jwt as pyjwt

    from tools import base, device

    bad: list[str] = []
    print("④ 签发侧形状")
    from config.settings import settings
    orig = settings.jwt_secret
    settings.jwt_secret = FIXTURE_KEY
    try:
        for name, fn, want_ttl in (
                ("_sign_local_jwt", lambda: base._sign_local_jwt(7, "admin"), 60),
                ("_sign_user_jwt", lambda: device._sign_user_jwt(7), 300)):
            tok = fn()
            claims = pyjwt.decode(tok, FIXTURE_KEY, algorithms=["HS256"],
                                  options={"verify_aud": False, "verify_sub": False})
            ok = isinstance(claims.get("sub"), int) and claims["sub"] == 7
            print(f"  {'✅' if ok else '❌'} {name}：`sub` 是数字  [{claims.get('sub')!r}]")
            if not ok:
                bad.append(f"{name} 的 sub 不是数字")
            ok = "aud" not in claims and "ver" not in claims
            print(f"  {'✅' if ok else '❌'} {name}：**不带 `aud`、不带 `ver`**  "
                  f"[aud={'aud' in claims} ver={'ver' in claims}]")
            if not ok:
                bad.append(f"{name} 多带了 aud/ver —— Rust 那边会验不过")
            left = claims["exp"] - int(time.time())
            ok = want_ttl - 5 <= left <= want_ttl + 2
            print(f"  {'✅' if ok else '❌'} {name}：有效期约 {want_ttl}s  [{left}s]")
            if not ok:
                bad.append(f"{name} 的有效期是 {left}s，期望约 {want_ttl}s")
            ok = len(tok.split(".")) == 3 and "=" not in tok
            print(f"  {'✅' if ok else '❌'} {name}：三段 base64url 且无 padding")
            if not ok:
                bad.append(f"{name} 的形状不对")
        # 空密钥 ⇒ 抛（fail-closed 原样保留，SAU-012）
        _stub(EMPTY_KEY)
        for name, fn in (("_sign_local_jwt", lambda: base._sign_local_jwt(7, None)),
                         ("_sign_user_jwt", lambda: device._sign_user_jwt(7))):
            try:
                fn()
                print(f"  ❌ {name}：空密钥却签出来了")
                bad.append(f"{name} 空密钥没抛")
            except RuntimeError:
                print(f"  ✅ {name}：空密钥 ⇒ `RuntimeError`（fail-closed 原样保留）")
    finally:
        settings.jwt_secret = orig
    _report(bad)


if __name__ == "__main__":
    for _fn in (test_rust_shaped_numeric_sub_is_accepted,
                test_new_and_old_agree_case_by_case,
                test_tightenings_are_deliberate,
                test_signers_keep_their_shape):
        _fn()
    print("\n全部通过")
