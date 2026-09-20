# backend/main.py
import base64
import time
import traceback
import os
import json
import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from playwright.sync_api import sync_playwright

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://jarvissgo.netlify.app", "http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

AXE_CDN = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.9.1/axe.min.js"

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "openai/gpt-oss-120b"


class ScanRequest(BaseModel):
    url: str


@app.post("/scan")
def scan(req: ScanRequest):
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


class AgentRunRequest(BaseModel):
    url: str
    goal: str


def get_interactive_elements(page):
    return page.evaluate("""
    () => {
      const selectors = 'a, button, input, textarea, select, [role="button"], [role="link"]';
      const els = Array.from(document.querySelectorAll(selectors));
      const visible = els.filter(el => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && r.top < window.innerHeight && r.top > -50;
      }).slice(0, 30);
      visible.forEach((el, i) => el.setAttribute('data-agent-index', String(i)));
      return visible.map((el, i) => {
        const r = el.getBoundingClientRect();
        return {
          index: i,
          tag: el.tagName.toLowerCase(),
          text: (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || '').trim().slice(0, 60),
          inputType: el.getAttribute('type') || '',
          bbox: { x: r.x, y: r.y, width: r.width, height: r.height }
        };
      });
    }
    """)


def ask_llm_next_action(goal, url, elements, history):
    elements_text = "\n".join(
        f'[{e["index"]}] <{e["tag"]}{" type=" + e["inputType"] if e["inputType"] else ""}> "{e["text"]}"'
        for e in elements
    )
    history_text = "\n".join(
        f'Step {i+1}: {h["action"]} on [{h.get("index", "-")}] {h.get("text", "")}'
        for i, h in enumerate(history)
    ) or "None yet."

    prompt = f"""You are a web-browsing agent. Task: "{goal}"
Current page: {url}

Visible interactive elements:
{elements_text}

Actions taken so far:
{history_text}

Decide the SINGLE next action to progress toward the task. Respond with ONLY raw JSON, no markdown, no explanation:
{{"action": "click" | "type" | "done", "index": <element index or null>, "text": "<text to type, or null>", "reasoning": "<one short sentence>"}}

Use "done" as soon as the task appears complete, or if no useful element exists. Pick "type" only for input/textarea elements, then follow it with "click" on a submit/search button in a later step if needed."""

    resp = requests.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 300,
        },
        timeout=20,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()
    content = content.strip("`").replace("json\n", "").strip()
    return json.loads(content)


@app.post("/agent-run")
def agent_run(req: AgentRunRequest):
    if not GROQ_API_KEY:
        return JSONResponse(status_code=500, content={"detail": "GROQ_API_KEY not set on server."})

    url = req.url.strip()
    if not url.startswith("http"):
        url = "https://" + url

    MAX_STEPS = 8
    history = []
    screenshots = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(url, wait_until="domcontentloaded", timeout=20000)

            for step in range(MAX_STEPS):
                elements = get_interactive_elements(page)
                shot = base64.b64encode(page.screenshot(full_page=False)).decode("utf-8")
                screenshots.append(f"data:image/png;base64,{shot}")

                try:
                    decision = ask_llm_next_action(req.goal, page.url, elements, history)
                except Exception as e:
                    history.append({"action": "error", "reasoning": f"LLM decision failed: {e}", "error": str(e)})
                    break

                action = decision.get("action")
                idx = decision.get("index")
                text = decision.get("text")
                reasoning = decision.get("reasoning", "")

                if action == "done":
                    history.append({"action": "done", "reasoning": reasoning})
                    break

                matched = next((e for e in elements if e["index"] == idx), None)
                bbox = matched["bbox"] if matched else None

                try:
                    target = page.locator(f'[data-agent-index="{idx}"]')
                    if action == "click":
                        target.click(timeout=5000)
                    elif action == "type" and text:
                        target.fill(text, timeout=5000)
                    history.append({"action": action, "index": idx, "text": text, "reasoning": reasoning, "bbox": bbox})
                    page.wait_for_timeout(1200)
                except Exception as e:
                    history.append({"action": action, "index": idx, "text": text, "reasoning": reasoning, "bbox": bbox, "error": str(e)})
                    break

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
                print("final axe-core scan failed:", axe_err)

            final_shot = base64.b64encode(page.screenshot(full_page=False)).decode("utf-8")
            final_url = page.url
            browser.close()

        goal_completed = any(h.get("action") == "done" for h in history)
        errored = any(h.get("error") for h in history)

        return {
            "goal": req.goal,
            "startUrl": url,
            "finalUrl": final_url,
            "history": history,
            "screenshots": screenshots,
            "finalScreenshot": f"data:image/png;base64,{final_shot}",
            "stepsTaken": len(history),
            "goalCompleted": goal_completed,
            "errored": errored,
            "violations": violations,
        }

    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=400, content={"detail": f"Agent run failed: {str(e)}"})