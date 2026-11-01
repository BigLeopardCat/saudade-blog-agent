"""Fresh actor verification, bounded conditions and idempotent result delivery to the blog."""

import httpx

from agent.principal import SOURCE_ASSERTION, Principal
from agent.scheduling.service import require_admin
from config.settings import settings
from tools.base import _sign_local_jwt


class BlogHost:
    def __init__(self, client=None):
        self.client = client or httpx.Client(timeout=15, trust_env=False)
        self.base = settings.agent_admin_base.rstrip("/")

    def close(self):
        self.client.close()

    def headers(self, owner):
        return {"Authorization": "Bearer " + _sign_local_jwt(owner, "user")}

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
        response = self.client.post(
            self.base + "/api/protected/automation-results",
            headers=self.headers(owner),
            json={"runId": run_id, "title": title[:80], "content": text[:1000]},
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or body.get("code") != 200:
            raise RuntimeError("schedule result notification was not acknowledged")
        return {"notification_id": body["data"]["notificationId"]}
