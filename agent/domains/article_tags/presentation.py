"""Readable tag changes without internal version or hierarchy metadata."""

from agent.reports import sanitize_untrusted


def labels(tags):
    return "、".join(f"「{sanitize_untrusted(tag['name'], 40)}」" for tag in tags) or "无标签"


def change_summary(plan, intent=None):
    before, after = plan["before"], plan["after"]
    old = {(tag["level"], tag["id"]): tag for tag in before}
    new = {(tag["level"], tag["id"]): tag for tag in after if tag["id"] is not None}
    added = [tag for tag in after if tag["id"] is None or (tag["level"], tag["id"]) not in old]
    removed = [tag for tag in before if (tag["level"], tag["id"]) not in new]
    lines = [f"文章：《{sanitize_untrusted(plan['title'], 255)}》"]
    if added:
        lines.append("添加标签：" + labels(added))
    if removed:
        lines.append("移除标签：" + labels(removed))
    created = [tag for tag in added if tag["id"] is None]
    for level, label in ((1, "一级"), (2, "二级")):
        selected = [tag for tag in created if tag["level"] == level]
        if selected:
            lines.append(f"将新建{label}标签：" + labels(selected))
    if intent and any(tag["level"] == 2 for tag in added) and intent.get("parent_tag"):
        lines.append("所属一级标签：" + sanitize_untrusted(intent["parent_tag"], 40))
    if intent and intent.get("color") and created:
        lines.append("新标签颜色：" + intent["color"])
    return "\n".join(lines)


