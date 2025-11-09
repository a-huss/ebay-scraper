import os
import typing as t
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from ebay_sold_itempages import main as run_scrape  # uses run_with_retries

app = FastAPI(
    title="FastAPI Scraper",
    version="1.1.0",
    description="Playwright-powered scraper (Railway / Docker).",
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
    """
    Quick browser sanity check.
    Uses run_scrape(..., smoke=True) which hits example.com.
    """
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
    """
    Public scraping endpoint.
    Always returns JSON with success flag; avoids raw 500s from scraper.
    """

    # Vercel-specific tweak (no effect on Railway unless VERCEL env is set)
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

    # Clamp
    pages = max(1, min(pages, 50))
    per_page = max(1, min(per_page, 200))

    # Optional proxy support
    old_http = os.environ.get("PLAYWRIGHT_HTTP_PROXY")
    old_https = os.environ.get("PLAYWRIGHT_HTTPS_PROXY")

    try:
        if proxy:
            os.environ["PLAYWRIGHT_HTTP_PROXY"] = proxy
            os.environ["PLAYWRIGHT_HTTPS_PROXY"] = proxy

        # run_scrape is async (run_with_retries)
        data = await run_scrape(
            query,
            pages=pages,
            per_page=per_page,
            headless=headless,
            usd_rate=usd_rate,
            mobile=mobile,
            smoke=False,
        )

        if data is None:
            return {
                "success": False,
                "error": "run_scrape returned None",
            }

        # Do NOT convert this into HTTP 500; just return structured JSON
        return data

    except Exception as exc:
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
