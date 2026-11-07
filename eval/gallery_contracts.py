"""Isolated gallery tasks with known complete selections and accepted image jobs."""

import json
import re
import time
from contextlib import contextmanager
from unittest.mock import patch

import httpx

from agent import confirm
from eval.own_fixtures import SECRET
from tools import base

JOB_ID = "6b9e2bf1-56af-428c-8177-e3ad0641e036"
ORGANIZE_REQUEST = ("帮我整理一下相册：先查清所有未分类图片，全部移到“文章封面”目录，"
                    "原有目录里的图片不动。先给我一张整批确认卡，不执行。")


@contextmanager
def backend():
    images = [{"imageKey": ident, "imageUrl": f"https://images.test/{ident}.png",
               "displayName": f"候选封面{ident}", "description": "公开练习封面",
               "folderId": 0, "isFavorite": False, "version": "a" * 64}
              for ident in range(1, 48)]
    images.append({**images[-1], "imageKey": 72, "imageUrl": "https://images.test/accepted.png",
                   "displayName": "学院风", "folderId": 1})
    folder = {"id": 1, "name": "文章封面", "parentId": 0, "imageCount": 1}
    state = {"caller_role": "admin", "requests": [], "unsupported_reads": [],
             "expected_ids": list(range(1, 48))}

    def handle(request):
        state["requests"].append(request)
        path, params = request.url.path, request.url.params
        if request.method == "GET" and path.endswith("/gallery/images"):
            rows = sorted(images, key=lambda row: row["imageKey"], reverse=True)
            if "folderId" in params:
                rows = [row for row in rows if row["folderId"] == int(params["folderId"])]
            if "before" in params:
                rows = [row for row in rows if row["imageKey"] < int(params["before"])]
            limit = int(params.get("limit", "20"))
            selected = rows[:limit]
            value = {"items": selected,
                     "nextCursor": selected[-1]["imageKey"] if len(rows) > limit else None}
        elif request.method == "GET" and path.endswith("/gallery/folders"):
            value = [folder] if int(params.get("parentId", "0")) == 0 else []
        elif request.method == "GET" and path.endswith("/gallery/images/72"):
            value = images[-1]
        elif request.method == "GET" and path.endswith("/image-generation/jobs/" + JOB_ID):
            value = {"id": JOB_ID, "request": {"provider": "https://images.test/v1",
                "model": "synthetic", "prompt": "学院风封面", "size": "1024x1024",
                "quality": "auto", "references": []}, "requestHash": "a" * 64,
                "status": "accepted", "imageRemoved": False, "imageId": 72}
        elif request.method == "POST" and path.endswith("/gallery/image-actions/prepare"):
            intent = json.loads(request.content)
            ids = intent.get("imageIds", [])
            selected = sorted((row for row in images if row["imageKey"] in ids),
                              key=lambda row: row["imageKey"])
            value = {"version": "a" * 64, "expiresAt": int(time.time()) + 900,
                "intent": intent, "matchedIds": [row["imageKey"] for row in selected],
                "preview": [{"imageId": row["imageKey"], "displayName": row["displayName"],
                    "folderId": row["folderId"], "isFavorite": row["isFavorite"]}
                    for row in selected[:10]], "previewTruncated": len(selected) > 10,
                "destination": {key: folder[key] for key in ("id", "name", "parentId")}
                    if intent.get("folderId") == 1 else None}
        else:
            state["unsupported_reads"].append(path)
            return httpx.Response(404)
        return httpx.Response(200, json={"code": 200, "data": value})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, \
            patch.object(base, "_client", client), \
            patch.object(confirm.settings, "confirmation_secret", SECRET):
        yield state


def organize_unclassified(turn, state):
    result = turn(ORGANIZE_REQUEST)
    observed = {row["image_id"] for event in result.get("native_events", [])
                if event.get("event") == "call" and event.get("name") == "get_gallery_index"
                and event.get("kind") == "ok"
                for row in json.loads(event["result"])["items"]}
    if not set(state["expected_ids"]).issubset(observed):
        raise ValueError("gallery selection contains images that were never read")
    if len(result["confirm_tokens"]) != 1 or result["exec_rows"]:
        raise ValueError("expected one complete gallery preparation without execution")
    grant = confirm.verify(result["confirm_tokens"][0], 7, 42)
    specs = grant.get("specs", []) if grant else []
    expected = {"action": "move", "imageIds": state["expected_ids"], "folderId": 1}
    if (len(specs) != 1 or specs[0].get("tool") != "manage_gallery_images"
            or specs[0].get("args", {}).get("intent") != expected):
        raise ValueError("gallery preparation omitted unseen images or changed the requested scope")
    if any(request.method != "GET" and not request.url.path.endswith("/prepare")
           for request in state["requests"]):
        raise ValueError("gallery task crossed the preparation boundary")
    state["contract_evidence"] = {"business_api": "synthetic", "selected_images": 47,
                                  "business_writes": 0}


def show_accepted_image(turn, state):
    first = turn(f"只读核对生图任务 {JOB_ID} 的保存状态，不生成、不修改。")
    second = turn("把刚刚那张直接展示在对话里，不要分析画面。")
    if not any(request.method == "GET" and request.url.path.endswith(
            "/image-generation/jobs/" + JOB_ID) for request in state["requests"]):
        raise ValueError("accepted generation status was never read")
    if not re.search(r"!\[[^\]\n]*\]\(https://images\.test/accepted\.png\)", second["text"]):
        raise ValueError("accepted image reference was not displayed as its actual image")
    for result in (first, second):
        if result["confirm_tokens"] or result["exec_rows"]:
            raise ValueError("displaying an accepted image prepared or executed a mutation")
    if (any(request.method != "GET" for request in state["requests"])
            or any(request.url.path.endswith("/gallery/images") for request in state["requests"])):
        raise ValueError("accepted image display scanned the catalog or requested a paid action")


CASES = [("gallery_organize_unclassified", backend, organize_unclassified),
         ("gallery_show_accepted_followup", backend, show_accepted_image)]
