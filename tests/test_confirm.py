"""确认令牌、旧图弹窗与跨语言执行边界的离线回归。

覆盖签名与身份绑定、具体参数、确认清单收窄、待办字段及无效请求零执行。
新确认领域的持久撤销和回放由 test_pending_favorite.py 等领域套件验证。
"""


from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent  # 仓根（20260924：测试统一搬进 tests/）


sys.path.insert(0, str(ROOT))


from agent import confirm
from tools import base as _base  # 工具的返回值契约（ToolResult：str 子类 + kind）

FAILED: list[str] = []
check = collector(FAILED)


import hashlib  # noqa: E402
import hmac  # noqa: E402

from config.settings import settings  # noqa: E402

def test_confirm():
    _SAVED_SECRET = settings.jwt_secret
    _STUB_SECRET = "test-secret-for-confirm-tokens-not-a-real-secret"
    settings.jwt_secret = _STUB_SECRET
    SPECS = [{"tool": "create_tag", "args": {"title": "测试标签", "parent_id": None, "color": "#eb2f96"}}]
    print("① 令牌签发/验签")
    tok = confirm.sign(7, 42, "tag_create", SPECS)
    check("往返：verify(sign(...)) 取回同一份 payload",
          (lambda p: p is not None and p["uid"] == 7 and p["conv"] == 42
           and p["workflow"] == "tag_create" and p["specs"] == SPECS)(confirm.verify(tok, 7, 42)))
    check("令牌形状：两段 base64url（正文 + 签名），长度可控",
          tok.count(".") == 1 and len(tok) < 2000, str(len(tok)))
    check("换人 → None（令牌绑 uid）", confirm.verify(tok, 8, 42) is None)
    check("换会话 → None（令牌绑会话）", confirm.verify(tok, 7, 43) is None)
    check("会话从有到无 → None（不能让绑了会话的令牌在无会话请求里生效）",
          confirm.verify(tok, 7, None) is None)
    check("篡改签名 → None",
          confirm.verify(tok[:-2] + ("aa" if not tok.endswith("aa") else "bb"), 7, 42) is None)
    check("篡改正文（换一个标签名）→ None（签名覆盖正文）",
          confirm.verify(confirm._b64e(json.dumps(
              {"v": 1, "uid": 7, "conv": 42, "exp": int(time.time()) + 600,
               "workflow": "tag_create",
               "specs": [{"tool": "create_tag", "args": {"title": "别的标签"}}]},
              ensure_ascii=False, separators=(",", ":")).encode()) + "." + tok.split(".")[1],
              7, 42) is None)
    check("格式坏（无点 / 空 / 非字符串）→ None 且不抛",
          confirm.verify("nodot", 7, 42) is None and confirm.verify("", 7, 42) is None
          and confirm.verify(None, 7, 42) is None)
    check("base64 坏 → None 且不抛", confirm.verify("!!!.???", 7, 42) is None)
    _past = {"v": 1, "uid": 7, "conv": 42, "exp": int(time.time()) - 1,
             "workflow": "tag_create", "specs": SPECS}
    _body = json.dumps(_past, ensure_ascii=False, separators=(",", ":")).encode()
    _sig = hmac.new(settings.jwt_secret.encode(), confirm._DOMAIN + _body, hashlib.sha256).digest()
    _stale = confirm._b64e(_body) + "." + confirm._b64e(_sig)
    check("过期（exp 在签发后已过）→ None（签名合法也不行）",
          confirm.verify(_stale, 7, 42) is None)
    check("TTL 是 10 分钟量级（够读完想一下，又短到过夜必失效）",
          300 <= confirm.TTL_SECONDS <= 900, str(confirm.TTL_SECONDS))
    check("token_expiry：解出来的就是签名里那一个（与 verify 的 payload 逐字相等）",
          confirm.token_expiry(tok) == (confirm.verify(tok, 7, 42) or {}).get("exp"))
    check("token_expiry：坏输入一律 0、不抛（前端按'无倒计时'处理，不影响验签）",
          confirm.token_expiry("") == 0 and confirm.token_expiry("abc") == 0
          and confirm.token_expiry("!!!.???") == 0 and confirm.token_expiry(None) == 0)
    print("\n② 令牌边界")
    check("specs 带 $ref → 不签发（引用依赖签发轮的帧，执行轮早已不在）",
          confirm.sign(7, 42, "tag_create",
                       [{"tool": "set_article_tags",
                         "args": {"article_id": 12, "add": ["$list_tags[0].name"]}}]) == "")
    check("$ref 在嵌套 list 里也算（只看顶层会漏）",
          confirm.sign(7, 42, "article_tags",
                       [{"tool": "set_article_tags", "args": {"add": ["$x[0].y"]}}]) == "")
    check("技能名为空 → 不签发（执行轮无从拼计划）", confirm.sign(7, 42, "", SPECS) == "")
    check("不可序列化的参数 → 不签发（不抛）",
          confirm.sign(7, 42, "tag_create",
                       [{"tool": "create_tag", "args": {"title": object()}}]) == "")
    settings.jwt_secret = ""
    check("密钥空缺 → **既不签也不验**（绝不降级成无签名令牌）",
          confirm.sign(7, 42, "tag_create", SPECS) == "" and confirm.verify(tok, 7, 42) is None)
    settings.jwt_secret = _STUB_SECRET   # 恢复桩值（见上方 _STUB_SECRET 处的说明）
    print("\n⑧ 令牌的一次性编号 jti：同一张令牌不许兑现两次")
    settings.jwt_secret = _STUB_SECRET
    _t1 = confirm.sign(7, 42, "tag_create", [{"tool": "create_tag_one", "args": {"name": "X"}}])
    _t2 = confirm.sign(7, 42, "tag_create", [{"tool": "create_tag_one", "args": {"name": "X"}}])
    _j1, _j2 = confirm.token_jti(_t1), confirm.token_jti(_t2)
    check("jti 是 32 位十六进制（落库列 varchar(64)，不撞不改写）",
          bool(re.fullmatch(r"[0-9a-f]{32}", _j1)) and bool(re.fullmatch(r"[0-9a-f]{32}", _j2)),
          f"{_j1!r}/{_j2!r}")
    check("  同一秒内两次签发得到**不同** jti（不派生自 uid/会话/时间——派生就会撞）",
          _j1 != _j2)
    check("  jti 取自令牌自身、与验签解出的 payload 一致（认领键的信任来源）",
          confirm.token_jti(_t1) == (confirm.verify(_t1, 7, 42) or {}).get("jti"))
    check("  坏输入给空串、不抛（签发那一轮绝不因为解不出 jti 而断）",
          confirm.token_jti("") == "" and confirm.token_jti("x.y.z") == ""
          and confirm.token_jti(None) == "" and confirm.token_jti("没有点号") == "")
    check("  版本号已 +1（改 payload 结构必须 +1：留着旧号旧令牌会'少个字段也照发'）",
          confirm._VERSION == 2)
    _v1_body = json.dumps({"v": 1, "uid": 7, "conv": 42, "exp": int(time.time()) + 600,
                           "workflow": "tag_create", "specs": []},
                          ensure_ascii=False, separators=(",", ":")).encode()
    _v1 = confirm._b64e(_v1_body) + "." + confirm._b64e(
        hmac.new(_STUB_SECRET.encode(), confirm._DOMAIN + _v1_body, hashlib.sha256).digest())
    check("  签名正确但版本为 1 的令牌验不过（旧令牌一律失效，而不是尽力解析）",
          confirm.verify(_v1, 7, 42) is None)
    settings.jwt_secret = _SAVED_SECRET
    print()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
        sys.exit(1)
    print("全部通过")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_confirm()
