"""Scheduled page watcher.

Opens a target URL in a headless browser, steps through a short form,
and reports through ntfy when the resulting grid shows selectable items.
All configuration comes from environment variables. Nothing is hard-coded.
"""

import os
import re
import sys
import json
import hashlib
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

TARGET_URL = os.environ.get("TARGET_URL", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
RUN_MODE = os.environ.get("RUN_MODE", "check").strip().lower()

SCAN_STEPS = 3            # current view plus two forward steps
STATE_FILE = Path("state.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# --- notifications ---------------------------------------------------------

def notify(title, message, priority="high", tags="bell"):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set; skipping send.")
        return
    headers = {
        "Title": title.encode("ascii", "ignore").decode(),
        "Priority": priority,
        "Tags": tags,
    }
    if TARGET_URL:
        headers["Click"] = TARGET_URL
    try:
        r = requests.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=30,
        )
        print(f"send status: {r.status_code}")
    except Exception as e:
        print(f"send error: {e}")


def notify_file(path, title="debug view"):
    """Send an image privately to the ntfy topic (used by debug mode only)."""
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set; skipping file send.")
        return
    try:
        with open(path, "rb") as f:
            r = requests.put(
                f"{NTFY_SERVER}/{NTFY_TOPIC}",
                data=f,
                headers={"Filename": os.path.basename(path), "Title": title},
                timeout=60,
            )
        print(f"file send status: {r.status_code}")
    except Exception as e:
        print(f"file send error: {e}")


# --- state (change detection, hash only) -----------------------------------

def load_prev():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text()).get("sig", "")
        except Exception:
            return ""
    return ""


def save_sig(sig):
    STATE_FILE.write_text(json.dumps({"sig": sig}))


def sig_of(items):
    return hashlib.sha256("|".join(sorted(items)).encode("utf-8")).hexdigest()


# --- generic form / grid navigation ----------------------------------------

FORWARD = re.compile(r"(new|next|continue|forward|proceed|search|show|go|»|>)", re.I)
GRID_HINT = re.compile(
    r"(mon|tue|wed|thu|fri|sat|sun|january|february|march|april|may|june|"
    r"july|august|september|october|november|december|available)",
    re.I,
)
EMPTY_HINT = re.compile(
    r"(no available|no times|none available|fully booked|not available|no appointments)",
    re.I,
)
MATCH_HINT = re.compile(r"(available|selectable|bookable|free)", re.I)


def switch_to_english(page):
    try:
        link = page.get_by_role("link", name=re.compile(r"english", re.I))
        if link.count() > 0 and link.first.is_visible():
            link.first.click(timeout=5000)
            page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass


def check_all_checkboxes(page):
    boxes = page.locator("input[type=checkbox]")
    for i in range(boxes.count()):
        cb = boxes.nth(i)
        try:
            if cb.is_visible() and not cb.is_checked():
                cb.check(force=True, timeout=3000)
        except Exception:
            pass


def set_first_count_to_one(page):
    selects = page.locator("select")
    for i in range(selects.count()):
        sel = selects.nth(i)
        try:
            key = (sel.get_attribute("name") or "") + (sel.get_attribute("id") or "")
            if re.search(r"(person|people|number|count|qty|adult)", key, re.I):
                sel.select_option("1")
        except Exception:
            pass


def looks_like_grid(page):
    try:
        return bool(GRID_HINT.search(page.inner_text("body")))
    except Exception:
        return False


def click_forward(page):
    for loc in [
        page.get_by_role("button", name=FORWARD),
        page.locator("input[type=submit]"),
        page.get_by_role("link", name=FORWARD),
    ]:
        try:
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=5000)
                page.wait_for_load_state("networkidle", timeout=20000)
                return True
        except Exception:
            continue
    return False


def reach_grid(page):
    for step in range(6):
        set_first_count_to_one(page)
        check_all_checkboxes(page)
        if looks_like_grid(page):
            print(f"reached grid at step {step}")
            return True
        if not click_forward(page):
            print(f"no forward control at step {step}")
            break
    return looks_like_grid(page)


# --- match extraction (most likely to need one tuning pass) ----------------

def view_label(page):
    try:
        text = page.inner_text("body")
    except Exception:
        return ""
    m = re.search(
        r"((january|february|march|april|may|june|july|august|september|"
        r"october|november|december)\s+\d{4})",
        text,
        re.I,
    )
    return m.group(1) if m else ""


def extract_matches(page):
    try:
        body = page.inner_text("body")
    except Exception:
        body = ""
    if EMPTY_HINT.search(body):
        return []
    found = []
    for selector in [
        "table a", "table button", "td a",
        "[class*='calendar'] a", "[class*='calendar'] button", "[class*='day'] a",
    ]:
        loc = page.locator(selector)
        for i in range(min(loc.count(), 80)):
            el = loc.nth(i)
            try:
                if not el.is_visible():
                    continue
                label = (el.inner_text() or "").strip()
                cls = el.get_attribute("class") or ""
                if re.fullmatch(r"\d{1,2}", label) or MATCH_HINT.search(cls):
                    if label and label not in found:
                        found.append(label)
            except Exception:
                continue
    return found


def scan(page):
    items = []
    for _ in range(SCAN_STEPS):
        label = view_label(page)
        matches = extract_matches(page)
        if matches:
            items.append(f"{label}: {', '.join(matches)}" if label else ", ".join(matches))
        moved = False
        for loc in [
            page.get_by_role("link", name=re.compile(r"(next|forward|»|>)", re.I)),
            page.get_by_role("button", name=re.compile(r"(next|forward|»|>)", re.I)),
        ]:
            try:
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=5000)
                    page.wait_for_load_state("networkidle", timeout=20000)
                    moved = True
                    break
            except Exception:
                continue
        if not moved:
            break
    return items


# --- run modes -------------------------------------------------------------

def run_check(debug=False):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=USER_AGENT, locale="en-GB")
        page = ctx.new_page()
        page.set_default_timeout(30000)

        page.goto(TARGET_URL, wait_until="networkidle")
        switch_to_english(page)
        reached = reach_grid(page)

        if debug:
            shot = "/tmp/view.png"
            try:
                page.screenshot(path=shot, full_page=True)
                notify_file(shot)
            except Exception as e:
                print(f"debug capture failed: {e}")
            browser.close()
            return

        if not reached:
            print("did not reach grid")
            browser.close()
            sys.exit(1)

        items = scan(page)
        browser.close()

    if not items:
        print("nothing found")
        save_sig("")
        return

    sig = sig_of(items)
    if sig == load_prev():
        print("unchanged")
        return

    message = "Availability found:\n\n" + "\n".join(items) + "\n\nTap to open."
    notify("Availability found", message, priority="urgent", tags="rotating_light")
    save_sig(sig)
    print("notified")


def run_test():
    notify(
        "Test",
        "Test alert. Seeing this on phone and desktop means it works.",
        priority="default",
        tags="white_check_mark",
    )
    print("test sent")


if __name__ == "__main__":
    if not TARGET_URL and RUN_MODE != "test":
        print("TARGET_URL not set")
        sys.exit(1)
    if RUN_MODE == "test":
        run_test()
    elif RUN_MODE == "debug":
        run_check(debug=True)
    else:
        run_check()
