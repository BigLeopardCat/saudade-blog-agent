"""Build the blog product's authorized tool menus."""

from agent.domains import browser
from agent.domains.favorites import preparation as favorite_preparation
from agent.domains.messages import preparation as message_preparation
from agent.domains.notifications import preparation as notification_preparation
from agent.products.blog.admin_domains import domains as admin_domains
from agent.products.blog.catalog import DOMAIN_DESCRIPTIONS, initial_tools
from agent.products.blog.read_tools import (
    account_tools,
    admin_tools,
    article_tools,
    catalog_tools,
    device_domain,
    moderation_tools,
    own_domains,
    own_reads_enabled,
    own_tools,
    public_domains,
    queue_tools,
    site_tools,
)


class ToolCatalog:
    descriptions = DOMAIN_DESCRIPTIONS

    @staticmethod
    def promote_tools(tools, domains):
        return initial_tools(tools, domains)

    def __init__(self, read_tools=None, site_read_tools=None, own_read_tools=None,
                 admin_read_tools=None, moderation_read_tools=None, queue_read_tools=None,
                 account_read_tools=None):
        self.read_tools = article_tools() if read_tools is None else read_tools
        self.site_read_tools = site_tools() if site_read_tools is None else site_read_tools
        self.own_read_tools = own_tools() if own_read_tools is None else own_read_tools
        self.admin_read_tools = admin_tools() if admin_read_tools is None else admin_read_tools
        self.moderation_read_tools = (moderation_tools() if moderation_read_tools is None
                                      else moderation_read_tools)
        self.catalog_read_tools = catalog_tools()
        self.account_read_tools = (account_tools() if account_read_tools is None
                                   else account_read_tools)
        self.queue_read_tools = queue_tools() if queue_read_tools is None else queue_read_tools

    def initial_tools(self, own_enabled):
        tools = dict(self.read_tools)
        if own_enabled and "get_unread_counts" in self.own_read_tools:
            tools["get_unread_counts"] = self.own_read_tools["get_unread_counts"]
        return tools

    def domains(self, config, can_prepare):
        cfg = (config or {}).get("configurable") or {}
        domains = {"site": {**self.site_read_tools, **browser.tools(config)},
                   **public_domains(), **device_domain(config)}
        domains.update(admin_domains(config, can_prepare, {
            "admin_reports": self.admin_read_tools,
            "admin_moderation": self.moderation_read_tools,
            "admin_catalog": self.catalog_read_tools,
            "admin_account_actions": self.account_read_tools,
            "admin_work_queue": self.queue_read_tools,
        }))
        if own_reads_enabled(cfg.get("principal")):
            domains.update(own_domains(self.own_read_tools))
        if can_prepare and favorite_preparation.available(config):
            domains["favorites"] = favorite_preparation.tools(self.own_read_tools)
            domains["notifications"] = notification_preparation.tools(self.own_read_tools)
            domains["messages"] = message_preparation.tools(self.own_read_tools)
        return domains

