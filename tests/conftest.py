"""Suite-wide fixtures.

The GoingToCamp conformer caches the park catalog and its two vocabulary
tables for the life of the process (see `providers.going_to_camp`) — one
process is one monitor cycle in production, but one process is the *whole
suite* under pytest, so the cache is emptied between tests. Without this a
test's fixture catalog would leak into every test that ran after it.
"""

from __future__ import annotations

import pytest

from providers.going_to_camp import clear_metadata_cache


@pytest.fixture(autouse=True)
def _empty_going_to_camp_caches():
    clear_metadata_cache()
    yield
    clear_metadata_cache()
