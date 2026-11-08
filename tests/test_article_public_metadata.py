"""Public display metadata stays typed, visible to the model and tied to read evidence."""

import json
from pathlib import Path

import pytest
from _memory_support import Capture, config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent.domains.articles.evidence import ArticleEvidence
from agent.products.blog.application import BlogApplication
from agent.products.blog.capabilities import ToolCatalog
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.service import AgentRuntime
from eval import native_assertions
from eval.navigation_contracts import backend
from rag.reading import render_article_result
from rag.sections import slim_frame
from tools.base import SITE_BASE
from tools.notes import get_article_detail
from tools.result import ok
from utils import trace
from utils.trace_io import load_trace

DISPLAY = {
    "authorName": "公开作者",
    "authorNameTruncated": False,
    "authorAvatar": "/avatar.png",
    "authorAvatarTruncated": False,
    "description": '既有摘要，含 "引号"。',
    "descriptionTruncated": False,
    "cover": "/cover.png",
    "coverTruncated": False,
    "coverFit": "contain",
    "coverFocusX": 0.25,
    "coverFocusY": 0.75,
    "coverZoom": 1.5,
}


def rows(**fields):
    return [
        {
            "id": 23,
            "title": "元信息文章",
            "status": "public",
            "isPublic": True,
            "tags": [],
            **DISPLAY,
            **fields,
        }
    ]


def runtime(model):
    return AgentRuntime(
        CheckpointConversationStore(InMemorySaver()),
        application=BlogApplication(
            ToolCatalog(read_tools={get_article_detail.name: get_article_detail})
        ),
        model_factory=lambda **_: model,
    )


@pytest.mark.parametrize("view", ["body", "metadata"])
def test_model_tool_message_has_public_display_fields_without_identity_or_extra_http(tmp_path, view):
    reply = json.dumps(
        {
            "authorName": DISPLAY["authorName"],
            "description": DISPLAY["description"],
            "authorNameTruncated": False,
            "descriptionTruncated": False,
            "source": "/article/23",
        },
        ensure_ascii=False,
    )
    model = Capture(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "get_article_detail",
                    "id": "read",
                    "args": {"article_id": 23, "max_chars": 1, "view": view},
                }
            ],
        ),
        AIMessage(content=reply),
    )
    trace.start_trace(
        "metadata-read", 7, "fixture", dir=str(tmp_path), name="metadata-read", by_day=False
    )
    try:
        with backend(
            rows(userId=7, username="private-account", email="private@example.test")
        ) as state:
            result = list(
                runtime(model).stream_events(
                    {
                        "messages": [
                            HumanMessage(content="按JSON返回公开作者与现有摘要，不改写摘要。")
                        ]
                    },
                    config(),
                )
            )[-1]
    finally:
        path = trace.finish_trace("metadata-read", "producer_done", 0.0, 2)
    message = next(m for m in model.prompts[-1] if isinstance(m, ToolMessage))
    shown = json.loads(message.content)
    for key, value in DISPLAY.items():
        assert shown[key] == value
        assert message.artifact["data"][key] == value
    assert result.reply == reply
    assert len(state["requests"]) == 1 and not state["unsupported_reads"]
    assert state["requests"][0].url.path == "/api/public/documents/note/23/read"
    assert not {"userId", "username", "email"}.intersection(shown)
    if view == "metadata":
        assert shown["source"] is None and "noteContent" not in shown
        assert "limit" not in state["requests"][0].url.params
        proof = ArticleEvidence()
        proof.observe("get_article_detail", {"article_id": 23}, ok("", data=shown))
        assert not proof.documents and 23 in proof.candidates
        text, visible = render_article_result(shown, "", 40)
        assert visible is None and not text.startswith("{")
    else:
        assert shown["source"]["offset"] == 0 and shown["source"]["end"] == 1
    assert not shown["readComplete"]
    case = next(
        json.loads(line)
        for line in (Path(__file__).resolve().parents[1] / "eval/golden/native_requests.jsonl")
        .read_text()
        .splitlines()
        if json.loads(line)["id"] == "article_public_metadata"
    )
    result_case = {
        "text": result.reply,
        "native_events": load_trace(path)["events"],
        "exec_rows": [],
    }
    assert native_assertions.check(case["gold"], result_case) == []
    assert native_assertions.check(
        case["gold"],
        {**result_case, "text": json.dumps({**json.loads(reply), "authorName": "编造作者"})},
    )


def test_excerpt_keeps_metadata_and_does_not_drop_a_summary_identical_to_the_body():
    with backend(rows(description="这是一篇编程实践文章，介绍 元信息文章。")):
        data = get_article_detail.invoke({"article_id": 23}).data
    assert data["description"] == data["noteContent"]
    assert "description" in slim_frame(data)
    data["noteContent"] = "正文" * 1000
    data["totalChars"] = len(data["noteContent"])
    data["source"]["end"] = len(data["noteContent"])
    data["nextOffset"] = None
    data["readComplete"] = True
    text, visible = render_article_result(data, str(data), 1500)
    assert len(text) <= 1500 and json.loads(text) == visible
    assert visible["authorName"] == DISPLAY["authorName"]
    assert visible["description"] == data["description"]
    assert visible["coverFocusX"] == DISPLAY["coverFocusX"]
    assert visible["descriptionTruncated"] is False
    assert visible["source"]["end"] == visible["nextOffset"] < data["source"]["end"]


@pytest.mark.parametrize(
    "fields",
    [
        {"authorName": "x" * 81},
        {"description": "x" * 2001},
        {"cover": "x" * 2049},
        {"authorAvatar": "x" * 2049},
        {"descriptionTruncated": 1},
        {"coverFocusX": True},
        {"coverZoom": 5},
        {"coverFit": "invented"},
    ],
)
def test_unverified_display_metadata_never_reaches_the_model(fields):
    with backend(rows(**fields)):
        result = get_article_detail.invoke({"article_id": 23})
    assert result.kind == "unavailable" and result.data is None


@pytest.mark.parametrize(
    "author,description", [("新作者", "新摘要"), (None, ""), ("另一位", '带 "引号"\n换行')]
)
def test_json_answer_is_compared_with_dynamic_tool_facts_not_a_fixed_author(author, description):
    facts = {"authorName": author, "description": description}
    calls = [
        {
            "name": "get_article_detail",
            "kind": "ok",
            "args": {"article_id": 23},
            "result": json.dumps(facts, ensure_ascii=False),
        }
    ]
    for field in facts:
        wanted = {
            "tools": ["get_article_detail"],
            "args": {"article_id": 23},
            "path": [field],
            "format": "json",
            "answer_path": [field],
        }
        assert native_assertions._answer_value(wanted, {"text": json.dumps(facts)}, calls)
        wrong = {**facts, field: "编造的内容"}
        assert not native_assertions._answer_value(wanted, {"text": json.dumps(wrong)}, calls)
        assert not native_assertions._answer_value(wanted, {"text": "unstructured text"}, calls)
        assert not native_assertions._answer_value(wanted, {"text": json.dumps(facts)}, [])
        assert not native_assertions._answer_value(
            {**wanted, "args": {"article_id": 99}}, {"text": json.dumps(facts)}, calls
        )


@pytest.mark.parametrize("url", ["/article/99", "https://foreign.invalid/article/23"])
def test_json_citations_keep_the_same_article_and_origin_checks(url):
    with backend(rows()):
        value = get_article_detail.invoke({"article_id": 23, "max_chars": 1})
    evidence = ArticleEvidence()
    evidence.observe("get_article_detail", {"article_id": 23}, ok(str(value), data=value.data))
    assert evidence.issue(json.dumps({"source": url}), SITE_BASE) == "unread_article_reference"


@pytest.mark.parametrize("body", ["", " ", "\n"])
def test_verified_empty_or_whitespace_range_authorizes_only_the_metadata_link(body):
    from agent.domains.articles.reading import project
    from eval.fixtures.document_reads import document_page

    page = document_page("note", 23, "元信息文章", body, {"limit": "1"}, DISPLAY)
    data = project(page, "note", 23, "", 0, 1, "", "")
    evidence = ArticleEvidence()
    evidence.observe("get_article_detail", {"article_id": 23}, ok("metadata", data=data))
    assert evidence.candidates == {23: "元信息文章"}
    assert not evidence.documents and not evidence.read_ranges
    assert evidence.issue(json.dumps({"source": "/article/23"}), SITE_BASE) is None
    assert (
        evidence.issue(json.dumps({"source": "/article/99"}), SITE_BASE)
        == "unread_article_reference"
    )
    assert evidence.issue(
        json.dumps({"source": "https://foreign.invalid/article/23"}), SITE_BASE
    ) == "unread_article_reference"


@pytest.mark.parametrize(
    "source",
    [None, {"type": "talk", "id": 23}, {"type": "note", "id": 99},
     {"type": "note", "id": True}, {"type": "note", "id": "23"}],
)
def test_empty_metadata_with_unverified_source_cannot_register_a_candidate(source):
    evidence = ArticleEvidence()
    evidence.observe("get_article_detail", {"article_id": 23}, ok("metadata", data={
        "noteTitle": "元信息文章", "noteContent": " ", "source": source,
    }))
    assert not evidence.candidates and not evidence.documents and not evidence.read_ranges
    assert evidence.invalid_references(json.dumps({"source": "/article/23"}), SITE_BASE)


def test_a_body_read_after_metadata_preserves_its_verified_visible_range():
    from agent.domains.articles.reading import project
    from eval.fixtures.document_reads import document_page

    evidence = ArticleEvidence()
    for body in (" ", "有正文"):
        page = document_page("note", 23, "元信息文章", body, {"limit": "1"}, DISPLAY)
        data = project(page, "note", 23, "", 0, 1, "", "")
        evidence.observe("get_article_detail", {"article_id": 23}, ok("read", data=data))
    assert evidence.candidates == evidence.documents == {23: "元信息文章"}
    assert evidence.read_ranges[23] == [{
        "revision": data["contentRevision"], "section_id": "", "start": 0, "end": 1,
        "visible_chars": 1, "selected_total_chars": 3, "selected_range_reaches_end": False,
    }]


def test_fenced_json_example_does_not_become_a_live_source_or_body_read():
    with backend(rows()):
        value = get_article_detail.invoke({"article_id": 23, "max_chars": 1})
    evidence = ArticleEvidence()
    evidence.observe("get_article_detail", {"article_id": 23}, value)
    before = evidence.snapshot()
    literal = '```json\n{"source":"/article/99"}\n```'
    output = evidence.sources(literal, SITE_BASE)
    assert output.startswith(literal)
    assert "/article/23" in output[len(literal) :]
    assert "/article/99" not in output[len(literal) :]
    assert evidence.snapshot() == before and 99 not in evidence.documents
