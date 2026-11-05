"""Blog domain discovery descriptions and frequently used UI actions."""

from agent.adminops_account import role_cn
from agent.domains.account_actions.targets import Role
from agent.domains.notices.targets import DEFAULT_TITLE

# Discovery identifies the task; detailed arguments belong to the loaded tools.
DOMAIN_DESCRIPTIONS = {
    "admin_image_generation": "AI image generation: approve prompt/references, inspect results, "
    "then approve gallery storage.",
    "admin_talks": "Publish, edit or permanently delete administrator posts; "
    "not guestbook moderation.",
    "admin_gallery": "Gallery images/folders, inspection, metadata edits, folder creation/empty "
    "deletion and article covers.",
    "article_editor": "Collaborate on the open editor: draft, title/body review, summary approval "
    "and in-place publication.",
    "admin_automations": "Persistent reminders, scheduled read-only analysis and approved "
    "announcement publication. Other business changes use the relevant domain prepare tool "
    "with schedule; use absolute run_at for clock times, never compute seconds. "
    "confirm lifecycle changes.",
    "public_activity": "Search public announcements, guestbook and talks by keyword; read latest "
    "entries. Public display names are not account identities.",
    "utilities": "Read the current time.",
    "devices": "List accessible devices and control authorized OLED displays.",
    "public_catalog": "Public article lists, pinned articles, categories and hierarchical tags.",
    "conversation_images": "Inspect a referenced conversation image.",
    "own_activity": "Read your guestbook entries/status or this conversation's original messages.",
    "admin_catalog": "Articles including private/drafts; confirm state, pinning "
    "article-tag changes and permanent article deletion with impact review.",
    "admin_notices": "Prepare a personal notification: recipient and body; "
    f"default title: {DEFAULT_TITLE}.",
    "admin_tags": "Tag dictionary and confirmed tag creation, edits or deletion. "
    "Article-tag assignment: admin_catalog.",
    "admin_account_actions": "Prepare account freeze, mute or role changes; preparation resolves "
    "known IDs/names and checks state. Search ambiguous names first. "
    + ", ".join(f"{role.value}={role_cn(role.value)}" for role in Role),
    "admin_announcements": "Global announcement publication, editing and batch deletion; deletion "
    "also removes recipients' copies.",
    "admin_categories": "Category dictionary and approved creation, editing or deletion; optional "
    "schedule runs the frozen change automatically after approval.",
    "admin_work_queue": "Quota applications/reset and user-facing to-dos/reminders, including "
    "selectable batch deletion. Known account IDs can reset directly. "
    "Business records, not private planning.",
    "admin_moderation": "Guestbook/comments with real publishers; confirm moderation or deletion.",
    "admin_reports": "Server health, user statistics and article traffic reports.",
    "site": "Author profile, social links, site map and page controls.",
    "favorites": "Your favorites and confirmed additions/removals.",
    "notifications": "本人通知条目、未读清单、已读和删除；包含个人通知与公告副本。"
    "清空本人列表保留全站公告原文和其他人的通知。",
    "messages": "Your inbox summaries/body and read-state approval; reading does not mark read.",
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
