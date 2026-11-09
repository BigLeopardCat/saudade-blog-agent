# ADR-0014：身份断言使用 PyJWT 并保留跨语言声明约束

- **状态**：验签库与声明校验已采纳；原记录中的 Agent 自签业务和设备令牌已被宿主委托替代。
- **核对日期**：2026-11-09。
- **历史来源**：[删除前记录](https://github.com/BigLeopardCat/saudade-blog-agent/blob/5e3d89e18f77ea0edf5c89811deb1346f02e80aa/docs/adr/adr-0014-assertion-verify-via-pyjwt.md)。

## 背景与决策

手写 JWT 需要自行维护算法、签名、audience 和过期边界。身份断言交由运行时依赖 PyJWT 验签，
应用继续校验声明类型、正整数账号和端点用途；不能把库验签成功等同于业务授权。

Rust 宿主的 `sub` 可以是 JSON 数字。当前验签显式使用 HS256、匹配 audience，
要求 `exp/sub/aud`，仅关闭 PyJWT 对 subject 必须为字符串的检查。
应用另外拒绝布尔、浮点、非正数和非法字符串，签名、用途和过期校验仍然开启。
缺少密钥或无效断言应拒绝，不提供绕过身份验证的开关。

## 当前安全边界

原记录把三处自签/验签一起迁至库；其中自签博客登录态和设备令牌的做法已退役。
Agent 只持有服务断言与确认用途的独立密钥，转发宿主签发的有限委托，
不恢复共享 `JWT_SECRET` 或设备签名密钥的兼容路径。
确认卡的域分隔 HMAC 是独立协议，不因 JWT 库替换而改成 JWT。

实现见 `agent/api/authentication.py`，
回归见 `tests/test_assertion_contract.py`；当前委托及清理授权见
[身份与写入](../identity-and-permissions.md)。测试须覆盖宿主实际的数字 subject，
不能只用字符串夹具证明跨语言兼容。
