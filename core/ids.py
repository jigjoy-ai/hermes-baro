"""Run identifiers: ``baro-YYYYMMDDTHHMMSSZ-<6 lowercase hex>``."""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from typing import Callable

RUN_ID_RE = r"^baro-\d{8}T\d{6}Z-[0-9a-f]{6}$"

_SUFFIX_RE = re.compile(r"^[0-9a-f]{6}$")


def new_run_id(
    now: datetime | None = None,
    rand: Callable[[], str] | None = None,
) -> str:
    """Return a new run id such as ``baro-20261105T142233Z-a1b2c3``.

    ``now`` defaults to the current UTC time; a naive datetime is taken as
    UTC and an aware one is converted to UTC. ``rand`` returns the 6-hex-digit
    suffix and defaults to a cryptographically random one.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    suffix = rand() if rand is not None else secrets.token_hex(3)
    if not isinstance(suffix, str) or not _SUFFIX_RE.match(suffix):
        raise ValueError(f"run id suffix must be 6 lowercase hex digits, got {suffix!r}")
    return f"baro-{now.strftime('%Y%m%dT%H%M%SZ')}-{suffix}"
