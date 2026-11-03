"""Create signed operation proposals for the shared confirmation protocol."""


def proposal(principal, cid, workflow, spec, *, executor, question, target, choices=None):
    from agent import confirm
    from agent.runtime.results import TurnResult
    from tools.base import unavailable

    specs = spec if isinstance(spec, list) else [spec]
    if choices is not None and (len(choices) != len(specs) or not 1 <= len(specs) <= 50
                                or not all(isinstance(label, str) and label for label in choices)):
        return unavailable("确认选项与操作清单不一致，本次未生成确认卡。")
    token = confirm.sign(principal.uid, cid, workflow, specs, executor=executor)
    if not token:
        return unavailable("确认服务暂不可用，本次未生成确认卡，也未执行改动。")
    expiry, jti = confirm.token_expiry(token), confirm.token_jti(token)
    options = [
        {"label": "确认执行", "value": "yes", "kind": "primary"},
        {"label": "取消操作", "value": "no", "kind": "default"},
    ]
    if choices is not None:
        options[1:1] = [{"label": label, "value": f"pick:{index}", "kind": "choice"}
                        for index, label in enumerate(choices)]
    reply = question + "\n确认后执行；修改需求直接在聊天里说。"
    return TurnResult(
        reply,
        {
            "pending_confirm": {
                "q": question,
                "opts": options,
                "token": token,
                "specs": specs,
                "workflow": workflow,
                "exp": expiry,
                "replay_safe": True,
            },
            "pending_action": {
                "task_id": f"pa_{jti}",
                "skill": workflow,
                "specs": specs,
                "target": target,
                "requested_by": "user",
                "source_event": "confirm_popup",
                "question": question,
                "options": options,
                "token": token,
                "jti": jti,
                "expires_at": expiry,
            },
            "confirm_text": reply,
            "receipts": [],
        },
    )
