import asyncio
import re
import time
import urllib.parse
import random
import traceback
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional, Tuple

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# =========================
# Data Model
# =========================

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


# =========================
# Helpers
# =========================

def _parse_price_to_gbp(price_text: str) -> Optional[float]:
    """Parse price text to GBP float - handles both GBP and USD."""
    if not price_text:
        return None

    cleaned = price_text.strip()

    # GBP patterns
    gbp_patterns = [
        r"£\s*([0-9][0-9,]*(?:\.[0-9]{2})?)",
        r"GBP\s*([0-9][0-9,]*(?:\.[0-9]{2})?)",
        r"([0-9][0-9,]*(?:\.[0-9]{2})?)\s*GBP",
    ]
    for pattern in gbp_patterns:
        m = re.search(pattern, cleaned, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass

    # USD patterns
    usd_patterns = [
        r"US\s*\$\s*([0-9][0-9,]*(?:\.[0-9]{2})?)",
        r"\$\s*([0-9][0-9,]*(?:\.[0-9]{2})?)",
        r"USD\s*([0-9][0-9,]*(?:\.[0-9]{2})?)",
        r"([0-9][0-9,]*(?:\.[0-9]{2})?)\s*USD",
        r"([0-9][0-9,]*(?:\.[0-9]{2})?)\s*US\$",
    ]
    for pattern in usd_patterns:
        m = re.search(pattern, cleaned, re.IGNORECASE)
        if m:
            try:
                usd = float(m.group(1).replace(",", ""))
                return round(usd * 0.78, 2)
            except ValueError:
                pass

    # Pure number fallback
    pure_number = re.search(r"^\s*([0-9][0-9,]*(?:\.[0-9]{2})?)\s*$", cleaned)
    if pure_number:
        try:
            return float(pure_number.group(1).replace(",", ""))
        except ValueError:
            pass

    return None


def _gbp_to_usd(gbp: Optional[float], usd_rate: float) -> Optional[float]:
    if gbp is None:
        return None
    return round(gbp * usd_rate, 2)


def _build_search_url(query: str, page: int, mobile: bool) -> str:
    q = urllib.parse.quote_plus(query)
    base = "https://www.ebay.co.uk"
    return (
        f"{base}/sch/i.html?_nkw={q}"
        f"&LH_Sold=1&LH_Complete=1&_sop=13&_ipg=50&_pgn={page}"
    )


# =========================
# Extraction helpers
# =========================

async def _extract_item_price_debug(page) -> Tuple[Optional[float], Optional[str]]:
    """Extract price (GBP) and sold info from an item page, robustly."""
    print("🔍 Looking for price on item page...")

    price_gbp: Optional[float] = None
    sold_info: Optional[str] = None

    # 1) Modern selectors
    modern_selectors = [
        '.x-price-primary span',
        '[data-testid="x-price-primary"] span',
        '.ux-textspans[aria-hidden="true"]',
        '.ux-labels-values__values .ux-textspans',
        '.ux-textspans--BOLD',
        '[data-testid="x-price-0"] .ux-textspans',
    ]
    for selector in modern_selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()
            if count == 0:
                continue
            for i in range(min(count, 5)):
                try:
                    text = await locator.nth(i).text_content()
                    if not text:
                        continue
                    cleaned = text.strip()
                    if not cleaned:
                        continue
                    parsed = _parse_price_to_gbp(cleaned)
                    if parsed is not None:
                        price_gbp = parsed
                        print(f"✅ Price via {selector}: {cleaned} -> £{price_gbp}")
                        break
                except Exception:
                    continue
            if price_gbp is not None:
                break
        except Exception:
            continue

    # 2) Legacy selectors
    if price_gbp is None:
        legacy_selectors = [
            '#prcIsum',
            '#mm-saleDscPrc',
            '.vi-price .notranslate',
            '.mainPrice',
            '.display-price',
            '.vi-price',
            '.notranslate',
            '#prcIsum_bidPrice',
            '.vi-price-width',
        ]
        for selector in legacy_selectors:
            try:
                locator = page.locator(selector)
                if await locator.count() > 0:
                    text = await locator.first.text_content()
                    if text:
                        parsed = _parse_price_to_gbp(text.strip())
                        if parsed is not None:
                            price_gbp = parsed
                            print(f"✅ Price via {selector}: £{price_gbp}")
                            break
            except Exception:
                continue

    # 3) HTML scan (cheap patterns only)
    if price_gbp is None:
        try:
            content = await page.content()
            patterns = [
                r'£\s*\d+[\d,]*\.?\d*',
                r'\$\s*\d+[\d,]*\.?\d*',
            ]
            for pattern in patterns:
                matches = re.findall(pattern, content)
                for match in matches:
                    parsed = _parse_price_to_gbp(match)
                    if parsed is not None:
                        price_gbp = parsed
                        print(f"🔍 Price from HTML: {match} -> £{price_gbp}")
                        break
                if price_gbp is not None:
                    break
        except Exception:
            pass

    # Sold info selectors
    sold_selectors = [
        "span.ux-textspans:has-text('Ended') + span.ux-textspans",
        "span.ux-textspans:has-text('Sold') + span.ux-textspans",
        "div.ux-labels-values__labels:has(span:has-text('Ended')) + div .ux-textspans",
        "div.ux-labels-values__labels:has(span:has-text('Sold')) + div .ux-textspans",
        "[data-testid='x-sold-date'] .ux-textspans",
        ".vi-tm-pos",
        ".vi-price .vi-acc-del-range",
        ".vi-bboxrev-pos",
        ".vi-notify-new-bg-dBtm",
    ]
    for selector in sold_selectors:
        try:
            locator = page.locator(selector)
            if await locator.count() > 0:
                txt = await locator.first.text_content()
                if txt:
                    sold_info = txt.strip()
                    print(f"📅 Sold info: {sold_info}")
                    break
        except Exception:
            continue

    return price_gbp, sold_info


async def _extract_additional_info(page) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract condition, shipping and image from item page."""
    condition = None
    shipping = None
    image = None

    # Condition
    for selector in [
        '.x-item-condition-text .ux-textspans',
        '[data-testid="x-item-condition-text"] .ux-textspans',
        '#vi-itm-cond',
        '.vi-condition',
        '[data-testid="x-item-condition"] .ux-textspans',
    ]:
        try:
            locator = page.locator(selector)
            if await locator.count() > 0:
                txt = await locator.first.text_content()
                if txt:
                    condition = txt.strip()
                    break
        except Exception:
            pass

    # Shipping
    for selector in [
        '[data-testid="x-shipping-cost"]',
        '#fshippingCost',
        '.vi-shipping',
        '.sh-price',
        '.frshippingCost',
        '.ux-labels-values__values:has-text("Shipping") .ux-textspans',
    ]:
        try:
            locator = page.locator(selector)
            if await locator.count() > 0:
                txt = await locator.first.text_content()
                if txt:
                    shipping = txt.strip()
                    break
        except Exception:
            pass

    # Image
    for selector in [
        '#icImg',
        '#mainImg',
        '.ux-image-filmstrip__item img',
        '.vi-image-gallery__main-image img',
        '.picture-panel img',
        '[data-testid="picture-container"] img',
        '.ux-image-carousel-item img',
    ]:
        try:
            locator = page.locator(selector)
            if await locator.count() > 0:
                src = await locator.first.get_attribute("src")
                if src:
                    # avoid tiny thumbs when possible
                    if 's-l64' in src or 's-l50' in src:
                        continue
                    if 's-l500' in src:
                        src = src.replace('s-l500', 's-l1600')
                    image = src
                    break
        except Exception:
            pass

    return condition, shipping, image


# =========================
# Browser context (Railway-friendly)
# =========================

async def _new_browser_context(pw, *, headless: bool):
    """Stable browser context for constrained containers (Railway)."""
    try:
        browser = await pw.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
    except Exception as e:
        print("❌ PLAYWRIGHT_LAUNCH_ERROR:", e)
        print(traceback.format_exc())
        raise

    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit(537.36) (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    print(f"🕵️ Using User Agent: {user_agent[:50]}...")

    context = await browser.new_context(
        viewport={"width": 1280, "height": 720},
        user_agent=user_agent,
        locale="en-GB",
        timezone_id="Europe/London",
        java_script_enabled=True,
        ignore_https_errors=True,
    )

    # Correct route handler signature: only `route`
    async def block_assets(route):
        r = route.request
        if r.resource_type in ("image", "media", "font"):
            await route.abort()
        else:
            await route.continue_()

    await context.route("**/*", block_assets)

    context.set_default_navigation_timeout(45000)
    context.set_default_timeout(30000)

    return browser, context


async def _safe_goto_page(page, url: str, *, max_retries: int = 2) -> bool:
    """Navigate to a URL with retries using domcontentloaded only."""
    for attempt in range(1, max_retries + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_load_state("domcontentloaded")
            return True
        except Exception as e:
            print(f"❌ Navigation attempt {attempt} to {url} failed: {e}")
            if attempt < max_retries:
                await asyncio.sleep(1)
    return False


# =========================
# Core run + retries
# =========================

async def run_with_retries(
    query: str,
    *,
    pages: int = 1,
    per_page: int = 30,
    headless: bool = True,
    usd_rate: float = 1.28,
    mobile: bool = False,
    smoke: bool = False,
    max_retries: int = 2,
) -> Dict[str, Any]:
    last_error: Optional[str] = None

    for attempt in range(1, max_retries + 1):
        print(f"🔄 Attempt {attempt}/{max_retries} for query='{query}'")
        try:
            result = await run(
                query=query,
                pages=pages,
                per_page=per_page,
                headless=headless,
                usd_rate=usd_rate,
                mobile=mobile,
                smoke=smoke,
            )

            if result.get("success"):
                print(f"✅ Success on attempt {attempt} with {result.get('count', 0)} items")
                return result

            last_error = result.get("error") or "Unknown error"
            print(f"⚠️ Attempt {attempt} failed logically: {last_error}")

        except Exception as e:
            last_error = str(e)
            print(f"❌ Exception in attempt {attempt}: {e}")
            print(traceback.format_exc())

        if attempt < max_retries:
            wait = 2 ** (attempt - 1)
            print(f"⏳ Waiting {wait}s before retry...")
            await asyncio.sleep(wait)

    return {
        "success": False,
        "error": f"All {max_retries} attempts failed. Last error: {last_error}",
        "query": query,
        "pages_requested": pages,
        "per_page_requested": per_page,
        "count": 0,
        "items": [],
        "elapsed_sec": 0,
    }


# =========================
# Single run
# =========================

async def run(
    query: str,
    *,
    pages: int = 1,
    per_page: int = 30,
    headless: bool = True,
    usd_rate: float = 1.28,
    mobile: bool = False,
    smoke: bool = False,
) -> Dict[str, Any]:
    """Single-attempt scrape with stable evaluate and low memory usage."""
    start_time = time.time()
    all_items: List[Dict[str, Any]] = []
    seen_urls = set()

    try:
        async with async_playwright() as pw:
            browser, context = await _new_browser_context(pw, headless=headless)

            if smoke:
                page = await context.new_page()
                ok = await _safe_goto_page(page, "https://example.com")
                title = await page.title() if ok else "navigation-failed"
                await browser.close()
                return {
                    "success": ok,
                    "title": title,
                    "elapsed_sec": round(time.time() - start_time, 3),
                    **({} if ok else {"error": "Failed to load example.com"}),
                }

            search_page = await context.new_page()

            try:
                for page_num in range(1, pages + 1):
                    if len(all_items) >= per_page:
                        break

                    search_url = _build_search_url(query, page_num, mobile=False)
                    print(f"🔍 Searching: {search_url}")

                    await asyncio.sleep(random.uniform(0.5, 1.0))
                    if not await _safe_goto_page(search_page, search_url):
                        print(f"❌ Failed to load search page {page_num}")
                        continue

                    print("✅ Search page loaded successfully")

                    # Light scroll only (avoid crashes)
                    try:
                        await search_page.mouse.wheel(0, 600)
                        await search_page.wait_for_timeout(200)
                    except Exception as e:
                        print(f"⚠️ Scroll skipped: {e}")

                    # Safer, simpler evaluate (no crazy traversals)
                    try:
                        items = await search_page.evaluate(
                            """
                            () => {
                              const out = [];
                              const cards = document.querySelectorAll('.s-item');

                              for (const card of cards) {
                                const link = card.querySelector('a[href*="/itm/"]');
                                if (!link) continue;

                                const href = link.getAttribute('href') || '';
                                if (!href) continue;

                                const titleEl =
                                  card.querySelector('.s-item__title, h3.s-item__title, [role="heading"]');
                                const title = (titleEl?.textContent || link.textContent || '').trim();
                                if (!title || title.toLowerCase().includes('shop on ebay')) continue;

                                const imgEl = card.querySelector('img');
                                const image =
                                  imgEl?.getAttribute('src') || imgEl?.getAttribute('data-src') || null;

                                const priceEl = card.querySelector('.s-item__price');
                                const price_text = (priceEl?.textContent || '').trim();

                                const shipEl = card.querySelector('.s-item__shipping, .s-item__logisticsCost');
                                const shipping_text = (shipEl?.textContent || '').trim();

                                out.push({ title, url: href, image, price_text, shipping_text });
                              }

                              return out;
                            }
                            """
                        )
                    except Exception as e:
                        print(f"❌ Page.evaluate on search page crashed: {e}")
                        items = []

                    print(f"📦 Found {len(items)} items on page {page_num}")

                    # Cap how many items we fully resolve per page
                    max_items_per_page = min(5, per_page - len(all_items))
                    for idx, item in enumerate(items[:max_items_per_page], start=1):
                        if len(all_items) >= per_page:
                            break

                        raw_url = item["url"]
                        if raw_url.startswith("//"):
                            raw_url = "https:" + raw_url
                        elif not raw_url.startswith("http"):
                            raw_url = "https://www.ebay.co.uk" + raw_url

                        clean_url = re.sub(r"\?.*$", "", raw_url)
                        if clean_url in seen_urls:
                            continue
                        seen_urls.add(clean_url)

                        print(f"🛒 Visiting ({idx}/{max_items_per_page}): {item['title'][:80]}")

                        item_page = await context.new_page()
                        try:
                            await asyncio.sleep(random.uniform(0.7, 1.3))

                            ok = await _safe_goto_page(item_page, raw_url)
                            if not ok:
                                print("❌ Item page load failed after retries")
                                continue

                            await item_page.wait_for_timeout(500)

                            price_gbp, sold_info = await _extract_item_price_debug(item_page)
                            condition, shipping, image = await _extract_additional_info(item_page)

                            search_price_text = item.get("price_text") or ""
                            search_shipping_text = item.get("shipping_text") or ""

                            if price_gbp is None and search_price_text:
                                parsed = _parse_price_to_gbp(search_price_text)
                                if parsed is not None:
                                    price_gbp = parsed
                                    print(f"🔄 Using search result price: £{price_gbp}")

                            if not shipping and search_shipping_text:
                                shipping = search_shipping_text

                            if not image and item.get("image"):
                                image = item["image"]

                            sold_item = SoldItem(
                                title=item["title"].replace("Opens in a new window or tab", "").strip(),
                                price_text=(
                                    f"£{price_gbp:.2f}"
                                    if price_gbp is not None
                                    else search_price_text or "N/A"
                                ),
                                price_gbp=price_gbp,
                                price_usd=_gbp_to_usd(price_gbp, usd_rate),
                                shipping_text=shipping,
                                condition=condition,
                                sold_info=sold_info,
                                url=clean_url,
                                image=image,
                            )

                            all_items.append(asdict(sold_item))
                            print(f"✅ Collected: {sold_item.title[:80]} | {sold_item.price_text}")

                        except Exception as e:
                            print(f"❌ Failed item ({item['title'][:80]}): {e}")
                            print(traceback.format_exc())
                        finally:
                            await item_page.close()

                    print(f"📊 Page {page_num} complete. Total collected so far: {len(all_items)}")

            finally:
                await browser.close()

    except Exception as e:
        print("❌ Outer fatal error in run():", e)
        print(traceback.format_exc())
        return {
            "success": False,
            "error": f"Fatal error: {e}",
            "query": query,
            "pages_requested": pages,
            "per_page_requested": per_page,
            "count": len(all_items),
            "items": all_items,
            "elapsed_sec": round(time.time() - start_time, 3),
        }

    success = len(all_items) > 0

    return {
        "success": success,
        "error": None if success else "No items collected",
        "query": query,
        "pages_requested": pages,
        "per_page_requested": per_page,
        "count": len(all_items),
        "items": all_items,
        "elapsed_sec": round(time.time() - start_time, 3),
    }


# Backwards-compatible entrypoint used by FastAPI
main = run_with_retries
