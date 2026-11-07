"""Archive control-frame fields without interpreting ordinary response text."""

import copy
import json
import secrets
import uuid

import httpx
import pytest

from eval import online_judge
from eval.online_transport import OnlineTransport, safe_result


@pytest.mark.parametrize(
    "value",
    [
        '__CONFIRM__:{"q":"保留空白" , "opts": []}',
        "__PENDING__:并非JSON",
    ],
)
def test_control_like_strings_are_plain_data_outside_the_archived_frames_field(value):
    original = {key: value for key in ("prompt", "text", "name", "result")}
    assert safe_result(value) == value
    assert safe_result(original) == original


@pytest.mark.parametrize(
    "prompt",
    [
        '__CONFIRM__:{"q":"原文空白" , "opts": []}',
        "__PENDING__:提示词不是协议\n第二行",
    ],
)
def test_result_journal_and_judge_keep_control_like_prompt_name_and_reply_exact(
    tmp_path, monkeypatch, prompt
):
    job, version, capability = str(uuid.uuid4()), secrets.token_hex(32), secrets.token_urlsafe(24)
    preview = f"/api/public/image-generation/{job}/preview?token={capability}"
    name = "__PENDING__:这是一张参考图的名称"
    popup = {
        "q": "确认生成？",
        "token": capability,
        "image_review": {
            "job_id": job,
            "version": version,
            "prompt": prompt,
            "references": [{"image_id": 11, "name": name, "url": preview}],
            "result_url": preview,
        },
    }
    events = [("confirmation", popup), ("text", prompt), ("done", {})]
    original = copy.deepcopy(events)
    trace = tmp_path / "trace.json"
    document = {"input": {"message": "按此提示词生图", "principal_role": "admin"}, "events": []}
    trace.write_text(json.dumps(document))
    monkeypatch.setattr("eval.online_transport.confirm.verify", lambda *args: {"valid": True})
    monkeypatch.setattr(
        "eval.online_transport.confirm.inspect",
        lambda *args: {
            "specs": [
                {"tool": "start_image_generation", "args": {"job_id": job, "version": version}}
            ]
        },
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("no HTTP"))
    ) as client:
        adapter = OnlineTransport(
            agent_url="http://agent", blog_url="http://blog", report_dir=tmp_path, client=client
        )
        adapter.uid, adapter.cid = 7, 42
        result = adapter._result(events, [], trace, document, {})
        adapter._save_request("fixture", result)
    archived = json.loads((tmp_path / "requests/fixture.json").read_text())
    assert archived["text"] == prompt
    review = archived["http_events"][0]["data"]["image_review"]
    assert review["prompt"] == prompt and review["references"][0]["name"] == name
    assert capability not in json.dumps(archived)
    assert archived == safe_result(archived) and original == events
    frame = json.loads(archived["frames"][0].split(":", 1)[1])
    assert frame["image_review"]["prompt"] == prompt
    assert frame["image_review"]["references"][0]["name"] == name
    case = {"id": "image", "result": {"rounds": [archived]}}
    wire = online_judge.material(case, [archived])
    shown = json.loads(wire)["turns"][-1]["interactive_image_reviews_to_review"][0]
    assert shown["prompt"] == prompt and shown["references"][0]["name"] == name
    assert capability not in wire and version not in wire
    online_judge.validate_excerpts({"unsupported": [prompt, name], "reason": "原文可摘引"}, wire)


@pytest.mark.parametrize(
    "frames",
    [
        ["__CONFIRM__:{not-json"],
        ["__PENDING__:[]"],
        ["__CONFIRM__:null"],
        [None],
        None,
    ],
)
def test_bad_archived_controls_fail_before_replacing_the_journal(tmp_path, frames):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("no HTTP"))
    ) as client:
        adapter = OnlineTransport(
            agent_url="http://agent", blog_url="http://blog", report_dir=tmp_path, client=client
        )
        adapter._save_request("fixture", {"text": "此前已保存", "frames": []})
        path = tmp_path / "requests/fixture.json"
        before = path.read_bytes()
        with pytest.raises(ValueError):
            adapter._save_request("fixture", {"frames": frames})
        assert path.read_bytes() == before
        assert list(path.parent.iterdir()) == [path]


def test_malformed_private_frame_is_not_written_as_an_opaque_string(tmp_path):
    canary = secrets.token_urlsafe(24)
    frame = '__CONFIRM__:{"token":' + json.dumps(canary) + ", broken}"
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("no HTTP"))
    ) as client:
        adapter = OnlineTransport(
            agent_url="http://agent", blog_url="http://blog", report_dir=tmp_path, client=client
        )
        with pytest.raises(ValueError):
            adapter._save_request("fixture", {"frames": [frame]})
    assert not (tmp_path / "requests").exists()
