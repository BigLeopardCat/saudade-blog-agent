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
    "admin_gallery_management": "Prepare single/bulk gallery edits, moves/copies, favorites, "
    "deletion, folders and article covers. Known article/image IDs go directly to preparation; "
    "it verifies targets and provides cover layout previews. "
    "Discover unknown IDs in admin_gallery.",
    "article_editor": "Collaborate on the open editor: draft, title/body review, summary approval "
    "and in-place publication.",
    "admin_automations": "Persistent reminders, scheduled read-only analysis and approved "
    "announcement publication. Other business changes use the relevant domain prepare tool "
    "with schedule. Clock times use run_at; relative delays use delay_seconds after approval. "
    "confirm lifecycle changes.",
    "public_activity": "Search public announcements, guestbook and talks by keyword; read latest "
    "entries. Public display names are not account identities.",
    "utilities": "Read the current time.",
    "task_planning": "Optional private checklist for complex work (3+ actions) "
    "or explicit task plans. "
    "Not for chat, memory, user to-dos or reminders.",
    "devices": "List accessible devices and control authorized OLED displays.",
    "public_catalog": "Public article lists, counters, pinned articles, categories "
    "and hierarchical tags.",
    "conversation_images": "Inspect a referenced conversation image.",
    "own_activity": "Read your guestbook entries/status or this conversation's original messages.",
    "admin_catalog": "Read administrator article metadata, including private/drafts.",
    "admin_article_management": "Prepare article state/pinning, tags or permanent deletion "
    "with impact review. Known article IDs can go directly to preparation.",
    "admin_notices": "Prepare a personal notification: recipient and body; "
    f"default title: {DEFAULT_TITLE}.",
    "admin_tags": "Tag dictionary and confirmed tag creation, edits or deletion. "
    "Article-tag assignment: admin_article_management.",
    "admin_accounts": "Read account metadata; check exact-ID operation eligibility without "
    "a mute duration. Read-only checks, not mutation preparation.",
    "admin_account_actions": "Check exact-ID eligibility (no mute duration needed); "
    "prepare account freeze, mute or role changes. "
    "Search ambiguous names in admin_accounts first. "
    "Automatic notices are fixed status templates; custom messages use admin_notices. "
    + ", ".join(f"{role.value}={role_cn(role.value)}" for role in Role),
    "admin_announcements": "Global announcement publication, editing and batch deletion; deletion "
    "also removes recipients' copies.",
    "admin_categories": "Category dictionary and approved creation, editing or deletion; optional "
    "schedule runs the frozen change automatically after approval.",
    "admin_work_queue": "Quota applications/reset and user-facing to-dos/reminders, including "
    "selectable batch deletion. Known account IDs can reset directly. "
    "Business records, not private planning.",
    "admin_moderation": "Guestbook/comment counts, review distributions and records. "
    "Use get_moderation_status for counts without content. "
    "Lists include excerpts and real publishers. "
    "Confirmation only for moderation or deletion.",
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
    accounts = domains.get("admin_accounts") or {}
    return {
        **tools,
        **{name: browser[name] for name in _BROWSER_ACTIONS if name in browser},
        **{name: device[name] for name in ("device_oled_display",) if name in device},
        **{name: accounts[name] for name in ("list_accounts",) if name in accounts},
    }
