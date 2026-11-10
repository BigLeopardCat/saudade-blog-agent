"""Production sticker failures require actual supported, complete shortcodes."""

import re

from agent.stickers import STICKER_NAMES
from eval.online_baseline import load_suite


def rule(name):
    return next(case["gold"]["text_any_regex"][0] for case in load_suite() if case["id"] == name)


def test_all_stickers_rejects_missing_colons_unknown_names_and_code_examples():
    check = rule("p4_sticker_catalog")
    valid = " ".join(f":{name}:" for name in STICKER_NAMES)
    assert re.search(check, valid)
    assert re.search(check, ":全部表情:")
    assert re.search(check, "![全部表情](:全部表情:)")
    assert not re.search(check, ":全部表情::汗:")
    assert not re.search(check, "`:全部表情:`")
    for invalid in (valid.replace(":贴贴:", "贴贴:"), valid.replace(":贴贴:", ":摊手:"),
                    valid.replace(":贴贴:", ""), "```\n" + valid + "\n```"):
        assert not re.search(check, invalid)


def test_requested_selection_requires_all_six_without_other_stickers():
    check = rule("p4_sticker_selected")
    valid = ":贴贴::比心:\n:得意: :略略: :喝茶: :晚安:"
    assert re.search(check, valid)
    assert re.search(check, " ".join(f"![{name}](:{name}:)" for name in
                                    ("贴贴", "比心", "得意", "略略", "喝茶", "晚安")))
    assert not re.search(check, ":全部表情:")
    assert not re.search(check, valid + ":头疼:")
    assert not re.search(check, valid.replace(":略略:", ":得意:"))


def test_praise_cannot_pass_with_an_invented_sticker_name():
    check = rule("persona_praise")
    assert re.search(check, "谢谢你 :比心:")
    assert not re.search(check, "谢谢你 :摊手:")
