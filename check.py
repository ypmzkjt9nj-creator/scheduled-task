"""Scheduled page watcher.

Opens a target URL in a headless browser, steps through a short form,
reaches the time page, and reports through ntfy when an opening exists.
All configuration comes from environment variables. Nothing is hard-coded.

Detection rule: click "first available time". If the page still shows the
"no available times could be found" message, there is nothing. If that
message is absent while still on the time page, an opening exists.
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

# Phrases that mean "nothing available". Matched only inside the results
# panel (after the "available times" heading) so the page's help text, which
# always mentions "no available appointments", never false-triggers.
EMPTY_PATTERNS = [
    "could be found",
    "no available",
    "no times",
    "no appointments",
    "no slots",
    "none available",
    "not available",
    "fully booked",
    "no free",
]
# Used only if the results heading cannot be located. Conservative: none of
# these appear in the page's help text.
EMPTY_PATTERNS_NARROW = [
    "could be found",
    "no available times",
    "no times available",
]
RESULTS_HEADER = "available times"
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
    return hashlib.sha256("|".join(items).encode("utf-8")).hexdigest()


# --- generic form navigation -----------------------------------------------

FORWARD = re.compile(
    r"(appointment|next|continue|forward|proceed|confirm|search|show|select|choose|»|>)",
    re.I,
)
GRID_HINT = re.compile(
    r"\b(mon|tue|wed|thu|fri|sat|sun)\b|"
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|"
    r"((january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+\d{4})",
    re.I,
)


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


def select_radios(page):
    """Select the first option in each radio group that has none chosen."""
    radios = page.locator("input[type=radio]")
    handled = set()
    for i in range(radios.count()):
        r = radios.nth(i)
        try:
            name = r.get_attribute("name") or ""
            key = name or f"_{i}"
            if key in handled:
                continue
            handled.add(key)
            if name:
                group = page.locator(f'input[type=radio][name="{name}"]')
                count = group.count()
                if any(group.nth(j).is_checked() for j in range(count)):
                    continue
                target = group.first
            else:
                if r.is_checked():
                    continue
                target = r
            if target.is_visible():
                target.check(force=True, timeout=3000)
        except Exception:
            continue


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
        select_radios(page)
        if looks_like_grid(page):
            print(f"reached calendar at step {step}")
            return True
        if not click_forward(page):
            print(f"no forward control at step {step}")
            break
    return looks_like_grid(page)


# --- availability -----------------------------------------------------------

def click_first_available(page):
    for loc in [
        page.get_by_role("button", name=re.compile(r"first available", re.I)),
        page.get_by_role("link", name=re.compile(r"first available", re.I)),
    ]:
        try:
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=8000)
                page.wait_for_load_state("networkidle", timeout=25000)
                return True
        except Exception:
            continue
    return False


def current_date(page):
    inputs = page.locator("input")
    for i in range(min(inputs.count(), 50)):
        try:
            v = inputs.nth(i).get_attribute("value") or ""
            if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", v):
                return v
        except Exception:
            continue
    return ""


def collect_slots(page):
    """Best-effort read of bookable times (clickable HH:MM). May be empty."""
    found = []
    for sel in ["a", "button", "input[type=submit]", "input[type=button]", "[onclick]", "[role=button]"]:
        loc = page.locator(sel)
        for i in range(min(loc.count(), 300)):
            el = loc.nth(i)
            try:
                if not el.is_visible():
                    continue
                txt = (el.inner_text() or "").strip() or (el.get_attribute("value") or "").strip()
                if re.fullmatch(r"\d{1,2}:\d{2}", txt):
                    found.append(txt)
            except Exception:
                continue
    return sorted(set(found))


def is_empty_message(body):
    """True only when a 'nothing available' phrase sits in the results panel."""
    low = (body or "").lower()
    idx = low.rfind(RESULTS_HEADER)
    if idx == -1:
        return any(p in low for p in EMPTY_PATTERNS_NARROW)
    tail = low[idx:]
    return any(p in tail for p in EMPTY_PATTERNS)


def assess(page):
    """Return (available, empty_message_shown, earliest_date, times)."""
    click_first_available(page)
    try:
        body = page.inner_text("body")
    except Exception:
        body = ""
    on_grid = bool(GRID_HINT.search(body))
    times = collect_slots(page)
    empty = is_empty_message(body)
    # Alert unless the results panel positively says there is nothing.
    # A detected bookable time forces availability regardless of wording.
    available = on_grid and (bool(times) or not empty)
    return available, empty, current_date(page), times


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

        if not reached and not debug:
            print("did not reach calendar")
            browser.close()
            sys.exit(1)

        available, empty, dv, times = (False, True, "", [])
        if reached:
            available, empty, dv, times = assess(page)

        if debug:
            try:
                page.screenshot(path="/tmp/view.png", full_page=True)
                notify_file("/tmp/view.png")
            except Exception as e:
                print(f"debug capture failed: {e}")
            notify(
                "debug",
                f"reached: {'yes' if reached else 'no'}\n"
                f"no-slots message: {'shown' if empty else 'absent'}\n"
                f"would alert: {'no' if not available else 'YES'}\n"
                f"earliest: {dv or 'n/a'}\n"
                f"times: {', '.join(times) if times else 'none'}",
                priority="default",
                tags="eye",
            )
            browser.close()
            return

        browser.close()

    if not available:
        print("nothing found")
        save_sig("")
        return

    detail = "Availability found."
    if dv:
        detail += f"\nEarliest: {dv}"
    if times:
        detail += f"\nTimes: {', '.join(times)}"
    detail += "\n\nTap to open and book."

    sig = sig_of([dv] + times) if (dv or times) else "available"
    if sig == load_prev():
        print("unchanged")
        return

    notify("Availability found", detail, priority="urgent", tags="rotating_light")
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
