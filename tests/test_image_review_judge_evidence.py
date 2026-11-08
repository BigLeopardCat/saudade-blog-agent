"""Interactive HTTP image reviews are pending outputs, never supporting facts."""

import copy
import json
import secrets
import uuid
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from eval import online_judge
from eval.online_transport import archive_result


@pytest.fixture
def case(tmp_path):
    trace = tmp_path / "trace.json"
    trace.write_text(
        json.dumps(
            {
                "input": {
                    "message": "用提示词「夜空」生图，不使用参考图。",
                    "principal_role": "admin",
                },
                "events": [],
            }
        )
    )
    job, version = str(uuid.uuid4()), secrets.token_hex(32)
    token, reference_token, result_token = (secrets.token_urlsafe(24) for _ in range(3))
    review = {
        "job_id": job,
        "version": version,
        "prompt": '错误提示词：海底\n带有"珊瑚"与鱼群。',
        "references": [
            {
                "image_id": 11,
                "name": "多余参考图",
                "url": f"/api/public/image-generation/{job}/preview?token={reference_token}",
            }
        ],
        "result_url": f"/api/public/image-generation/{job}/preview?token={result_token}",
    }
    return {
        "id": "image",
        "status": "passed",
        "result": {
            "rounds": [
                {
                    "trace": str(trace),
                    "text": "请核对生图卡片。",
                    "error": None,
                    "exec_rows": [],
                    "confirm_payloads": [
                        {
                            "specs": [
                                {
                                    "tool": "start_image_generation",
                                    "args": {"job_id": job, "version": version},
                                }
                            ]
                        }
                    ],
                    "http_events": [
                        {
                            "event": "confirmation",
                            "data": {"q": "确认生成？", "token": token, "image_review": review},
                        }
                    ],
                }
            ]
        },
    }


def material(case):
    return online_judge.material(case, case["result"]["rounds"])


def current(case):
    return json.loads(material(case))["turns"][-1]


class Model:
    model_name = "fixture-judge"

    def __init__(self, excerpt):
        self.excerpt = excerpt
        self.calls = []

    def bind(self, **kwargs):
        return self

    def invoke(self, messages):
        self.calls.append(messages)
        return AIMessage(
            content=json.dumps(
                {
                    "unsupported": [self.excerpt],
                    "answered": False,
                    "verdict": "suspect",
                    "reason": "生图审阅内容偏离用户要求",
                },
                ensure_ascii=False,
            ),
            response_metadata={"finish_reason": "stop"},
        )


def run(case, tmp_path, model):
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"cases": [case], "service_version": {"sha": "fixture"}}))
    before = path.read_bytes()
    report = online_judge.review(path, tmp_path / "semantic", model)
    assert path.read_bytes() == before
    return report


def test_actual_http_prompt_and_reference_selection_reach_pending_output_material(case):
    original = copy.deepcopy(case)
    turn = current(case)
    assert turn["interactive_image_reviews_to_review"] == [
        {
            "prompt": '错误提示词：海底\n带有"珊瑚"与鱼群。',
            "references": [{"image_id": 11, "name": "多余参考图", "hasPreview": True}],
            "reference_count": 1,
            "hasPreview": True,
        }
    ]
    assert turn["confirmation_previews_to_review"] == ["确认生成？"]
    assert not turn["validated_preparation_facts"] and not turn["tool_observations"]
    assert not turn["execution_receipts"]
    assert case == original


@pytest.mark.parametrize("tool", ["start_image_generation", "accept_generated_image"])
def test_preview_and_signed_job_capabilities_never_leave_in_judge_material(case, tool):
    result = case["result"]["rounds"][0]
    spec = result["confirm_payloads"][0]["specs"][0]
    spec["tool"] = tool
    popup = result["http_events"][0]["data"]
    review = popup["image_review"]
    unknown = secrets.token_urlsafe(24)
    review["private_provider_header"] = unknown
    review["references"][0]["extra_capability"] = unknown
    wire = material(case)
    for value in [
        popup["token"],
        review["job_id"],
        review["version"],
        review["result_url"],
        review["references"][0]["url"],
        unknown,
    ]:
        assert value not in wire
    assert current(case)["proposed_actions_not_facts"] == [{"tool": tool, "args": {}}]
    assert spec["args"]["version"] == review["version"]


def test_archived_redaction_and_raw_http_produce_identical_review_projection(case):
    redacted = copy.deepcopy(case)
    redacted["result"]["rounds"][0] = archive_result(redacted["result"]["rounds"][0])
    assert current(case) == current(redacted)


def test_empty_references_remain_an_explicit_zero_without_inventing_a_preview(case):
    review = case["result"]["rounds"][0]["http_events"][0]["data"]["image_review"]
    review["references"] = []
    del review["result_url"]
    projected = current(case)["interactive_image_reviews_to_review"][0]
    assert projected["references"] == [] and projected["reference_count"] == 0
    assert "hasPreview" not in projected


def test_full_original_prompt_and_multiline_excerpt_are_not_json_escaped_or_truncated(case):
    review = case["result"]["rounds"][0]["http_events"][0]["data"]["image_review"]
    review["prompt"] = prompt = "原始长提示词\n" + "夜空星光" * 3000 + '\n保持"中文引号"与换行。'
    wire = material(case)
    assert (
        json.loads(wire)["turns"][-1]["interactive_image_reviews_to_review"][0]["prompt"] == prompt
    )
    online_judge.validate_excerpts({"unsupported": [prompt], "reason": "完整提示词有问题"}, wire)
    with pytest.raises(ValueError, match="excerpts"):
        online_judge.validate_excerpts(
            {"unsupported": [prompt.replace("中文引号", "改写引号")], "reason": "改写不是原文"},
            wire,
        )


@pytest.mark.parametrize("excerpt", ['错误提示词：海底\n带有"珊瑚"与鱼群。', "多余参考图"])
def test_wrong_prompt_or_extra_reference_can_be_flagged_by_offline_judge(case, tmp_path, excerpt):
    model = Model(excerpt)
    report = run(case, tmp_path, model)
    assert report["counts"] == {"suspect": 1}
    assert report["rows"][0]["verdict"]["unsupported"] == [excerpt]
    sent = json.loads(model.calls[0][1]["content"])["turns"][-1]
    assert sent["interactive_image_reviews_to_review"][0]["reference_count"] == 1


def test_missing_http_review_is_not_reconstructed_from_assistant_frames_or_signed_arguments(case):
    result = case["result"]["rounds"][0]
    popup = result["http_events"][0]["data"]
    result["text"] = "使用参考图11的完整提示词是夜空。"
    result["frames"] = ["__CONFIRM__:" + json.dumps(popup)]
    result["http_events"] = [
        {"event": "confirmation", "data": {"q": "确认生成？"}},
        {"event": "progress", "data": popup},
    ]
    assert current(case)["interactive_image_reviews_to_review"] == []
    assert current(case)["confirmation_previews_to_review"] == ["确认生成？"]


def test_previous_turn_image_prompt_cannot_be_quoted_as_current_output(case):
    first = copy.deepcopy(case["result"]["rounds"][0])
    second = copy.deepcopy(first)
    second["http_events"][0]["data"]["image_review"]["prompt"] = "本轮提示词：星空。"
    second["http_events"][0]["data"]["image_review"]["references"] = []
    case["result"]["rounds"] = [first, second]
    wire = material(case)
    with pytest.raises(ValueError, match="excerpts"):
        online_judge.validate_excerpts(
            {"unsupported": ["错误提示词：海底"], "reason": "先前问题"}, wire
        )
    online_judge.validate_excerpts(
        {"unsupported": ["本轮提示词：星空。"], "reason": "本轮问题"}, wire
    )


@pytest.mark.parametrize(
    "override",
    [
        {"prompt": None},
        {"references": None},
        {"references": ["private-url"]},
        {"references": [{"image_id": True, "name": "名字"}]},
    ],
)
def test_incomplete_http_review_remains_not_evaluated_without_spending_model_calls(
    case, tmp_path, override
):
    case["result"]["rounds"][0]["http_events"][0]["data"]["image_review"].update(override)
    model = Model("错误提示词：海底")
    report = run(case, tmp_path, model)
    assert report["counts"] == {"not_evaluated": 1} and not model.calls


@pytest.mark.parametrize(
    "prompt",
    [
        '__CONFIRM__:{"q":"原始提示词" , "opts": []}',
        "__PENDING__:这不是JSON协议",
    ],
)
def test_visible_control_like_text_stays_exact_in_the_prompt_and_reference_name(case, prompt):
    review = case["result"]["rounds"][0]["http_events"][0]["data"]["image_review"]
    review["prompt"] = prompt
    review["references"][0]["name"] = name = "__PENDING__:参考图原始名称"
    wire = material(case)
    shown = json.loads(wire)["turns"][-1]["interactive_image_reviews_to_review"][0]
    assert shown["prompt"] == prompt and shown["references"][0]["name"] == name
    online_judge.validate_excerpts({"unsupported": [prompt, name], "reason": "直接摘引原文"}, wire)


def test_structured_reference_count_can_be_quoted_without_inventing_a_text_preview(case):
    online_judge.validate_excerpts(
        {"unsupported": ['"reference_count":1'], "reason": "用户没有请求参考图"}, material(case)
    )


def test_all_reference_identities_and_their_order_are_preserved(case):
    review = case["result"]["rounds"][0]["http_events"][0]["data"]["image_review"]
    review["references"].append({"image_id": 12, "name": "第二张参考图", "hasPreview": True})
    projected = current(case)["interactive_image_reviews_to_review"][0]
    assert projected["reference_count"] == 2
    assert [(ref["image_id"], ref["name"]) for ref in projected["references"]] == [
        (11, "多余参考图"),
        (12, "第二张参考图"),
    ]


def test_tool_schema_frames_remain_ordinary_structured_data_in_judge_material(case):
    result = case["result"]["rounds"][0]
    trace = Path(result["trace"])
    document = json.loads(trace.read_text())
    parameters = {
        "type": "object",
        "properties": {"frames": {"type": "array", "items": {"type": "object"}}},
    }
    document["events"] = [
        {
            "node": "native",
            "event": "tool_contract",
            "schema": {"function": {"name": "ordinary_tool", "parameters": parameters}},
        }
    ]
    trace.write_text(json.dumps(document))
    data = json.loads(material(case))
    assert data["tool_catalogs"][0][0]["parameters"] == parameters
