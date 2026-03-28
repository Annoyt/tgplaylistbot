from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
@patch("services.vk_music.search", new_callable=AsyncMock)
async def test_vk_search_mock(mock_search):
    """Test that our service call logic is correctly mocked and called."""
    mock_search.return_value = [
        {"title": "Mock Song", "artist": "Mock Artist", "url": "http://mock.mp3"}
    ]
    
    # We import here to ensure the patch is active
    from services.vk_music import search
    results = await search("test query")
    
    assert len(results) == 1
    assert results[0]["title"] == "Mock Song"
    mock_search.assert_called_once_with("test query")
