import asyncio

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for each test case."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def db():
    """In-memory SQLite for tests."""
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        # Run schema migrations (we can use the init_db logic but target this conn)
        # For simplicity in tests, we'll manually execute the schema or mock the get_db

        # We need a way to make the app use THIS connection.
        # For now, let's just provide the connection for service tests.
        yield conn


@pytest.fixture
async def client():
    """Async client for FastAPI testing."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
