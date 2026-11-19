"""Focused semantic review checks important claims independently."""

import json

import pytest

from eval import online_judge, semantic_judge


def test_compact_tool_catalogs_preserve_each_menu_and_contract():
    a = {"name": "read", "description": "original"}
    b = {"name": "read", "description": "updated"}
    c = {"name": "prepare"}
    catalogs = [[a, c], [b, c], [c]]
    compact = online_judge._intern_tool_catalogs({"tool_catalogs": catalogs})
    pool = compact["tool_contract_pool"]
    assert [[pool[i] for i in menu] for menu in compact["tool_catalogs"]] == catalogs
    assert len(pool) == 3


@pytest.mark.parametrize("scope", ["article_sources", "account_authority"])
def test_focused_review_catches_claim_missed_by_general_review(monkeypatch, scope):
    answer = "已回答请求。无依据的补充结论。"
    turn = {"user_request": "原请求", "assistant_reply_to_review": answer,
            "runtime_context": {"available_tools": ["unrelated_tool"]}}
    data = {"turns": [turn]}
    if scope == "article_sources":
        turn["runtime_context"]["article_provenance"] = {"body_reads": [{"id": 22}, {"id": 23}]}
    else:
        data["case_tags"] = ["identity"]
        turn["runtime_context"]["product_facts"] = {
            "account_roles": {"assign_admin_requires": "superadmin"}}

    def judge(text, _llm, *, instructions, diagnostics):
        if instructions == online_judge.INSTRUCTIONS:
            return {"unsupported": [], "answered": True, "verdict": "ok", "reason": "初审通过"}
        assert scope in json.loads(text)
        assert "unrelated_tool" not in text
        assert instructions == online_judge.FOCUSED_CLAIMS
        diagnostics["reviewed"] = True
        return {"unsupported": ["无依据的补充结论"], "answered": True,
                "verdict": "suspect", "reason": "证据不支持补充结论"}

    monkeypatch.setattr(semantic_judge, "judge_one", judge)
    diagnostics = {}
    verdict = online_judge.semantic_review(json.dumps(data), None, diagnostics=diagnostics)
    assert verdict["unsupported"] == ["无依据的补充结论"]
    assert diagnostics == {"focused_claims": {"reviewed": True}}
