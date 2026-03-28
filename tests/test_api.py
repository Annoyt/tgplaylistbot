import pytest


@pytest.mark.asyncio
async def test_health_check(client):
    """Test the health check endpoint."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "musicbot-platform"}


@pytest.mark.asyncio
async def test_index_page(client):
    """Test the index page renders."""
    response = await client.get("/")
    assert response.status_code == 200
    assert "html" in response.headers["content-type"]
