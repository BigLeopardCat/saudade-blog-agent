"""Blog domain discovery descriptions and frequently used UI actions."""

from agent.adminops_account import role_cn
from agent.domains.account_actions.targets import Role
from agent.domains.notices.targets import DEFAULT_TITLE

DOMAIN_DESCRIPTIONS = {
    "task_planning": "Optional task checklist for multiple deliverables or work continuing after "
                     "approval. Skip for simple questions and chat. No business effects.",
    # Discovery identifies a domain; argument and execution contracts live on its tools.
    "public_activity": "分页读取公开公告、河灯留言和说说；含公开留名，不含发表账号或待审记录。",
    "utilities": "查询当前时间或城市天气估算。",
    "devices": "查询可访问设备，或在已授权OLED设备显示文字。",
    "public_catalog": "分页查询公开文章清单、置顶文章、分类、分层标签和文章计数。",
    "conversation_images": "Re-read a referenced conversation image when details are needed.",
    "own_activity": "读取本人河灯留言及审核状态，或当前会话聊天原文。",
    "admin_catalog": "后台文章分页查询（含草稿、私密）；准备单篇或整批状态、置顶确认，"
                     "或文章标签增删替换（可同卡创建标签）。仅管理员。",
    "admin_notices": "按账号编号或完整账号名准备个人通知确认；需收件人和正文，"
                     f"标题默认「{DEFAULT_TITLE}」。仅管理员。",
    "admin_tags": "查询标签字典，准备新建、修改层级或字段、删除确认；"
                  "文章挂标签用admin_catalog。仅管理员。",
    "admin_account_actions": "Find accounts by login name or nickname; "
                             "exact lookup by ID or username. "
                             "Prepare freeze/unfreeze, "
                             "role change, mute/unmute. Preparation resolves the target "
                             "and current state. Admin only. Roles: "
                             + ", ".join(f"{role.value}={role_cn(role.value)}" for role in Role)
                             + ".",
    "admin_announcements": "准备站内公告发布、修改、删除确认。仅管理员。",
    "admin_categories": "查询文章分类，按编号或完整名称准备新建、修改、删除确认。仅管理员。",
    "admin_work_queue": "查询额度重置申请、本人后台待办和日程；"
                        "准备待办变更、额度审批或重置确认。仅管理员。",
    "admin_moderation": "查询河灯留言、文章评论及审核状态，含匿名留言的真实发表账号；"
                        "准备审核或删除确认。仅管理员。",
    "admin_reports": "Read server health, user statistics and article traffic reports. Admin only.",
    "site": "打开站内页面、设置主题和特效；查询作者信息、社交链接和站点功能。",
    "favorites": "查询本人收藏，准备收藏增删确认。",
    "notifications": "分页读取本人最近通知、未读条目和原始跳转定位，准备已读确认；查看不标记已读。",
    "messages": "查询本人信件摘要、单封正文，准备收件箱已读确认；查看不标记已读。",
}


_BROWSER_ACTIONS = ("navigate_page", "set_page_appearance")


def initial_tools(tools, domains):
    # These operations remain authorized by the domain builder and again on invocation.
    browser = domains.get("site") or {}
    device = domains.get("devices") or {}
    accounts = domains.get("admin_account_actions") or {}
    return {
        **tools,
        **{name: browser[name] for name in _BROWSER_ACTIONS if name in browser},
        **{name: device[name] for name in ("device_oled_display",) if name in device},
        **{name: accounts[name] for name in ("list_accounts",) if name in accounts},
    }
