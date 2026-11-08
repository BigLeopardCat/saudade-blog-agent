"""Private preview capabilities never enter captured evaluator or judge material."""

import copy
import json
import secrets
from types import SimpleNamespace

from eval.online_transport import OnlineTransport, archive_result, safe_result


def material():
    review = {"job_id": "6b9e2bf1-56af-428c-8177-e3ad0641e036", "version": "a" * 64,
              "prompt": "完整提示词，参考文章 https://example.test/articles/7?query=token",
              "result_url": "/api/public/image-generation/job/preview?token=result-secret",
              "references": [{"image_id": 11, "name": "原图",
                              "url": "/api/public/image-generation/job/preview?token=ref-secret"}]}
    popup = {"q": "审阅实际结果", "token": secrets.token_urlsafe(24), "image_review": review}
    return {"frames": ["__CONFIRM__:" + json.dumps(popup),
                       "__PENDING__:" + json.dumps({"payload": {"image_review": review}})],
            "http_events": [{"event": "confirmation", "data": popup}],
            "article_url": "https://example.test/articles/7?token=public-reference"}


def test_artifact_redaction_preserves_noncredential_facts_and_original_evidence():
    raw = material()
    original = copy.deepcopy(raw)
    safe = archive_result(raw)
    text = json.dumps(safe)
    assert "result-secret" not in text and "ref-secret" not in text
    assert raw["http_events"][0]["data"]["token"] not in text
    assert raw == original and archive_result(safe) == safe
    review = safe["http_events"][0]["data"]["image_review"]
    assert review["hasPreview"] is True and review["references"][0]["hasPreview"] is True
    assert review["references"][0]["image_id"] == 11 and review["version"] == "a" * 64
    assert "完整提示词" in review["prompt"] and "query=token" in review["prompt"]
    assert safe["article_url"] == original["article_url"]
    for frame in safe["frames"]:
        assert "image_review" in frame and "hasPreview" in frame


def test_request_journal_reuses_same_redaction_without_altering_raw_event(tmp_path):
    raw = material()
    OnlineTransport._save_request(SimpleNamespace(report_dir=tmp_path), "fixture", raw)
    saved = json.loads((tmp_path / "requests/fixture.json").read_text())
    assert saved == archive_result(raw)
    assert "result-secret" in json.dumps(raw) and "result-secret" not in json.dumps(saved)


def test_malformed_display_artifact_can_still_be_archived_without_inventing_a_preview():
    raw = {"image_review": {"references": None, "result_url": {"invalid": True}}}
    safe = safe_result(raw)
    assert safe == {"image_review": {"references": None, "hasPreview": False}}
