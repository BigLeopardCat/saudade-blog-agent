"""Blog domain discovery descriptions and frequently used UI actions."""

from agent.adminops_account import role_cn
from agent.domains.account_actions.targets import Role
from agent.domains.notices.targets import DEFAULT_TITLE

DOMAIN_DESCRIPTIONS = {
    "public_activity": "分页读取公开公告、已通过审核的河灯留言和说说；"
                       "可按编号读取内容与公开留名，不含发表账号、待审或驳回记录。",
    "utilities": "读取当前时间或指定城市的当前天气模型估算；"
                 "天气附来源、有效时刻与地点，重名时核对候选。",
    "devices": "读取当前账号可访问的设备（含共享演示设备），或按请求显示OLED文字；"
               "仅已授权身份可下发，区分下发与设备确认，不管理物联网平台。",
    "public_catalog": "分页读取公开文章清单、置顶文章、分类和分层标签及公开文章计数；不读取正文。",
    "conversation_images": "Re-read a referenced conversation image when details are needed.",
    "own_activity": "读取本人河灯留言的状态、理由与分页内容，以及当前会话的真实聊天文本。"
                    "不读取其他账号数据或其他会话，不加载图片，只读。",
    "admin_catalog": "分页读取后台文章清单（含草稿、私密文章的元数据）。"
                     "可按分类、关键词或文章编号筛选；可准备文章状态、置顶或标签增删替换确认；标签缺失时可同卡创建并挂载，仅管理员可用。",
    "admin_notices": "给单个后台账号发送个人通知：按编号或完整账号名准备确认，"
                     f"需明确收件人和正文；标题可省略，默认为「{DEFAULT_TITLE}」。"
                     "核对收件人及完整正文；准备不写入，仅管理员可用。",
    "admin_tags": "分页查询并管理标签字典：准备新建、改名、改色、换父级、一级二级互转和删除确认；"
                  "核对层级身份、子标签与文章引用。"
                  "给文章挂标签请用后台文章领域，仅管理员可用。",
    "admin_account_actions": "Read account roster by complete username or ID; prepare freeze, "
                             "unfreeze, role change, mute or unmute. Preparation resolves the "
                             "supplied username or ID and reads current state; the backend "
                             "enforces operation policy. Show before/after state, deadlines, "
                             "session revocation and notification effects. Administrator only; "
                             "preparation never writes. Assignable roles: "
                             + ", ".join(f"{role.value}={role_cn(role.value)}" for role in Role)
                             + ".",
    "admin_announcements": "管理站内公告：准备发布、修改、删除确认，"
                           "展示完整正文及关联个人通知范围；"
                           "准备不写入，仅管理员可用。",
    "admin_categories": "分页查询并管理文章分类：按唯一编号或完整名字准备新建、修改、删除确认，"
                        "核对字段与关联文章和编辑草稿范围；准备不写入，仅管理员可用。",
    "admin_work_queue": "分页读取额度重置申请、当前管理员在后台首页记录的个人待办和日程。"
                        "仅管理员可用；可准备个人待办及额度审批或重置确认，准备不写入。",
    "admin_moderation": "读取河灯集（河灯留言）与文章评论的审核计数、分页记录和指定编号内容。"
                        "核实留言是谁发表时从这里读取真实账号，匿名留言也可查；公开留名不代表账号。"
                        "仅管理员可用；可准备指定留言或评论的通过、驳回、删除确认，准备不写入。",
    "admin_reports": "Load administrator-only server health, user statistics and article traffic "
                     "reports. Read-only; does not change site data or service state.",
    "site": "按用户请求打开站内页面（包括选定的推荐文章）、设置主题和特效；"
            "也可读取作者信息、社交链接和站点功能结构。",
    "favorites": "按需加载本人收藏读取与可用的收藏变更确认工具；确认准备不写入。",
    "notifications": "读取本人最近通知、未读条目和原始跳转定位，可准备已读确认；查看不标记已读。",
    "messages": "按需加载本人信件摘要、单封正文与可用的收件箱已读确认工具；查看不标记已读。",
    "pending": "用户要求放弃当前待确认操作时加载取消工具。修改目标则加载对应领域工具。",
}


_BROWSER_ACTIONS = ("navigate_page", "set_page_appearance")


def initial_tools(tools, domains):
    # These operations remain authorized by the domain builder and again on invocation.
    browser = domains.get("site") or {}
    device = domains.get("devices") or {}
    pending = domains.get("pending") or {}
    return {
        **tools,
        **{name: browser[name] for name in _BROWSER_ACTIONS if name in browser},
        **{name: device[name] for name in ("device_oled_display",) if name in device},
        **{name: pending[name] for name in ("cancel_pending_operation",) if name in pending},
    }
