import asyncio
import sys
import os

# Add current directory to path
sys.path.append(os.getcwd())

from ebay_sold_itempages import run

async def main():
    print("🧪 Testing scraper directly...")
    result = await run(
        "yugioh blue-eyes white dragon",
        pages=1,
        per_page=5,
        headless=False,  # Set to False to see the browser
        usd_rate=1.28
    )
    print(f"✅ Success: {result['success']}")
    print(f"📊 Count: {result['count']}")
    for item in result.get('items', [])[:3]:  # Show first 3 items
        print(f"  - {item['title'][:50]}... - £{item.get('price_gbp', 'N/A')}")

if __name__ == "__main__":
    asyncio.run(main())
