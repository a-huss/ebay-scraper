# api_server.py
import asyncio
import os
import typing as t
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from ebay_sold_itempages import run as run_scrape  # noqa: E402

app = FastAPI(
    title="FastAPI Scraper",
    version="1.1.0",
    description="Playwright-powered scraper (Vercel-deployable).",
)

# Add CORS middleware for production
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://www.certiauth.co.uk",           # Your custom domain
        "https://certi-admin-dashboard.vercel.app",  # Your Vercel domain
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
        "environment": "production" if os.environ.get("VERCEL") else "development"
    }


@app.get("/health")
def health():
    return {
        "status": "ok", 
        "environment": "production" if os.environ.get("VERCEL") else "development"
    }


@app.get("/smoke")
async def smoke():
    # quick browser sanity check w/ example.com
    try:
        data = await run_scrape("__SMOKE__", pages=1, per_page=1, headless=True, usd_rate=1.28, mobile=True, smoke=True)
        return {"ok": True, "title": data.get("title", "n/a")}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


async def _call_run_scrape(
    query: str,
    *,
    pages: int,
    per_page: int,
    headless: bool,
    usd_rate: float,
    mobile: bool,
) -> t.Any:
    if asyncio.iscoroutinefunction(run_scrape):
        return await run_scrape(
            query, pages=pages, headless=headless, usd_rate=usd_rate, per_page=per_page, mobile=mobile
        )

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: run_scrape(
            query,
            pages=pages,
            headless=headless,
            usd_rate=usd_rate,
            per_page=per_page,
            mobile=mobile,
        ),
    )


@app.get("/scrape")
async def scrape(
    query: str = Query(..., min_length=1),
    pages: int = Query(1, ge=1, le=50),
    per_page: int = Query(30, ge=1, le=200),
    headless: bool = True,
    usd_rate: float = Query(1.28, gt=0),
    proxy: t.Optional[str] = None,
    dummy: bool = False,
    mobile: bool = False,  # Changed to False for better desktop scraping
):
    # Add Vercel-specific optimizations
    if os.environ.get("VERCEL"):
        # Production optimizations for Vercel
        per_page = min(per_page, 10)  # Reduce items in production to avoid timeouts
        headless = True  # Force headless in production
    
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

    pages = max(1, min(pages, 50))
    per_page = max(1, min(per_page, 200))

    old_http = os.environ.get("PLAYWRIGHT_HTTP_PROXY")
    old_https = os.environ.get("PLAYWRIGHT_HTTPS_PROXY")

    try:
        if proxy:
            os.environ["PLAYWRIGHT_HTTP_PROXY"] = proxy
            os.environ["PLAYWRIGHT_HTTPS_PROXY"] = proxy

        data = await _call_run_scrape(
            query,
            pages=pages,
            per_page=per_page,
            headless=headless,
            usd_rate=usd_rate,
            mobile=mobile,
        )
        if data is None:
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "run_scrape returned None"},
            )
        return JSONResponse(content=data)

    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(exc)},
        )
    finally:
        if old_http is not None:
            os.environ["PLAYWRIGHT_HTTP_PROXY"] = old_http
        elif "PLAYWRIGHT_HTTP_PROXY" in os.environ:
            del os.environ["PLAYWRIGHT_HTTP_PROXY"]

        if old_https is not None:
            os.environ["PLAYWRIGHT_HTTPS_PROXY"] = old_https
        elif "PLAYWRIGHT_HTTPS_PROXY" in os.environ:
            del os.environ["PLAYWRIGHT_HTTPS_PROXY"]