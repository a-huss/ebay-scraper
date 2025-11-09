import asyncio
import re
import time
import urllib.parse
import random
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional, Tuple

from playwright.async_api import async_playwright, TimeoutError as PWTimeout, Error as PWError

# Enhanced stealth configuration
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/120.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0 Safari/537.36"
]

# Memory-optimized arguments for Railway
CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-gpu",  # Critical for Railway
    "--single-process",  # Use single process to save memory
    "--no-zygote",
    "--no-first-run",
    "--disable-extensions",
    "--disable-plugins",
    "--disable-translate",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--memory-pressure-off",
    "--max-old-space-size=512",  # Limit memory
]

# Free proxy rotation (will fall back to no proxy if these don't work)
FREE_PROXIES = [
    None,  # First try without proxy
]

@dataclass
class SoldItem:
    title: str
    price_text: str
    price_gbp: Optional[float]
    price_usd: Optional[float]
    shipping_text: Optional[str]
    condition: Optional[str]
    sold_info: Optional[str]
    url: str
    image: Optional[str]


def _parse_price_to_gbp(price_text: str) -> Optional[float]:
    """Parse price text to GBP float - handles both GBP and USD"""
    if not price_text:
        return None
    
    # Try GBP first
    gbp_patterns = [
        r"£\s*([0-9][0-9,]*(?:\.[0-9]{2})?)",
        r"GBP\s*([0-9][0-9,]*(?:\.[0-9]{2})?)",
    ]
    
    for pattern in gbp_patterns:
        m = re.search(pattern, price_text, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                continue
    
    # Try USD and convert to GBP (approx 0.78 exchange rate)
    usd_patterns = [
        r"US\s*\$\s*([0-9][0-9,]*(?:\.[0-9]{2})?)",
        r"\$\s*([0-9][0-9,]*(?:\.[0-9]{2})?)",
        r"USD\s*([0-9][0-9,]*(?:\.[0-9]{2})?)",
    ]
    
    for pattern in usd_patterns:
        m = re.search(pattern, price_text, re.IGNORECASE)
        if m:
            try:
                usd_amount = float(m.group(1).replace(",", ""))
                # Convert USD to GBP (approximate rate)
                return round(usd_amount * 0.78, 2)
            except ValueError:
                continue
    
    return None


def _gbp_to_usd(gbp: Optional[float], usd_rate: float) -> Optional[float]:
    if gbp is None:
        return None
    return round(gbp * usd_rate, 2)


def _build_search_url(query: str, page: int, mobile: bool) -> str:
    q = urllib.parse.quote_plus(query)
    base = "https://www.ebay.co.uk"
    return f"{base}/sch/i.html?_nkw={q}&LH_Sold=1&LH_Complete=1&_sop=13&_ipg=50&_pgn={page}"


async def _extract_item_price_debug(page) -> Tuple[Optional[float], Optional[str]]:
    """Enhanced price extraction with debugging"""
    print("🔍 Debug: Looking for price on item page...")
    
    # More comprehensive price selectors
    price_selectors = [
        '.x-price-primary span',
        '[data-testid="x-price-primary"] span',
        'span[itemprop="price"]',
        '.ux-textspans[aria-hidden="true"]',
        '.ux-labels-values__values .ux-textspans',
        '.vi-price .notranslate',
        '#prcIsum',
        '#mm-saleDscPrc',
        '.mainPrice',
        '.display-price',
        '.vi-price',
        '.notranslate',
    ]
    
    price_gbp = None
    price_text_found = None
    
    for selector in price_selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()
            if count > 0:
                for i in range(min(count, 3)):
                    try:
                        text = await locator.nth(i).text_content()
                        if text:
                            cleaned_text = text.strip()
                            print(f"💰 Found price text with '{selector}': '{cleaned_text}'")
                            parsed_price = _parse_price_to_gbp(cleaned_text)
                            if parsed_price is not None:
                                price_gbp = parsed_price
                                price_text_found = cleaned_text
                                print(f"✅ Successfully parsed price: £{price_gbp}")
                                break
                    except Exception as e:
                        continue
                if price_gbp is not None:
                    break
        except Exception:
            continue
    
    # If no price found, try to get any text that looks like a price
    if price_gbp is None:
        try:
            # Get all text content and look for price patterns
            content = await page.content()
            # Look for both GBP and USD prices
            price_matches = re.findall(r'[£$]\s*\d+[\d,]*\.?\d*', content)
            if price_matches:
                for match in price_matches:
                    price_text_found = match
                    price_gbp = _parse_price_to_gbp(price_text_found)
                    if price_gbp:
                        print(f"🔍 Found price in page content: {price_text_found} -> £{price_gbp}")
                        break
        except Exception:
            pass

    # Extract sold info with better selectors
    sold_info = None
    sold_selectors = [
        "span.ux-textspans:has-text('Ended') + span.ux-textspans",
        "div.ux-labels-values__labels:has(span:has-text('Ended')) + div .ux-textspans",
        "span:has-text('Sold')",
        ".vi-tm-pos",
        ".vi-price .vi-acc-del-range",
        ".vi-bboxrev-pos",
        ".vi-notify-new-bg-dBtm",  # Sold badge
    ]
    
    for selector in sold_selectors:
        try:
            locator = page.locator(selector)
            if await locator.count() > 0:
                sold_text = await locator.first.text_content()
                if sold_text:
                    sold_info = sold_text.strip()
                    print(f"📅 Found sold info: {sold_info}")
                    break
        except Exception:
            continue

    return price_gbp, sold_info


async def _extract_additional_info(page) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract condition, shipping, and image from item page"""
    condition = None
    shipping = None
    image = None
    
    # Enhanced condition selectors
    condition_selectors = [
        '.x-item-condition-text .ux-textspans',
        '[data-testid="x-item-condition-text"] .ux-textspans',
        '#vi-itm-cond',
        '.ux-labels-values__values:has(.ux-textspans:has-text("Condition")) .ux-textspans',
        '.vi-condition',
        '.prodDetail',
    ]
    
    for selector in condition_selectors:
        try:
            locator = page.locator(selector)
            if await locator.count() > 0:
                condition_text = await locator.first.text_content()
                if condition_text:
                    condition = condition_text.strip()
                    break
        except Exception:
            pass
    
    # Enhanced shipping selectors
    shipping_selectors = [
        '#fshippingCost',
        '.ux-labels-values__values:has(.ux-textspans:has-text("Postage")) .ux-textspans',
        '[data-testid="x-shipping-cost"]',
        '.vi-shipping',
        '.sh-price',
        '.frshippingCost',
    ]
    
    for selector in shipping_selectors:
        try:
            locator = page.locator(selector)
            if await locator.count() > 0:
                shipping_text = await locator.first.text_content()
                if shipping_text:
                    shipping = shipping_text.strip()
                    break
        except Exception:
            pass
    
    # Enhanced image selectors
    image_selectors = [
        '#icImg',
        '#mainImg',
        '.ux-image-filmstrip__item img',
        '.vi-image-gallery__main-image img',
        '.picture-panel img',
    ]
    
    for selector in image_selectors:
        try:
            locator = page.locator(selector)
            if await locator.count() > 0:
                image_src = await locator.first.get_attribute('src')
                if image_src:
                    image = image_src
                    break
        except Exception:
            pass
    
    return condition, shipping, image


async def _new_browser_context(pw, *, headless: bool, mobile: bool, proxy_index=0):
    """Memory-optimized browser context for Railway"""
    browser = await pw.chromium.launch(
        headless=headless, 
        args=CHROMIUM_ARGS
    )
    
    # Use a single user agent to save memory
    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    
    print(f"🕵️ Using User Agent: {user_agent[:50]}...")
    
    context = await browser.new_context(
        viewport={"width": 1280, "height": 720},  # Smaller viewport to save memory
        user_agent=user_agent,
        locale="en-GB",
        timezone_id="Europe/London",
    )
    
    # Block unnecessary resources to save memory
    await context.route("**/*.{png,jpg,jpeg,gif,webp,svg}", lambda route: route.abort())
    await context.route("**/*.css", lambda route: route.abort())
    await context.route("**/*.woff2", lambda route: route.abort())
    
    # Increase timeouts for better reliability
    context.set_default_navigation_timeout(60000)
    context.set_default_timeout(45000)
    
    return browser, context, await context.new_page()


async def run_with_retries(
    query: str,
    *,
    pages: int = 1,
    per_page: int = 30,
    headless: bool = True,
    usd_rate: float = 1.28,
    mobile: bool = False,
    smoke: bool = False,
    max_retries: int = 2  # Reduced retries to save memory
) -> Dict[str, Any]:
    """
    Enhanced run function with retry logic and proxy rotation
    """
    for attempt in range(max_retries):
        try:
            print(f"🔄 Attempt {attempt + 1}/{max_retries} for query: '{query}'")
            
            result = await run(
                query, 
                pages=pages, 
                per_page=per_page,
                headless=headless,
                usd_rate=usd_rate,
                mobile=mobile,
                smoke=smoke,
                proxy_index=attempt
            )
            
            if result.get('success') and result.get('count', 0) > 0:
                print(f"✅ Success on attempt {attempt + 1} - found {result['count']} items")
                return result
            elif result.get('success') and result.get('count', 0) == 0:
                print(f"⚠️ No items found on attempt {attempt + 1}, but request succeeded")
                # Still return the result even if no items found
                return result
                
        except Exception as e:
            print(f"❌ Attempt {attempt + 1} failed: {e}")
            if attempt == max_retries - 1:
                # If all retries failed, return error
                return {
                    "success": False,
                    "error": f"All {max_retries} attempts failed: {str(e)}",
                    "query": query,
                    "pages_requested": pages,
                    "per_page_requested": per_page,
                    "count": 0,
                    "items": [],
                    "elapsed_sec": 0,
                }
            
            # Wait before retry with exponential backoff
            wait_time = 2 ** attempt
            print(f"⏳ Waiting {wait_time}s before retry...")
            await asyncio.sleep(wait_time)
    
    return {
        "success": False,
        "error": f"All {max_retries} attempts failed",
        "query": query,
        "pages_requested": pages,
        "per_page_requested": per_page,
        "count": 0,
        "items": [],
        "elapsed_sec": 0,
    }


async def run(
    query: str,
    *,
    pages: int = 1,
    per_page: int = 30,
    headless: bool = True,
    usd_rate: float = 1.28,
    mobile: bool = False,
    smoke: bool = False,
    proxy_index: int = 0
) -> Dict[str, Any]:
    """
    Memory-optimized scraper for Railway with individual page visits
    """
    start_time = time.time()
    all_items: List[Dict[str, Any]] = []
    seen_urls = set()

    async with async_playwright() as pw:
        browser, context, page = await _new_browser_context(pw, headless=headless, mobile=mobile, proxy_index=proxy_index)

        try:
            if smoke:
                await page.goto("https://example.com", wait_until="domcontentloaded")
                title = await page.title()
                return {"success": True, "title": title}

            for page_num in range(1, pages + 1):
                if len(all_items) >= per_page:
                    break
                    
                # Navigate to search results page
                search_url = _build_search_url(query, page_num, mobile=False)
                print(f"🔍 Searching: {search_url}")
                
                try:
                    # Add random delay before navigation to appear more human
                    await asyncio.sleep(random.uniform(1, 3))
                    
                    await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_load_state("networkidle")
                    print("✅ Search page loaded successfully")
                except PWTimeout:
                    print(f"❌ Timeout loading search page {page_num}")
                    continue
                except Exception as e:
                    print(f"❌ Error loading search page {page_num}: {e}")
                    continue

                # Scroll to load content with random patterns
                for _ in range(3):
                    await page.mouse.wheel(0, random.randint(800, 2000))
                    await page.wait_for_timeout(random.randint(300, 800))

                # Extract item links & titles from search results
                items = await page.evaluate("""
                (() => {
                  const out = [];
                  const seen = new Set();
                  const nodes = Array.from(document.querySelectorAll('a[href*="/itm/"]'));
                  for (const a of nodes) {
                    const href = a.getAttribute('href') || '';
                    if (!href || seen.has(href)) continue;
                    let card = a.closest('li') || a.closest('[class*="s-item"]') || a.parentElement;
                    const title = (card && (card.querySelector('.s-item__title, h3.s-item__title, [role="heading"]')?.textContent || '').trim())
                                  || (a.textContent || '').trim();
                    if (!title || title.toLowerCase().includes('shop on ebay')) continue;
                    
                    // Try to get image
                    let image = null;
                    const imgEl = card?.querySelector('img');
                    if (imgEl) {
                        image = imgEl.getAttribute('src') || imgEl.getAttribute('data-src');
                    }
                    
                    // Try to get price from search results as fallback
                    let price_text = '';
                    const priceEl = card?.querySelector('.s-item__price');
                    if (priceEl) {
                        price_text = priceEl.textContent.trim();
                    }
                    
                    // Try to get shipping from search results
                    let shipping_text = '';
                    const shippingEl = card?.querySelector('.s-item__shipping, .s-item__logisticsCost');
                    if (shippingEl) {
                        shipping_text = shippingEl.textContent.trim();
                    }
                    
                    out.push({title, url: href, image, price_text, shipping_text});
                    seen.add(href);
                  }
                  return out;
                })()
                """)

                print(f"📦 Found {len(items)} items on page {page_num}")

                # MEMORY OPTIMIZATION: Process only first 10 items per page to avoid crashes
                items_to_process = items[:10]
                
                # Visit individual item pages with memory management
                processed_count = 0
                for item in items_to_process:
                    if len(all_items) >= per_page or processed_count >= 10:
                        break
                        
                    # Normalize URL
                    item_url = item['url']
                    if item_url.startswith('//'):
                        item_url = 'https:' + item_url
                    elif not item_url.startswith('http'):
                        item_url = 'https://www.ebay.co.uk' + item_url
                    
                    # Remove tracking parameters
                    clean_url = re.sub(r"\?.*$", "", item_url)
                    if clean_url in seen_urls:
                        continue
                    seen_urls.add(clean_url)

                    print(f"🛒 Visiting ({processed_count + 1}/10): {item['title'][:60]}...")
                    
                    try:
                        # Add longer delay between item pages to reduce memory pressure
                        await asyncio.sleep(2)
                        
                        # Navigate to item page
                        await page.goto(item_url, wait_until="domcontentloaded", timeout=30000)
                        await page.wait_for_load_state("networkidle")
                        
                        # Extract detailed information
                        price_gbp, sold_info = await _extract_item_price_debug(page)
                        condition, shipping, image = await _extract_additional_info(page)
                        
                        # Use search result image if item page image not found
                        if not image and item.get('image'):
                            image = item['image']
                        
                        # Use search result price as fallback
                        search_price_text = item.get('price_text', '')
                        search_shipping_text = item.get('shipping_text', '')
                        
                        if price_gbp is None and search_price_text:
                            price_gbp = _parse_price_to_gbp(search_price_text)
                            if price_gbp:
                                print(f"🔄 Using search result price: £{price_gbp}")
                        
                        # Use search result shipping as fallback
                        if not shipping and search_shipping_text:
                            shipping = search_shipping_text
                        
                        # Create item
                        sold_item = SoldItem(
                            title=item['title'].replace("Opens in a new window or tab", "").strip(),
                            price_text=f"£{price_gbp:.2f}" if price_gbp else search_price_text or "N/A",
                            price_gbp=price_gbp,
                            price_usd=_gbp_to_usd(price_gbp, usd_rate),
                            shipping_text=shipping,
                            condition=condition,
                            sold_info=sold_info,
                            url=clean_url,
                            image=image,
                        )
                        
                        all_items.append(asdict(sold_item))
                        processed_count += 1
                        print(f"✅ Collected: {sold_item.title[:60]}... | Price: £{price_gbp}")
                        
                    except Exception as e:
                        print(f"❌ Failed to process {item['title'][:60]}: {e}")
                        continue

                print(f"📊 Page {page_num} complete. Total collected: {len(all_items)}")

        except Exception as e:
            print(f"❌ Fatal error: {e}")
            return {
                "success": False,
                "error": f"{type(e).__name__}: {e}",
                "query": query,
                "pages_requested": pages,
                "per_page_requested": per_page,
                "count": len(all_items),
                "items": all_items,
                "elapsed_sec": round(time.time() - start_time, 3),
            }
        finally:
            await browser.close()

    return {
        "success": True,
        "query": query,
        "pages_requested": pages,
        "per_page_requested": per_page,
        "count": len(all_items),
        "items": all_items,
        "elapsed_sec": round(time.time() - start_time, 3),
    }


# For backward compatibility, alias the main function
main = run_with_retries