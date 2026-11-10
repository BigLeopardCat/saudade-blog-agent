"""Blog domain discovery descriptions and frequently used UI actions."""

from agent.domains.account_actions.targets import Role
from agent.domains.notices.targets import DEFAULT_TITLE
from agent.role_labels import role_cn

# Discovery identifies the task; detailed arguments belong to the loaded tools.
DOMAIN_DESCRIPTIONS = {
    "inbox": "Read your unread notices and messages together: counts and recent headers; "
    "no read-state changes. Details and actions use notifications/messages.",
    "task_control": "Save and pause the current unfinished task for later explicit resume; "
    "not remembering facts, business to-dos or timed reminders.",
    "admin_image_generation": "AI image generation: approve prompt/references, inspect results, "
    "then approve gallery storage.",
    "admin_talks": "Publish or schedule 说说, edit or permanently delete administrator posts; "
    "not guestbook moderation.",
    "admin_gallery": "Read gallery index, folders, full image metadata, image display/inspection "
    "and transfer status. No mutation preparation.",
    "admin_gallery_management": "Prepare gallery edits, transfers, favorites, deletion "
    "and folders. Known IDs go directly to preparation; "
    "article covers use admin_article_management.",
    "article_editor": "Collaborate on the open editor: draft, title/body review, summary approval "
    "and in-place publication.",
    "admin_automations": "Persistent reminders, scheduled read-only reports and approved "
    "announcements. Other scheduled writes use their domain's prepare tool. "
    "Relative delays start after approval.",
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
    "admin_article_management": "Prepare article covers (prepare_article_cover), state, "
    "pinning, tags or permanent deletion. Known IDs need no lookup; tag assignment creates "
    "missing tags on approval; covers include layout previews.",
    "admin_notices": "Prepare a personal notification: recipient and body; "
    f"default title: {DEFAULT_TITLE}.",
    "admin_tags": "Tag dictionary and confirmed edits/deletion. "
    "Creation: ask for a missing name before loading; color is optional. "
    "Article-tag assignment: admin_article_management.",
    "admin_accounts": "Read account metadata and operation eligibility (冻结/禁言资格). "
    "For mute requests without a duration, check eligibility here before asking for duration.",
    "admin_account_actions": "Prepare freeze, mute or role changes. Mute requires a duration "
    "or explicit permanence; missing duration or eligibility checks use admin_accounts. "
    "Resolve ambiguous names first. System notices are fixed templates; "
    "custom messages use admin_notices. "
    + ", ".join(f"{role.value}={role_cn(role.value)}" for role in Role),
    "admin_announcements": "Global announcement publication, editing and batch deletion; deletion "
    "also removes recipients' copies.",
    "admin_categories": "Category dictionary; prepare or schedule creation, editing and deletion.",
    "admin_work_queue": "用户待办 (business to-dos), quota applications/reset, including "
    "batch deletion. Known account IDs can reset directly; not private planning.",
    "admin_moderation": "Guestbook/comment counts (get_moderation_status, no body reads), "
    "records, anonymous publishers (list_admin_board), confirmed moderation/deletion.",
    "admin_reports": "Article view/like/favorite rankings and totals (get_note_stats), "
    "period reports, user statistics and server health.",
    "site": "Current site feature guide, caller navigation availability, "
    "author profile and social links.",
    "favorites": "Your favorites and confirmed additions/removals.",
    "notifications": "Your notices/unread, read-state and deletion, including announcement "
    "copies; deletion preserves global originals and others' copies.",
    "messages": "Your inbox summaries/body and read-state approval; reading does not mark read.",
}


_BROWSER_ACTIONS = ("navigate_page", "set_page_appearance")


def initial_tools(tools, domains):
    # These operations remain authorized by the domain builder and again on invocation.
    browser = domains.get("site") or {}
    return {
        **tools,
        **{name: browser[name] for name in _BROWSER_ACTIONS if name in browser},
    }
