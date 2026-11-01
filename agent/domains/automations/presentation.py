"""Readable schedule receipts; labels render verified operations, never user intent."""

LABELS = {
    "create_automation": "创建计划任务",
    "pause_automation": "暂停计划任务",
    "resume_automation": "恢复计划任务",
    "cancel_automation": "取消计划任务",
}


def action_text(name, args, meta):
    label = LABELS.get(name)
    if label is None:
        return None
    definition = (meta.get("schedule") or {}).get("definition") or args.get("definition") or {}
    title = definition.get("title")
    return f"{label}「{title}」" if title else label
