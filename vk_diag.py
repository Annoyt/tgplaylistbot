
import asyncio
import logging
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).parent))

from services import vk_music
from app.db.database import init_db

async def test_search():
    logging.basicConfig(level=logging.INFO)
    print("--- VK Music Diag ---")
    await init_db()
    
    query = "Nirvana"
    print(f"Searching for: {query}")
    results = await vk_music.search(query, count=5)
    
    print(f"\nFound {len(results)} results:")
    for r in results:
        print(f"- {r.artist} - {r.title} [{r.source}]")
    
    if len(results) == 0:
        print("\nWARNING: No results found from VK. Check logs/credentials.")

if __name__ == "__main__":
    asyncio.run(test_search())
