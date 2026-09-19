# backend/main.py
import base64
import time
import traceback
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from playwright.sync_api import sync_playwright

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

AXE_CDN = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.9.1/axe.min.js"

class ScanRequest(BaseModel):
    url: str

@app.post("/scan")
def scan(req: ScanRequest):  # note: plain def, not async def
    url = req.url.strip()
    if not url.startswith("http"):
        url = "https://" + url

    started = time.time()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 800})

            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            title = page.title()

            violations = []
            try:
                page.add_script_tag(url=AXE_CDN)
                page.wait_for_timeout(500)
                raw = page.evaluate("async () => { const r = await axe.run(); return JSON.parse(JSON.stringify(r)); }")
                for v in raw.get("violations", []):
                    for node in v.get("nodes", []):
                        violations.append({
                            "id": v.get("id"),
                            "impact": v.get("impact") or "minor",
                            "title": v.get("help"),
                            "description": v.get("description"),
                            "wcag": ", ".join(t.upper() for t in v.get("tags", []) if t.startswith("wcag")) or "Best Practice",
                            "selector": (node.get("target") or ["unknown"])[0],
                            "helpUrl": v.get("helpUrl"),
                        })
            except Exception as axe_err:
                print("axe-core step failed:", axe_err)

            screenshot_bytes = page.screenshot(full_page=False)
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

            browser.close()

        return {
            "url": url,
            "title": title,
            "screenshot": f"data:image/png;base64,{screenshot_b64}",
            "violations": violations,
            "scanTimeMs": round((time.time() - started) * 1000),
        }

    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=400, content={"detail": f"Scan failed: {str(e)}"})