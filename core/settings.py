"""The plugin's settings (plugin.yaml ``config_schema``) and what they resolve to.

Off by default: the plugin then picks no backend, URL or model, and Baro uses
whatever its user configured. Turned on, Baro runs on the Hermes subscription
through the proxy that ``hermes proxy start`` serves on this machine; the proxy
attaches the user's own credential, so the plugin never holds one.
"""

from __future__ import annotations

from .command import normalize_base_url

HERMES_PROXY_URL = "http://127.0.0.1:8645/v1"

SETTING_DEFAULTS = {
    "hermes_subscription": False,
    "hermes_subscription_model": "nous/welcome",
    "hermes_proxy_url": HERMES_PROXY_URL,
}

_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]")

MAX_SETTING_CHARS = 2000


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


def backend_from_settings(settings: dict) -> dict | None:
    """The backend the settings name, checked for Baro's command line; None when they name none.

    This is the only place a backend comes from: a tool call cannot name one
    (tools.py), so the model can never point Baro at a host of its choosing.
    The proxy has to be on this machine, so what the settings add never sends
    the repository anywhere else. Raises ValueError for a value that cannot be
    passed on.
    """
    backend = subscription_backend(settings)
    if backend is None:
        return None
    url = normalize_base_url(backend["openai_base_url"])
    if not is_local_url(url):
        raise ValueError(
            f"the Proxy URL setting must point at this machine (localhost, 127.0.0.1 or ::1), got {url!r}"
        )
    model = backend["model"]
    if "\x00" in model or len(model) > MAX_SETTING_CHARS or model.startswith("-"):
        raise ValueError("the Subscription model setting is not a usable model name")
    return {"llm": backend["llm"], "openai_base_url": url, "model": model}
