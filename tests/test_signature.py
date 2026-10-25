"""bot/signature.py: "🤖 @bot" only in private chats."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.signature import with_signature


@pytest.fixture
def bot():
    return SimpleNamespace(me=AsyncMock(return_value=SimpleNamespace(username="plcm_bot")))


async def test_private_appends_after_blank_line(bot):
    assert await with_signature("🎬 720p", bot, "private") == "🎬 720p\n\n🤖 @plcm_bot"


async def test_private_empty_caption_is_just_signature(bot):
    assert await with_signature("", bot, "private") == "🤖 @plcm_bot"


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
async def test_groups_unchanged_and_no_api_call(bot, chat_type):
    assert await with_signature("🎬 720p", bot, chat_type) == "🎬 720p"
    bot.me.assert_not_called()
