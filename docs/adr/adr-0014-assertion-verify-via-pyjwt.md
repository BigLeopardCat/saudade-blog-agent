# ADR-0014：身份断言的验签与签发改用 PyJWT（`verify_sub` 那枚地雷写在最前面）

- **状态**：已采纳（20261020）
- **背景文档**：外部审计 `/home/ubuntu/审计意见.md` §1.1（JWT 实现替换）、§1.2（异常精细化）；
  父仓 `src/auth_jwt.rs`（`AssertionClaims` / `create_agent_assertion` / `verify_token`）。
- **影响范围**：三处手写 HS256 换成 `PyJWT`，**判据一字未改**：
  - `server.py::_verify_assertion_claims`（验 Rust 的身份断言，**验签侧**）；
  - `tools/base.py::_sign_local_jwt`（签 60s 代调令牌，**签发侧**）；
  - `tools/device.py::_sign_user_jwt`（签 300s 给 device-service，**签发侧**）。
  `pyproject.toml` 的 `[project].dependencies` 加 `PyJWT==2.15.1`（**运行时**依赖）。
  ⚠️ `agent/confirm.py` 的 HMAC **不在范围内**：那是**域分隔签名**（`_DOMAIN + body`），
  不是 JWT，换成 JWT 会改变它的语义。
- **判据**：`tests/test_assertion_contract.py`（4 组，含反控）+ 既有的
  `tests/test_hardening.py::_test_user_assertion` / `tests/test_confirm.py` 一字未动仍全绿。

## 决策

三处都用库。**签发侧**换的理由弱一些（那里只有几十行确定性代码，原来那句"只为一条内部
断言引一个 JWT 依赖不值得"还站得住）；**验签侧**换的理由很硬：手写验签要自己盯住
`alg` 混淆、空签名、边界时钟、`aud` 缺失这些事，而标准库不会替你看 —— 审计 §1.1 要的
就是这一处。

`[project].dependencies` 而不是 dev 组：三条路径都在**生产请求链路**上。

## ⚠️ 唯一的地雷：`verify_sub` 必须关掉

Rust 那边：

```rust
struct AssertionClaims {
    sub: i32,        // ← JSON **数字**
    aud: String,
    exp: usize,
    iat: usize,
    role: Option<String>,
}
```

PyJWT 的 `decode` 默认 `options["verify_sub"] = True`，而 `_validate_sub` 在 `sub`
**不是字符串**时抛 `InvalidSubjectError("Subject must be a string")`。
生产 `AGENT_REQUIRE_ASSERTION=1` ⇒ 每一条断言都被拒 ⇒ **全站对话 401**。

所以那一行写成：

```python
options={"require": ["exp", "sub", "aud"], "verify_sub": False}
```

**Rust 那边不能改**（`sub` 是数字是它自己的契约，改它要动 `create_agent_assertion`
与所有消费方），只能在这里关。

**为什么既有的套件抓不到它**：`tests/test_hardening.py::sign_assertion` 签的是
**字符串** `sub`（`{"sub": str(sub)}`）—— 那是为了迁就旧手写实现的宽容
（`int(payload.get("sub") or 0)` 两种都收）。而**字符串 `sub` 在 `verify_sub` 开着的时候
照样通过**，所以那套用例在"关"与"不关"两种设置下都绿。
⇒ 新套件 `tests/test_assertion_contract.py` ① 专门拿 **Rust 同形状（数字 sub + `iat`）**
的令牌去撞，并带反控：**把选项拨回默认就必须抛 `InvalidSubjectError`**。

## 逐例同判（`tests/test_assertion_contract.py` ②）

换实现最容易漂的是边角，而它们每一条都是 401。所以把**旧的手写实现逐字搬进测试**当参照物，
18 条用例两边各跑一遍：合法 / 带 role / role 为 null / 多余声明 / 字符串 sub / 过期 / 错 aud /
缺 aud / 缺 exp / 缺 sub / sub 为 0 / sub 为负 / sub 不是数字 / 别的密钥签的 /
`alg:none` 且签名为空 / 垃圾串 / 空串 / 两段。结论全部一致。

**两处故意不同**（新实现更严，单列在 `test_tightenings_are_deliberate`）：

| 格 | 旧 | 新 |
|---|---|---|
| 头部写 `RS256`、签名却是合法 HS256 | 放行（**从不读 `alg`**） | 拒（`algorithms=["HS256"]` 白名单） |
| 头部写 `alg:none`、签名却是合法 HS256 | 放行（同上） | 拒（同上） |

⚠️ **这两格是"更严"，不是"修了漏洞"**：旧实现从不读头部，靠"永远只算 HMAC"达到同样的
安全效果（攻击者拿不到密钥就伪造不出签名）。新增的白名单只是把这件事写成显式的。
真·`alg:none` 攻击（签名段是空的）**两边都拒**，那一条在用例表里。

**还有一处理论差异、实测不可观测，所以没写成判据**：PyJWT 的过期判据是 `exp <= now`、
旧实现是 `exp < now`。要看见它得让 `time.time()` 恰好返回整数（`exp` 是整秒、`now` 带小数）
—— 试过，两边都判过期。

## 顺带做的事

- **异常精细化（审计 §1.2）只在新写的这一处做**：`except (InvalidTokenError, ValueError,
  TypeError)`，不回头批量改存量 116 处（那是 `eval/except_gate.py` 的账，**只记账、不批量改**，
  理由见该闸头注：存量里那些宽 `except` 大多是**故意的降级路径**）。
  副作用：`server.py` 的 BLE001 从 2 条降到 **1 条**（基线 117 → 116）。
- **签发侧形状一个字没动**：`{sub, exp, role}`，**不带 `aud`、不带 `ver`**。
  两条"没有"都是刻意的（见 `_sign_local_jwt` 的注释）：Rust `Validation::default()` 会因
  多余的 `aud` 判 audience 无效；`ver` 缺席是"这枚令牌代表 Rust 本次请求刚认证过的身份"的
  载体，填任何值都会让"改过密码的管理员 + 管理助手"整体 401。
  新套件 ④ 把这两条判出来（不是靠注释）。
- **空密钥 fail-closed 原样保留**（SAU-012，CWE-321）：`jwt.encode` 拿空 key 照样算得出
  签名、也不报错，所以那两处 `if not secret: raise` 一个字没动。新套件 ④ 也判它。
- **为什么不是审计文档写的 `PyJWT==3.0.0`**：那个版本不存在，最新是 **2.15.1**。
- **测试夹具的密钥顺带补齐到 ≥32 字节**：PyJWT 2.15.1 起对短 HMAC 密钥发
  `InsecureKeyLengthWarning`（RFC 7518 §3.2）。生产 `JWT_SECRET` 实测 **64 字节**、
  不会触发；是 17 处夹具字面量（26~30 字节）在刷屏。**没有用 `filterwarnings` 关掉它**
  —— 关掉就是"把警告变静音"，而这条警告在真的配短密钥时是有用的。

## 上线顺序（硬要求）

依赖 + 代码落地 → CI 构建部署 → **restart `saudade-agent`**。
生产 `AGENT_REQUIRE_ASSERTION=1`，改错 = **全站对话 401**，所以这一批**单独提交、
单独一次重启**，与别的批次不共用同一次重启。
