"""Synthetic notification contents and anonymous-author lookup, with no writes."""

from contextlib import contextmanager
from unittest.mock import patch

import httpx

from agent import confirm
from eval.own_fixtures import SECRET
from tools import base


@contextmanager
def backend(role):
    state = {"caller_role": role, "requests": [], "unsupported_reads": []}
    notifications = [
        {"id": 101, "title": "合成部署完成", "content": "本次部署已完成", "isRead": False},
        {"id": 102, "title": "合成评论待审", "content": "有评论等待审核", "isRead": False},
    ]
    for row in notifications:
        row.update(type="system", link=None, createdAt="2026-10-26T12:00:00")

    def handle(request):
        state["requests"].append(request)
        path = request.url.path
        if request.method == "POST" and path == "/api/protected/account-actions/prepare":
            return httpx.Response(200, json={"code": 422, "message": "需要明确禁言时长"})
        if request.method != "GET":
            raise AssertionError("synthetic reading task cannot perform a business mutation")
        if path == "/api/protected/notifications/summary":
            data = {"notifications": 2, "messages": 0, "total": 2}
        elif path == "/api/protected/notifications":
            data = {"items": notifications, "unread": 2}
        elif path in {"/api/public/activity/talks", "/api/protect/comments"}:
            data = []
        elif path == "/api/protected/accounts" and role in {"admin", "superadmin"}:
            data = [{"id": 20, "username": "synthetic_author_20", "role": "user",
                     "mutedUntil": None, "frozen": False, "muted": False,
                     "quotaUsed": 0, "quotaLimit": 500}]
        elif path == "/api/public/board":
            data = [{"talkKey": 45, "approved": 1, "content": "这个合成看板娘好笨啊",
                     "author": "", "createTime": "2026-10-26T12:00:00"}]
        elif path == "/api/public/activity/guestbook":
            data = [{"id": 45, "title": "", "titleTruncated": False,
                     "content": "这个合成看板娘好笨啊", "contentTruncated": False,
                     "displayName": "", "displayNameTruncated": False, "source": "board",
                     "approved": 1, "createdAt": "2026-10-26T12:00:00"}]
        elif path == "/api/protect/board" and role in {"admin", "superadmin"}:
            data = [{"talkKey": 45, "approved": 1, "content": "这个合成看板娘好笨啊",
                     "userId": 20, "username": "synthetic_author_20", "nickname": "合成昵称",
                     "author": "", "ai_result": "pass", "createTime": "2026-10-26T12:00:00"}]
        else:
            state["unsupported_reads"].append(path)
            return httpx.Response(404)
        return httpx.Response(200, json={"code": 200, "data": data,
                                        "total": len(data) if isinstance(data, list) else 1})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, \
            patch.object(base, "_client", client), \
            patch.object(confirm.settings, "jwt_secret", SECRET):
        yield state


def notifications():
    return backend("user")


def publisher():
    return backend("superadmin")
