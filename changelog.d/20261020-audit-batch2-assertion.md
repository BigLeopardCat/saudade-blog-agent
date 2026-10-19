- **身份断言的出厂默认翻成"强制"，且回退身份不再有灰度期**（20261019 安全审计 SAU-005，
  CWE-290；⚠️ 正常用户的对话行为一个字节都没变）。两半各自独立，任一半失效另一半仍然拦得住：
  ① **默认值翻转**：`AGENT_REQUIRE_ASSERTION` 的代码默认从 `0` 改成 `1` ⇒ 断言缺席/验签失败的
     请求直接 401。旧默认的理由是 20260917 的**滚动上线**（Rust 还没发头时不能把在途请求打成
     401）；那个前提今天不成立（Rust 早已发头、生产 `.env` 本来就是 1），而 `0` 只在"克隆即
     部署"的人手里生效——他们拿到的正是最不安全的那一档。评测/离线场景要旧路径得显式拨回去
     （`tests/run_all.py::_PINNED` 已按出厂档钉 `1`）。
  ② **未验成的身份不进灰度**：`authz.must_enforce(decision, principal)` 替代四个拦截点上的
     `enforcing(decision.scope)`（`graph.py` 三处 + `react_line.py` 一处）——"这一次的拒绝拦不
     拦"多看一眼**这次是谁在调**。`source="body"`（回退身份，`role` 恒 `None`）在**私有面与
     写面**上硬拦，只留 `_PUBLIC_SURFACE`（读公开内容 + 驱动它自己那一屏）。
     修好的是**一件早就该这样的事**：`check` 说零权限、`enforcing` 说先别拦，两句话在阴影里
     同时成立了一整个观测期，于是 uid 即身份（审计的复现是一句 curl）。
     ⚠️ 判据是 `source` 而不是 `role is None`：`source="golden"` 的评测直调身份同样 `role=None`，
     按 role 判会把一整族既有用例从"shadow 放行"打成 BLOCK（判据基线不该被一次加固顺带挪动）。
  ③ **`read.own` / `write.own` 仍不进 `_HARD_SCOPES`**（审计 SAU-007 的"隐私面"那半）——
     它们是**既有能力**（收藏/通知/会话历史早就在跑），满足 shadow 的观测前提；能拒它们的只有
     `zako`（整轮零工具）与未验成的身份，后者已由 ② 硬拦。上一批里那句"硬拦买不到新防线"是按
     **已收口**的情形推的——shadow 下 deny 其实是放行，本批把那段理由改掉了（`agent/authz.py`）。

  判据：`tests/test_authz.py` ⑪（三档来源各有正控 + 反控：回退身份硬拦 / 断言身份跟随开关 /
  golden 身份跟随开关，外加"全仓没有第二处裸问 `enforcing(decision.scope)`"）；
  `tests/test_hardening.py` 断言**代码默认值**是 `True`（不读环境）；两处源码字面锁
  （`test_authz.py` ⑦ / `test_admin_write.py`）跟随改成新函数名，处数仍是 3。
