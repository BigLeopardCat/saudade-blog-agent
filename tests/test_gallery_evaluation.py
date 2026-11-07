"""Scripted models verify gallery evaluation wiring, never model intent quality."""

import pytest
from langchain_core.messages import AIMessage

from eval import gallery_contracts, native_contracts, online_baseline
from eval.golden_evaluation import check_gold


def call(name, **args):
    return {"name": name, "args": args, "id": name}


def script(monkeypatch, tmp_path, responses):
    prompts = []
    answers = iter(responses)

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, prompt, **kwargs):
            prompts.append(prompt)
            return next(answers)

    monkeypatch.setattr("agent.runtime.service.get_llm", lambda **_: Model())
    monkeypatch.setattr(native_contracts.golden_trace, "trace_root",
                        lambda: str(tmp_path / "traces"))
    return prompts


@pytest.mark.parametrize("limit,selected,passed",
                         [(50, 47, True), (50, 30, False), (30, 47, False)])
def test_large_library_contract_rejects_a_preparation_that_omits_the_unseen_tail(
        monkeypatch, tmp_path, limit, selected, passed):
    script(monkeypatch, tmp_path, [
        AIMessage(content="", tool_calls=[call("load_admin_gallery_info_tools")]),
        AIMessage(content="", tool_calls=[call("get_gallery_index", folder_id=0, limit=limit),
                                           call("list_gallery_folders")]),
        AIMessage(content="", tool_calls=[call("prepare_gallery_images", intent={
            "action": "move", "imageIds": list(range(1, selected + 1)), "folderId": 1})]),
        AIMessage(content="请核对整批移动到文章封面的确认卡。"),
    ])
    report = native_contracts.run(["gallery_organize_unclassified"])
    result = report["cases"][0]
    assert result["passed"] is passed
    assert result["business_writes"] == 0 and len(result["rounds"]) == 1
    assert result["rounds"][0]["confirmation_count"] == 1
    if passed:
        assert result["evidence"]["selected_images"] == 47
    else:
        assert ("omitted unseen images" if selected == 30 else "never read") in result["reason"]


@pytest.mark.parametrize("reply,passed", [
    ("![学院风](https://images.test/accepted.png)", True),
    ("![学院风](https://images.test/unrelated.png) https://images.test/accepted.png", False),
])
def test_accepted_job_followup_checks_actual_image_without_catalog_rescan(
        monkeypatch, tmp_path, reply, passed):
    prompts = script(monkeypatch, tmp_path, [
        AIMessage(content="", tool_calls=[call("load_admin_image_generation_info_tools")]),
        AIMessage(content="", tool_calls=[call("get_image_generation",
            job_id=gallery_contracts.JOB_ID, wait_seconds=0)]),
        AIMessage(content="生成结果已经存入图库，图片编号72。"),
        AIMessage(content="", tool_calls=[call("get_gallery_image_metadata", image_id=72)]),
        AIMessage(content=reply),
    ])
    result = native_contracts.run(["gallery_show_accepted_followup"])["cases"][0]
    assert result["passed"] is passed and result["business_writes"] == 0
    assert len(result["rounds"]) == 2
    assert '"imageId": 72' in str(prompts[3])
    assert result["rounds"][1]["confirmation_count"] == 0


def test_short_reference_golden_requires_rendering_without_forcing_another_read():
    case = next(row for row in online_baseline.load_suite()
                if row["id"] == "gallery_display_followup")
    gold = case["rounds"][1]["gold"]
    result = {"text": "![学院风](https://images.test/accepted.png)", "tool_calls": [],
              "exec_rows": [], "commands": [],
              "native_events": [{"node": "native", "event": "llm_start"}]}
    assert not check_gold(gold, result)
    assert check_gold(gold, {**result, "text": "图片链接在这里。"})
    repeated = [*result["native_events"], {"node": "native", "event": "tool_requested",
                 "name": "get_gallery_image_metadata"}]
    assert check_gold(gold, {**result, "native_events": repeated,
                                         "tool_calls": ["get_gallery_image_metadata"]})
