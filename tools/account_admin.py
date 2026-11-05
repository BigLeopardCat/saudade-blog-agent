"""Legacy account directory adapter; native mutations use versioned domain operations."""


from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from tools import base as _base
from tools.result import ToolResult, empty, ok, unavailable


def _user_directory(config: RunnableConfig) -> dict[int, dict] | ToolResult:
    """Read the host account directory, whose successful response is a bare array.

    Reject unavailable or malformed responses rather than treating them as an empty directory."""
    if not isinstance(config, dict) or "configurable" not in config:
        raise TypeError(
            f"_user_directory 收到的东西不是 config（{type(config).__name__}）——"
            f"调用方很可能把名录快照或别的 dict 当 config 传了；快照是第三个形参 "
            f"`index=`（见 _find_named_user / _find_user_by_id 的签名）")
    uid = _base._device_get_user_id(config)
    if uid <= 0:
        return unavailable("无法获取当前用户身份，后台账号列表不可用")
    headers = {"Authorization": "Bearer "
               + _base.host_credential(uid)}
    try:
        resp = _base._client.get(f"{_base.ADMIN_BASE}/api/temp-users", headers=headers, timeout=15)
    except Exception as exc:
        _base.logger.error("user directory call failed: %s", exc)
        return unavailable(f"读后台账号列表的请求失败: {exc}")
    if resp.status_code in (401, 403):
        return unavailable("当前身份无权访问后台账号列表（该功能仅管理员可用）")
    if resp.status_code != 200:
        return unavailable(f"后台账号列表返回 HTTP {resp.status_code}")
    try:
        rows = resp.json()
    except Exception:
        return unavailable("后台账号列表返回的不是 JSON")
    if not isinstance(rows, list):
        return unavailable("后台账号列表返回的形状不对（不是数组），读不出账号名录")
    out: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            rid = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        row["id"] = rid
        out[rid] = row
    return out

@tool
def list_accounts(config: RunnableConfig) -> str | ToolResult:
    """List the host account directory with IDs, login names, roles and moderation state.

    Requires administrator authorization. Use the domain catalog for nickname search."""
    from agent import adminops_account as _account
    rows = _base._user_directory(config)
    if isinstance(rows, ToolResult):
        return rows
    listed = [r for r in (rows or {}).values() if isinstance(r, dict)]
    if not listed:
        return empty(_account.render_account_roster([]))
    return ok(_account.render_account_roster(listed),
              meta={"count": len(listed),
                    "usernames": [str(r.get("username") or "").strip()
                                  for r in listed]})

def _reachable_uids(config: RunnableConfig):
    """Return IDs visible in the host directory, or None when visibility cannot be verified."""
    idx = _base._user_directory(config)
    if isinstance(idx, ToolResult):
        return None
    return {uid for uid in idx if isinstance(uid, int)}

__all__ = ['list_accounts']
