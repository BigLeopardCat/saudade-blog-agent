"""Device endpoint and actor credentials used by the optional OLED demo."""

from config import settings as _settings

__all__ = ["DEVICE_SERVICE_URL"]

DEVICE_SERVICE_URL = _settings.device_service_url


def _actor_credential(user_id: int) -> str:
    from agent.delegation import credential

    return credential(user_id)


def _valid_device_id(device_id: str) -> bool:
    return bool(device_id) and all(c.isalnum() or c in "-_" for c in device_id)
