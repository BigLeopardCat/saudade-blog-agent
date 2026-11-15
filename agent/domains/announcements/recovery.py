"""Host-owned idempotent announcement requests and exact receipt lookup."""

import hashlib
import json
import re
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from agent.authz import SCOPE_ADMIN_CONSOLE
from agent.domains.announcements import targets
from agent.runtime.access import configured_reads_enabled
from agent.runtime.turns import check_cancelled
from tools import base
from tools.result import ok, unavailable

PATH = "/api/protected/announcement-operations"
Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


class Outcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    changed: StrictBool
    beforeVersion: Digest
    state: targets.Plan


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operationId: Digest
    requestHash: Digest
    outcome: Outcome


def operation_id(config):
    value = ((config or {}).get("configurable") or {}).get("confirmed_operation_id")
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("缺少已批准操作的稳定编号")
    return value


def fingerprint(args):
    request = targets.intent(args["intent"])
    target, fields = request["target"] or {}, request["fields"]
    value = [args["version"], request["action"], target.get("id"), target.get("title"),
             fields["title"], fields["content"]]
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def verified(value, args, config):
    try:
        parsed = Receipt.model_validate(value).model_dump()
        if (parsed["operationId"] != operation_id(config)
                or parsed["requestHash"] != fingerprint(args)):
            raise ValueError("mismatched_receipt")
    except (ValueError, TypeError):
        return base.unavailable("公告操作回执与已批准请求不匹配，无法确认结果；本次未重新执行。")
    return parsed["outcome"]


def change(args, config):
    check_cancelled(config)
    result = base._admin_request("POST", PATH,
        {"operationId": operation_id(config), "change": args}, config)
    if isinstance(result, base.ToolResult):
        if result.meta.get("error") == "write_unknown":
            # Read back a lost response once; this branch never resends the POST.
            recovered = recover(args, config, retry=False)
            if not isinstance(recovered, base.ToolResult):
                return recovered
        return result
    return verified(result, args, config)


def recover(args, config, *, retry):
    identifier = operation_id(config)
    result = base._admin_get(PATH + "/" + identifier, config)
    if isinstance(result, base.ToolResult):
        return result
    if isinstance(result, dict) and result.get("status") == "completed":
        return verified(result.get("receipt"), args, config)
    if (isinstance(result, dict) and result.get("status") == "absent"
            and result.get("operationId") == identifier and retry):
        # Absence is not proof of nonexecution. Reissuing is safe only through this keyed API,
        # whose business transaction elects one winner even while the previous request is in flight.
        return change(args, config)
    return base.unavailable("公告操作结果仍未知；保留原操作编号，本次未重新执行。")


@tool
def get_announcement_operation(
    operation_id: Digest, config: RunnableConfig,
) -> object:
    """只读查询本人公告操作的已提交历史回执，operation_id 取自真实操作结果。
    absent 只表示暂未取得回执，不证明未执行，不重新发起或批准业务操作。
    完成回执描述当时结果，不证明公告现在仍存在或内容未变。
    """
    if not configured_reads_enabled(config, SCOPE_ADMIN_CONSOLE):
        return unavailable("需要已验签的管理员身份。")
    result = base._admin_get(PATH + "/" + operation_id, config)
    if isinstance(result, base.ToolResult):
        return result
    if isinstance(result, dict) and result.get("status") == "absent" and (
            result.get("operationId") == operation_id):
        value = {**result, "note": "暂未取得已提交回执，不证明原请求未执行。"}
        return ok(json.dumps(value, ensure_ascii=False), data=value)
    if isinstance(result, dict) and result.get("status") == "completed":
        try:
            receipt = Receipt.model_validate(result.get("receipt"))
            if receipt.operationId != operation_id:
                raise ValueError("mismatched_receipt")
        except (ValueError, TypeError, KeyError):
            return unavailable("公告历史回执无法核对。")
        return ok(json.dumps(result, ensure_ascii=False), data=result)
    return unavailable("公告操作服务未返回可核对的状态。")
