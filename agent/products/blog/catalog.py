"""Blog domain discovery descriptions and frequently used UI actions."""

from agent.domains.account_actions.targets import Role
from agent.domains.notices.targets import DEFAULT_TITLE
from agent.role_labels import role_cn

# Discovery identifies the task; detailed arguments belong to the loaded tools.
DOMAIN_DESCRIPTIONS = {
    "admin_image_generation": "AI image generation: approve prompt/references, inspect results, "
    "then approve gallery storage.",
    "admin_talks": "Publish, edit or permanently delete administrator posts; "
    "not guestbook moderation.",
    "admin_gallery": "Read gallery index, folders, full image metadata, image display/inspection "
    "and transfer status. No mutation preparation.",
    "admin_gallery_management": "Prepare gallery edits, transfers, favorites, deletion, folders "
    "and covers. Known IDs go directly to verified preparation with cover previews; "
    "find unknown IDs via admin_gallery.",
    "article_editor": "Collaborate on the open editor: draft, title/body review, summary approval "
    "and in-place publication.",
    "admin_automations": "Persistent reminders, scheduled read-only reports and approved "
    "announcements. Other scheduled writes use their domain's prepare tool. "
    "Relative delays start after approval.",
    "public_activity": "Search public announcements, guestbook and talks by keyword; read latest "
    "entries. Public display names are not account identities.",
    "utilities": "Read the current time.",
    "task_planning": "Private checklist for complex work (3+ actions) or explicit plans; "
    "not chat, memory or business reminders.",
    "devices": "List accessible devices and control authorized OLED displays.",
    "public_catalog": "Public article lists, counters, pinned articles, categories "
    "and hierarchical tags.",
    "conversation_images": "Inspect a referenced conversation image.",
    "own_activity": "Read your guestbook entries/status or this conversation's original messages.",
    "admin_catalog": "Read administrator article metadata, including private/drafts.",
    "admin_article_management": "Prepare article state, pinning, tags or permanent deletion "
    "with impact review; known IDs need no prior lookup.",
    "admin_notices": "Prepare a personal notification: recipient and body; "
    f"default title: {DEFAULT_TITLE}.",
    "admin_tags": "Tag dictionary and confirmed tag creation, edits or deletion. "
    "Article-tag assignment: admin_article_management.",
    "admin_accounts": "Read account metadata and exact-ID operation eligibility; "
    "no mute duration needed.",
    "admin_account_actions": "Check eligibility without mute duration; prepare freeze, mute "
    "or roles. Resolve ambiguous names first. System notices are fixed templates; "
    "custom messages use admin_notices. "
    + ", ".join(f"{role.value}={role_cn(role.value)}" for role in Role),
    "admin_announcements": "Global announcement publication, editing and batch deletion; deletion "
    "also removes recipients' copies.",
    "admin_categories": "Category dictionary; confirmed creation, editing and deletion.",
    "admin_work_queue": "Quota applications/reset and business to-dos/reminders, including "
    "batch deletion. Known account IDs can reset directly; not private planning.",
    "admin_moderation": "Guestbook/comment counts (get_moderation_status), records and "
    "verified publishers; confirmed moderation/deletion.",
    "admin_reports": "Server health, user statistics and article traffic reports.",
    "site": "Author profile, social links, site map and page controls.",
    "favorites": "Your favorites and confirmed additions/removals.",
    "notifications": "Your notices/unread, read-state and deletion, including announcement "
    "copies; deletion preserves global originals and others' copies.",
    "messages": "Your inbox summaries/body and read-state approval; reading does not mark read.",
}


_BROWSER_ACTIONS = ("navigate_page", "set_page_appearance")


def initial_tools(tools, domains):
    # These operations remain authorized by the domain builder and again on invocation.
    browser = domains.get("site") or {}
    device = domains.get("devices") or {}
    accounts = domains.get("admin_accounts") or {}
    return {
        **tools,
        **{name: browser[name] for name in _BROWSER_ACTIONS if name in browser},
        **{name: device[name] for name in ("device_oled_display",) if name in device},
        **{name: accounts[name] for name in ("list_accounts",) if name in accounts},
    }
