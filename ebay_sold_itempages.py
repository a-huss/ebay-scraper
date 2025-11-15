import asyncio
import re
import time
import urllib.parse
import random
import traceback
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional, Tuple

from playwright.async_api import async_playwright


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
                # simple USD->GBP approx; final USD shown uses usd_rate (below)
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
    """
    Force SOLD + COMPLETED + sort by Newly Listed + 50 per page + NEW items only.
    NEW only = LH_ItemCondition=1000
    """
    q = urllib.parse.quote_plus(query)
    base = "https://www.ebay.co.uk"
    return (
        f"{base}/sch/i.html?_nkw={q}"
        f"&LH_Sold=1&LH_Complete=1&_sop=13&_ipg=50&_pgn={page}"
        f"&LH_ItemCondition=1000"
    )


# =========================
# Extraction helpers
# =========================

async def _extract_item_price_debug(page) -> Tuple[Optional[float], Optional[str]]:
    """Extract price (GBP) and sold info from an item page, robustly."""
    print("🔍 Looking for price on item page...")

    price_gbp: Optional[float] = None
    sold_info: Optional[str] = None

    # Give the price area a moment to render if it's lazy
    try:
        await page.wait_for_selector(
            '.x-price-primary, [data-testid="x-price-primary"], [data-testid="x-price-0"], '
            '#prcIsum, .vi-price, .ux-textspans--BOLD',
            timeout=2500
        )
    except Exception:
        pass

    # 1) Modern selectors (expanded)
    modern_selectors = [
        '.x-price-primary .ux-textspans',
        '[data-testid="x-price-primary"] .ux-textspans',
        '[data-testid="x-price-0"] .ux-textspans',
        '.x-price-section .ux-textspans[aria-hidden="true"]',
        '.ux-textspans--BOLD',
        '.ux-labels-values__values .ux-textspans',
    ]
    for selector in modern_selectors:
        try:
            loc = page.locator(selector)
            n = await loc.count()
            if n == 0:
                continue
            for i in range(min(n, 6)):
                try:
                    txt = await loc.nth(i).text_content()
                    if not txt:
                        continue
                    parsed = _parse_price_to_gbp(txt.strip())
                    if parsed is not None:
                        price_gbp = parsed
                        print(f"✅ Price via {selector}: {txt.strip()} -> £{price_gbp}")
                        break
                except Exception:
                    continue
            if price_gbp is not None:
                break
        except Exception:
            continue

    # 2) Legacy selectors (+ extras)
    if price_gbp is None:
        legacy_selectors = [
            '#prcIsum',
            '#mm-saleDscPrc',
            '#prcIsum_bidPrice',
            '.vi-price .notranslate',
            '.mainPrice',
            '.display-price',
            '.vi-price',
            '.notranslate',
            '.vi-price-width',
        ]
        for selector in legacy_selectors:
            try:
                loc = page.locator(selector)
                if await loc.count() > 0:
                    txt = await loc.first.text_content()
                    if txt:
                        parsed = _parse_price_to_gbp(txt.strip())
                        if parsed is not None:
                            price_gbp = parsed
                            print(f"✅ Price via {selector}: £{price_gbp}")
                            break
            except Exception:
                continue

    # 3) Structured data / inline JSON
    if price_gbp is None:
        try:
            # JSON-LD
            scripts = await page.locator('script[type="application/ld+json"]').all()
            for s in scripts:
                try:
                    content = await s.text_content()
                    if not content:
                        continue
                    m = re.search(r'"price"\s*:\s*"([^"]+)"', content)
                    if m:
                        parsed = _parse_price_to_gbp(m.group(1))
                        if parsed is not None:
                            price_gbp = parsed
                            print(f"📊 Price from JSON-LD: {m.group(1)} -> £{price_gbp}")
                            break
                except Exception:
                    continue
        except Exception:
            pass

    # 4) Cheap HTML scan fallback
    if price_gbp is None:
        try:
            html = await page.content()
            patterns = [
                r'"convertedPrice"\s*:\s*"([^"]*)"',
                r'"binPrice"\s*:\s*"([^"]*)"',
                r'"price"\s*:\s*"([^"]*)"',
                r'data-price="([^"]*)"',
                r'£\s*(\d+[\d,]*\.?\d*)',
                r'US\s*\$\s*(\d+[\d,]*\.?\d*)',
            ]
            for pat in patterns:
                for m in re.findall(pat, html):
                    parsed = _parse_price_to_gbp(m)
                    if parsed is not None:
                        price_gbp = parsed
                        print(f"🔍 Price from HTML pattern: {m} -> £{price_gbp}")
                        break
                if price_gbp is not None:
                    break
        except Exception:
            pass

    # Sold info (best-effort)
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
            loc = page.locator(selector)
            if await loc.count() > 0:
                txt = await loc.first.text_content()
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

    # Condition - updated selectors
    for selector in [
        '.x-item-condition-text',
        '[data-testid="x-item-condition-text"]',
        '.ux-labels-values__values-content .ux-textspans',
        '#vi-itm-cond',
        '.vi-condition',
        '[class*="condition"]',
        '.ux-textspans',  # broader
    ]:
        try:
            locator = page.locator(selector)
            if await locator.count() > 0:
                txt = await locator.first.text_content()
                if txt:
                    candidate = txt.strip()
                    if any(k in candidate.lower() for k in ['new', 'new with', 'pre-owned', 'used', 'excellent', 'good', 'fair', 'condition']):
                        condition = candidate
                        print(f"📦 Condition: {condition}")
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

    # Image (prefer higher res)
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

    # Keep CSS for layout; block heavy assets
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
# Item extraction from search page
# =========================

async def _extract_items_from_search_page(page) -> List[Dict[str, Any]]:
    """Extract items from search page with decent price capture."""
    items: List[Dict[str, Any]] = []
    try:
        items = await page.evaluate(
            """
            () => {
                const out = [];
                const listings = document.querySelectorAll('.s-item__wrapper, .s-item');
                for (const el of listings) {
                    try {
                        const link = el.querySelector('a.s-item__link');
                        if (!link) continue;
                        const href = link.getAttribute('href') || '';
                        if (!href.includes('/itm/')) continue;

                        const titleEl = el.querySelector('.s-item__title');
                        const title = titleEl ? titleEl.textContent.trim() : '';
                        if (!title || title.includes('Shop on eBay')) continue;

                        const priceEl = el.querySelector('.s-item__price');
                        const priceText = priceEl ? priceEl.textContent.trim() : '';

                        const shippingEl = el.querySelector('.s-item__shipping, .s-item__logisticsCost');
                        const shippingText = shippingEl ? shippingEl.textContent.trim() : '';

                        const imgEl = el.querySelector('.s-item__image img');
                        const image = imgEl ? (imgEl.getAttribute('src') || imgEl.getAttribute('data-src')) : null;

                        const condEl = el.querySelector('.s-item__subtitle, .SECONDARY_INFO');
                        const condition = condEl ? condEl.textContent.trim() : '';

                        out.push({ title, url: href, price_text: priceText, shipping_text: shippingText, image, condition });
                    } catch {}
                }
                return out;
            }
            """
        )
        print(f"📦 Found {len(items)} items on search page")
        for i, item in enumerate(items[:3]):
            print(f"  {i+1}. {item['title'][:60]}... | Price: '{item.get('price_text', 'N/A')}'")
    except Exception as e:
        print(f"❌ Item extraction failed: {e}")
        items = []
    return items


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
    """Single-attempt scrape with robust price extraction and NEW-only search."""
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

                    await asyncio.sleep(random.uniform(0.4, 0.9))
                    if not await _safe_goto_page(search_page, search_url):
                        print(f"❌ Failed to load search page {page_num}")
                        continue

                    print("✅ Search page loaded successfully")

                    # Brief wait to let cards render
                    await search_page.wait_for_timeout(1200)

                    items = await _extract_items_from_search_page(search_page)
                    if not items:
                        print("❌ No items found on search page")
                        continue

                    # Process only a few items per page to avoid crashes
                    max_items_per_page = min(3, per_page - len(all_items))
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
                            await asyncio.sleep(random.uniform(0.6, 1.1))

                            ok = await _safe_goto_page(item_page, raw_url)
                            if not ok:
                                print("❌ Item page load failed after retries")
                                continue

                            await item_page.wait_for_timeout(800)

                            # Extract details
                            price_gbp, sold_info = await _extract_item_price_debug(item_page)
                            condition, shipping, image = await _extract_additional_info(item_page)

                            # NEW-only safety check (should already be filtered by search)
                            if condition and not any(
                                k in condition.lower()
                                for k in ['new', 'new with', 'new without', 'new with tags']
                            ):
                                print(f"⏩ Skipping non-new item (condition: {condition})")
                                continue

                            # Fallbacks from search card
                            search_price_text = item.get("price_text") or ""
                            search_shipping_text = item.get("shipping_text") or ""
                            search_condition = item.get("condition") or ""

                            if price_gbp is None and search_price_text:
                                parsed = _parse_price_to_gbp(search_price_text)
                                if parsed is not None:
                                    price_gbp = parsed
                                    print(f"🔄 Using search result price: £{price_gbp}")

                            if not condition and search_condition:
                                condition = search_condition

                            if not shipping and search_shipping_text:
                                shipping = search_shipping_text

                            if not image and item.get("image"):
                                image = item["image"]

                            sold_item = SoldItem(
                                title=item["title"].replace("Opens in a new window or tab", "").strip(),
                                price_text=(f"£{price_gbp:.2f}" if price_gbp is not None else search_price_text or "N/A"),
                                price_gbp=price_gbp,
                                price_usd=_gbp_to_usd(price_gbp, usd_rate),
                                shipping_text=shipping,
                                condition=condition,
                                sold_info=sold_info,
                                url=clean_url,
                                image=image,
                            )

                            all_items.append(asdict(sold_item))
                            print(f"✅ Collected NEW item: {sold_item.title[:80]} | {sold_item.price_text} | {sold_item.condition}")

                        except Exception as e:
                            print(f"❌ Failed item ({item['title'][:80]}): {e}")
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
