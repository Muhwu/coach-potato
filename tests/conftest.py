from datetime import timezone

import pytest

from server import timeutil


@pytest.fixture(autouse=True)
def _pin_timezone(monkeypatch):
    """Calendar days follow the OS zone in the app; pin them to UTC so a test's
    result never depends on where it runs. Timezone tests override this."""
    monkeypatch.setattr(timeutil, "TZ", timezone.utc)
