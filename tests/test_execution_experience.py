"""User-visible discovery evidence, tool visibility and concise operation previews."""


from agent.domains.article_tags.preparation import change_summary
from agent.domains.articles.evidence import ArticleEvidence
from agent.products.blog.catalog import initial_tools
from tools.result import ok


def test_discovery_allows_real_candidate_links_without_forcing_a_body_read():
    evidence = ArticleEvidence()
    evidence.observe(
        "rag_search", {}, ok("candidate", data=[{"type": "note", "id": 22, "title": "Topic"}])
    )
    assert evidence.issue("[Topic](/article/22)", "https://example.invalid") is None
    assert evidence.issue("[invented](/article/99)", "https://example.invalid")
    assert evidence.issue("https://forged.invalid/article/22", "https://example.invalid")
    assert not evidence.documents


def test_device_action_is_visible_only_when_authorized_catalog_contains_it():
    action = object()
    assert initial_tools({}, {"devices": {"device_oled_display": action}}) == {
        "device_oled_display": action
    }
    assert initial_tools({}, {"devices": {}}) == {}


def test_tag_card_describes_only_the_change_without_internal_ids():
    def tag(ident, name):
        return {"id": ident, "name": name, "level": 1, "parentId": None, "color": None}

    plan = {
        "title": "我，管理员！",
        "before": [tag(46, "测试"), tag(47, "权限")],
        "after": [tag(46, "测试"), tag(47, "权限"), tag(None, "泠月喵")],
    }
    text = change_summary(plan)
    assert "添加标签：「泠月喵」" in text and "将新建一级标签：「泠月喵」" in text
    assert "#46" not in text and "权限" not in text and "→" not in text
