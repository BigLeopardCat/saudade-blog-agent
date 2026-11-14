"""Blog domain discovery descriptions and frequently used UI actions."""

from agent.domains.account_actions.targets import Role
from agent.domains.notices.targets import DEFAULT_TITLE
from agent.role_labels import role_cn
from agent.runtime.catalog import capability_overview

# Discovery identifies the task; detailed arguments belong to the loaded tools.
DOMAIN_DESCRIPTIONS = {
    "inbox": "Combined unread counts and headers for 通知 + 站内信; "
    "individual actions use notifications or messages.",
    "task_control": "Save and pause the current unfinished task for later explicit resume; "
    "not remembering facts, business to-dos or timed reminders.",
    "admin_image_generation": "AI image generation: approve prompt/references, inspect results, "
    "then approve gallery storage.",
    "admin_talks": "Read administrator 说说 and publisher IDs; publish, schedule, edit or delete. "
    "Guestbook uses admin_moderation.",
    "admin_gallery": "Read/display/inspect gallery images, metadata, folders and transfer status.",
    "admin_gallery_management": "Prepare image/folder edits, transfers, favorites and deletion. "
    "Article covers use admin_article_management.",
    "article_editor": "Collaborate on the open editor: draft, title/body review, summary approval "
    "and in-place publication.",
    "admin_automations": "Persistent reminders, scheduled read-only reports and announcements. "
    "Other scheduled writes use their domain.",
    "public_activity": "Search public announcements, guestbook (河灯留言) and talks; read latest "
    "entries. Talks are admin-published; display names are not account identities.",
    "utilities": "Read the current time.",
    "task_planning": "Agent内部执行清单，不是用户待办或提醒。仅用于复杂任务（3步以上）"
    "或用户明确要求的执行计划。",
    "devices": "User-authorized OLED screens: list screens and change display content.",
    "public_catalog": "Public article lists, per-article counters, pinned articles, categories "
    "and hierarchical tags.",
    "conversation_images": "Inspect a referenced conversation image.",
    "own_activity": "Read current-conversation chat history (聊天记录), or your own "
    "河灯/留言 and review status.",
    "admin_catalog": "Read administrator article metadata, including private/drafts.",
    "admin_article_management": "Prepare article covers with layout previews, state, pinning, "
    "tags or permanent deletion.",
    "admin_notices": "Prepare personal notifications to specified recipients; "
    f"default title: {DEFAULT_TITLE}.",
    "admin_tags": "Read/create/edit/delete tag dictionary and parent tags. "
    "Article-tag assignment uses admin_article_management.",
    "admin_accounts": "Read account metadata and operation eligibility (冻结/禁言资格). "
    "For mute requests without a duration, check eligibility here before asking for duration.",
    "admin_account_actions": "Prepare account freeze, mute or role changes. "
    "Missing mute duration uses admin_accounts; custom messages use admin_notices. "
    + ", ".join(f"{role.value}={role_cn(role.value)}" for role in Role),
    "admin_announcements": "Global announcement publication, editing and batch deletion; deletion "
    "also removes recipients' copies.",
    "admin_categories": "Category dictionary; prepare or schedule creation, editing and deletion.",
    "admin_work_queue": "用户待办 (business to-dos), batch deletion, quota applications/reset; "
    "not private planning.",
    "admin_moderation": "Read guestbook/comment counts, records and anonymous publishers; "
    "prepare moderation/deletion.",
    "admin_reports": "Aggregate article counters/rankings, period reports, user statistics "
    "and server health.",
    "site": "Current site feature guide, caller navigation availability, "
    "author profile and social links.",
    "favorites": "Your favorites and confirmed additions/removals.",
    "notifications": "Your notices/unread, read-state and deletion, including announcement "
    "copies; deletion preserves global originals and others' copies.",
    "messages": "站内信/私信: list recent received messages, read bodies, "
    "prepare read-state changes. Reading does not mark read.",
}


_COMMON_SITE_TOOLS = ("navigate_page", "set_page_appearance", "get_blog_info")


def initial_tools(tools, domains):
    # These operations remain authorized by the domain builder and again on invocation.
    browser = domains.get("site") or {}
    selected = {
        **tools,
        **{name: browser[name] for name in _COMMON_SITE_TOOLS if name in browser},
    }
    if selected or any(domains.values()):
        overview = capability_overview(selected, domains)
        return {**selected, overview.name: overview}
    return selected
