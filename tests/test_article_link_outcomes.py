"""Frozen reading requests accept prose punctuation without accepting another article."""

import json
from pathlib import Path

import pytest

from eval.golden_evaluation import check_gold

CASES = [json.loads(line) for line in
         (Path(__file__).parents[1] / "eval/golden/retrieval_requests.jsonl")
         .read_text().splitlines() if line.strip()]


def reply(text):
    return {"text": text, "commands": [], "tool_calls": [], "exec_rows": []}


@pytest.mark.parametrize("case_id,ident", [
    ("retrieval_whole_article_summary", 22), ("retrieval_short_reference_order", 23),
])
@pytest.mark.parametrize("ending", [
    "", " ", "\n", ")", "），读取完整。", "。", "?view=full", "#section",
])
def test_expected_article_accepts_markdown_and_prose_boundaries(case_id, ident, ending):
    case = next(row for row in CASES if row["id"] == case_id)
    gold = case.get("rounds", [{"gold": case.get("gold")}])[-1]["gold"]
    result = reply(f"来源：https://example.test/article/{ident}{ending}")
    assert not check_gold({"text_any_regex": gold["text_any_regex"]}, result)


@pytest.mark.parametrize("suffix", ["0", "s", "foo", "_draft", "-other"])
def test_other_article_paths_are_not_accepted(suffix):
    case = next(row for row in CASES if row["id"] == "retrieval_whole_article_summary")
    assert check_gold({"text_any_regex": case["gold"]["text_any_regex"]},
                      reply(f"来源：https://example.test/article/22{suffix}"))


def test_ambiguous_reference_accepts_multiline_disambiguation_only():
    case = next(row for row in CASES if row["id"] == "retrieval_short_reference_ambiguous")
    gold = {"text_any_regex": case["rounds"][1]["gold"]["text_any_regex"]}
    assert not check_gold(gold, reply("是文章22，\n还是文章23？"))
    assert check_gold(gold, reply("文章22的结论是……"))
