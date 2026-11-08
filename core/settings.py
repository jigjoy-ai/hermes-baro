"""The plugin's settings (plugin.yaml ``config_schema``) and what they resolve to.

Off by default: the plugin then picks no backend, URL or model, and Baro uses
whatever its user configured. Turned on, Baro runs on the Hermes subscription
through the proxy that ``hermes proxy start`` serves on this machine; the proxy
attaches the user's own credential, so the plugin never holds one.
"""

from __future__ import annotations

HERMES_PROXY_URL = "http://127.0.0.1:8645/v1"

SETTING_DEFAULTS = {
    "hermes_subscription": False,
    "hermes_subscription_model": "nous/welcome",
    "hermes_proxy_url": HERMES_PROXY_URL,
}

_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]")


def is_local_url(url: str) -> bool:
    """True for an http(s) URL whose host is this machine."""
    rest = url.split("://", 1)[-1]
    host = rest.split("/", 1)[0]
    if host.startswith("["):
        host = host.split("]", 1)[0] + "]"
    else:
        host = host.rsplit(":", 1)[0]
    return host in _LOOPBACK_HOSTS or host.endswith(".localhost")


def subscription_backend(settings: dict) -> dict | None:
    """The ``llm`` / ``openai_base_url`` / ``model`` the subscription setting implies, or None."""
    if not settings.get("hermes_subscription"):
        return None
    return {
        "llm": "openai",
        "openai_base_url": str(settings.get("hermes_proxy_url") or HERMES_PROXY_URL),
        "model": str(settings.get("hermes_subscription_model") or SETTING_DEFAULTS["hermes_subscription_model"]),
    }
