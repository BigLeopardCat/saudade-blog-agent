"""Explicit assembly of the blog product's tools, context and middleware."""

from agent.domains import conversation_images
from agent.domains.registry import confirmed_domain, native_domain
from agent.principal import CHAT_ONLY_ROLES
from agent.products.blog.articles import ArticleMiddleware
from agent.products.blog.capabilities import ToolCatalog
from agent.products.blog.catalog import DOMAIN_DESCRIPTIONS, initial_tools
from agent.products.blog.context import conversation_context, conversation_session
from agent.products.blog.history import tool_access
from agent.products.blog.pending import expired
from agent.products.blog.pending import restore as restore_pending
from agent.products.blog.prompt import build_prompt
from agent.products.blog.read_tools import own_reads_enabled
from agent.products.blog.tool_policy import BlogToolMiddleware
from agent.runtime import memory


class BlogApplication:
    middleware = (ArticleMiddleware, BlogToolMiddleware)
    descriptions = DOMAIN_DESCRIPTIONS
    context = staticmethod(conversation_context)
    session_key = staticmethod(conversation_session)
    pending = staticmethod(restore_pending)
    expired = staticmethod(expired)
    history_access = staticmethod(tool_access)

    def __init__(self, catalog=None):
        self.catalog = catalog if catalog is not None else ToolCatalog()

    def prompt(self, messages, principal):
        return build_prompt(messages, principal, own_reads_enabled(principal))

    def tools(self, config, snapshot, pending, saver, available):
        principal = ((config or {}).get("configurable") or {}).get("principal")
        if getattr(principal, "known_role", None) in CHAT_ONLY_ROLES:
            return {}, {}
        can_prepare = available
        tools = self.catalog.initial_tools(own_reads_enabled(principal))
        domains = self.catalog.domains(config, can_prepare)
        visible = memory.history(snapshot.messages, principal, tool_access=self.history_access)
        images = conversation_images.tools(visible, config)
        if images:
            domains = {**domains, "conversation_images": images}
        return initial_tools(tools, domains), domains

    @staticmethod
    def confirmation(grant):
        domain = confirmed_domain(grant)
        if domain is None or native_domain(grant) is not domain:
            raise ValueError("这张确认卡不支持当前执行入口，请重新发起操作；本次未执行。")
        return domain
