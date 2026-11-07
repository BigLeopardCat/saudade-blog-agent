"""Fresh actor verification, bounded conditions and idempotent result delivery to the blog."""

import hashlib
import json

import httpx

from agent.principal import SOURCE_ASSERTION, Principal
from agent.scheduling.service import require_admin
from config.settings import settings
from tools.base import host_credential


class HostOutcomeUnverified(Exception):
    """A business POST was attempted without a trustworthy outcome receipt."""

    def __init__(self, run_id):
        super().__init__("scheduled business receipt unavailable")
        self.run_id = run_id

    def result(self):
        return {
            "status": "incomplete",
            "reason": "execution_unverified",
            "run_id": self.run_id,
            "text": "定时操作可能已执行，但未能核对回执。请先核实执行记录，不要重新创建相同任务。",
        }


class BlogHost:
    def __init__(self, client=None):
        self.client = client or httpx.Client(timeout=15, trust_env=False)
        self.base = settings.agent_admin_base.rstrip("/")

    def close(self):
        self.client.close()

    def headers(self, owner):
        return {"Authorization": "Bearer " + host_credential(owner)}

    def actor(self, owner):
        response = self.client.get(
            self.base + "/api/protected/automation-actor", headers=self.headers(owner)
        )
        if response.status_code in (401, 403):
            raise PermissionError("schedule owner is no longer authorized")
        response.raise_for_status()
        body = response.json()
        value = body.get("data") if isinstance(body, dict) and body.get("code") == 200 else None
        if (
            not isinstance(value, dict)
            or value.get("id") != owner
            or type(value.get("id")) is not int
        ):
            raise PermissionError("could not verify schedule owner's identity")
        principal = Principal(owner, value.get("role"), SOURCE_ASSERTION)
        require_admin(principal)
        return principal

    def article_public(self, owner, identifier):
        self.actor(owner)
        response = self.client.get(self.base + f"/api/public/notes/{identifier}")
        if response.status_code == 404:
            return False
        response.raise_for_status()
        body = response.json()
        row = body.get("data") if isinstance(body, dict) and body.get("code") == 200 else None
        if not isinstance(row, dict) or type(row.get("noteKey")) is not int:
            raise ValueError("public article condition returned invalid evidence")
        if row["noteKey"] != identifier:
            raise ValueError("public article condition returned a different target")
        return True

    def notify(self, owner, run_id, title, text):
        self.actor(owner)
        content = (
            text if len(text) <= 1000 else text[:960] + "\n\n完整结果可请助手查询此计划的执行记录。"
        )
        response = self.client.post(
            self.base + "/api/protected/automation-results",
            headers=self.headers(owner),
            json={"runId": run_id, "title": title[:80], "content": content},
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or body.get("code") != 200:
            raise RuntimeError("schedule result notification was not acknowledged")
        data = body.get("data")
        if not isinstance(data, dict) or type(data.get("replayed")) is not bool:
            raise ValueError("invalid schedule notification receipt")
        identifier = data.get("notificationId")
        if identifier is None and data["replayed"]:
            return {"delivered": False, "reason": "notification_removed", "replayed": True}
        if type(identifier) is not int or identifier <= 0:
            raise ValueError("invalid schedule notification identity")
        return {"notification_id": identifier, "delivered": True, "replayed": data["replayed"]}

    def publish_announcement(self, owner, run_id, title, content):
        """Publish one approved payload; the blog atomically deduplicates by owner/run."""
        try:
            self.actor(owner)
        except PermissionError:
            return {"status": "blocked", "reason": "owner_not_authorized"}
        try:
            response = self.client.post(
                self.base + "/api/protected/automation-announcements",
                headers=self.headers(owner),
                json={"runId": run_id, "title": title, "content": content},
            )
            if response.status_code in (401, 403):
                return {"status": "blocked", "reason": "owner_not_authorized"}
            response.raise_for_status()
            body = response.json()
            data = body.get("data") if isinstance(body, dict) and body.get("code") == 200 else None
            digest = hashlib.sha256(
                json.dumps([title, content], ensure_ascii=False, separators=(",", ":")).encode()
            ).hexdigest()
            if (
                not isinstance(data, dict)
                or data.get("payloadHash") != digest
                or type(data.get("replayed")) is not bool
                or type(data.get("notificationCount")) is not int
                or data["notificationCount"] < 0
            ):
                raise ValueError("invalid scheduled announcement receipt")
            identifier = data.get("announcementId")
            if identifier is None and data["replayed"]:
                return {
                    "status": "completed",
                    "text": "定时公告曾发布，现已删除，不会重新发布。",
                    "publication": data,
                }
            if type(identifier) is not int or identifier <= 0:
                raise ValueError("invalid scheduled announcement identity")
            return {
                "status": "completed",
                "text": (
                    f"已发布公告 #{identifier}「{title}」，"
                    f"创建 {data['notificationCount']} 条个人通知。"
                ),
                "publication": data,
            }
        except (httpx.HTTPError, ValueError) as exc:
            raise HostOutcomeUnverified(run_id) from exc

    def change(self, owner, run_id, write, title):
        """Execute an approved version-bound transaction, with an atomic backend receipt."""
        try:
            self.actor(owner)
        except PermissionError:
            return {"status": "blocked", "reason": "owner_not_authorized"}
        try:
            response = self.client.post(
                self.base + "/api/protected/automation-changes",
                headers=self.headers(owner),
                json={"runId": run_id, "write": write},
            )
            if response.status_code in (401, 403):
                return {"status": "blocked", "reason": "owner_not_authorized"}
            if response.status_code == 409:
                return {
                    "status": "incomplete",
                    "text": "定时变更未执行或未能确认，请核对目标状态后重新审批。",
                }
            response.raise_for_status()
            body = response.json()
            data = body.get("data") if isinstance(body, dict) and body.get("code") == 200 else None
            digest = hashlib.sha256(
                json.dumps(
                    write, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            if (
                not isinstance(data, dict)
                or data.get("payloadHash") != digest
                or type(data.get("replayed")) is not bool
                or not isinstance(data.get("outcome"), (dict, list))
            ):
                raise ValueError("invalid scheduled business receipt")
            return {
                "status": "completed",
                "text": f"定时任务「{title}」已执行并核对回执。",
                "change": data,
            }
        except (httpx.HTTPError, ValueError) as exc:
            raise HostOutcomeUnverified(run_id) from exc
