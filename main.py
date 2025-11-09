import asyncio
import os
import typing as t
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# Import the high-level entrypoint (run_with_retries aliased as main)
from ebay_sold_itempages import main as run_scrape  # <-- IMPORTANT

app = FastAPI(
    title="FastAPI Scraper",
    version="1.1.0",
    description="Playwright-powered scraper (Railway/Playwright docker).",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://www.certiauth.co.uk",
        "https://certi-admin-dashboard.vercel.app",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {
        "service": "fastapi-scraper",
        "endpoints": {
            "health": "/health",
            "smoke": "/smoke",
            "scrape": "/scrape?query=...&pages=1&per_page=30&headless=true",
        },
        "environment": "production" if os.environ.get("VERCEL") else "development",
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "environment": "production" if os.environ.get("VERCEL") else "development",
    }


@app.get("/smoke")
async def smoke():
    # Quick browser sanity check
    try:
        data = await run_scrape(
            "__SMOKE__",
            pages=1,
            per_page=1,
            headless=True,
            usd_rate=1.28,
            mobile=False,
            smoke=True,
        )
        return {"ok": True, "title": data.get("title", "n/a")}
    except Exception as e:
        # Keep 200 and report failure in body so platforms don't think the service is dead
        return {"ok": False, "error": str(e)}


@app.get("/scrape")
async def scrape(
    query: str = Query(..., min_length=1),
    pages: int = Query(1, ge=1, le=50),
    per_page: int = Query(30, ge=1, le=200),
    headless: bool = True,
    usd_rate: float = Query(1.28, gt=0),
    proxy: t.Optional[str] = None,
    dummy: bool = False,
    mobile: bool = False,
):
    # Vercel-specific tweak kept for compatibility (no effect on Railway unless VERCEL is set)
    if os.environ.get("VERCEL"):
        per_page = min(per_page, 10)
        headless = True

    if dummy:
        return {
            "success": True,
            "items": [],
            "note": "dummy response (no browser launched)",
            "params": {
                "query": query,
                "pages": pages,
                "per_page": per_page,
                "headless": headless,
                "usd_rate": usd_rate,
                "mobile": mobile,
            },
        }

    # Clamp inputs
    pages = max(1, min(pages, 50))
    per_page = max(1, min(per_page, 200))

    # Proxy handling (if you use it)
    old_http = os.environ.get("PLAYWRIGHT_HTTP_PROXY")
    old_https = os.environ.get("PLAYWRIGHT_HTTPS_PROXY")

    try:
        if proxy:
            os.environ["PLAYWRIGHT_HTTP_PROXY"] = proxy
            os.environ["PLAYWRIGHT_HTTPS_PROXY"] = proxy

        # run_scrape is async (run_with_retries), so call directly
        data = await run_scrape(
            query,
            pages=pages,
            per_page=per_page,
            headless=headless,
            usd_rate=usd_rate,
            mobile=mobile,
            smoke=False,
        )

        # If scraper returns nothing, expose as logical error (not HTTP 500)
        if data is None:
            return {
                "success": False,
                "error": "run_scrape returned None",
            }

        # IMPORTANT: do NOT wrap in a 500; just return whatever the scraper says
        # data already contains success/error/count/items.
        return data

    except Exception as exc:
        # Catch unexpected stuff; still respond 200 with error payload
        import traceback

        print("❌ /scrape unhandled error:", exc)
        print(traceback.format_exc())

        return {
            "success": False,
            "error": f"/scrape failed: {type(exc).__name__}: {exc}",
        }

    finally:
        # Restore proxy env vars
        if old_http is not None:
            os.environ["PLAYWRIGHT_HTTP_PROXY"] = old_http
        elif "PLAYWRIGHT_HTTP_PROXY" in os.environ:
            del os.environ["PLAYWRIGHT_HTTP_PROXY"]

        if old_https is not None:
            os.environ["PLAYWRIGHT_HTTPS_PROXY"] = old_https
        elif "PLAYWRIGHT_HTTPS_PROXY" in os.environ:
            del os.environ["PLAYWRIGHT_HTTPS_PROXY"]
