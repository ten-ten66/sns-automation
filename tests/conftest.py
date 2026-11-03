"""Shared test fixtures for the SNS automation test suite."""

import asyncio
import os
import pytest
import pytest_asyncio
from unittest.mock import MagicMock, patch

# Set test environment before importing app modules
os.environ["SNS_API_KEY"] = "test-api-key-12345"
os.environ["ANTHROPIC_API_KEY"] = "test-key"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_sns.db"

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from src.core.models import Base


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db_engine():
    """Create a test database engine."""
    engine = create_async_engine("sqlite+aiosqlite:///test_sns.db", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    """Create a test database session."""
    session_maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_maker() as session:
        yield session


@pytest.fixture
def mock_claude_response():
    """Create a mock Claude API response."""
    def _make(text: str = '{"text": "test post", "hashtags": ["test"], "keywords": ["test"]}'):
        response = MagicMock()
        response.content = [MagicMock(text=text)]
        response.stop_reason = "end_turn"
        response.usage = MagicMock(input_tokens=100, output_tokens=50)
        return response
    return _make


@pytest.fixture
def mock_anthropic(mock_claude_response):
    """Mock the Anthropic client."""
    with patch("anthropic.Anthropic") as mock:
        client = MagicMock()
        client.messages.create.return_value = mock_claude_response()
        mock.return_value = client
        yield client
