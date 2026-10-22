"""Keep the legacy entity helper's asyncio replacements local to each test."""

import asyncio
import pytest

_create_task = asyncio.create_task
_get_running_loop = asyncio.get_running_loop


@pytest.fixture(autouse=True)
def restore_asyncio_helpers():
    """The init helper replaces these globals; do not leak them to other tests."""
    yield
    asyncio.create_task = _create_task
    asyncio.get_running_loop = _get_running_loop
