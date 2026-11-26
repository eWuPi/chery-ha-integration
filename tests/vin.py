"""Vehicle identifier used by the test fixtures.

The real registration is never committed. It can come from ``CHERY_TEST_VIN``
or from ``tests/vin.local`` (git-ignored); when neither is present the
synthetic ``TESTVIN0000000001`` is used, so CI and the public repository only
ever run on fake data.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_VIN = "TESTVIN0000000001"
ENV_VAR = "CHERY_TEST_VIN"
LOCAL_FILE = Path(__file__).with_name("vin.local")


def load_vin() -> str:
    """Return the configured registration, or the placeholder."""
    from_env = os.environ.get(ENV_VAR, "").strip()
    if from_env:
        return from_env
    try:
        from_file = LOCAL_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return DEFAULT_VIN
    return from_file or DEFAULT_VIN


VIN = load_vin()
