#!/usr/bin/env python3
"""Inspect Cinema City booking seat maps and report genuinely adjacent selectable seats.

Consumes state/seen.json produced by watch.py. The checker never clicks a seat,
creates a hold, logs in, or proceeds toward payment. Unknown/ambiguous seat states
are rejected rather than counted as available.
"""
import argparse
import asyncio
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

AVAILABLE_WORDS = {"available", "free", "selectable", "enabled", "vacant"}
UNAVAILABLE_WORDS = {
    "sold", "occupied", "unavailable", "held", "reserved", "blocked",
    "disabled", "broken", "restricted", "taken", "unselectable",
    "nonselectable", "inactive", "locked", "gap", "aisle",
}


def truthy(v: Any) -> bool:
    if isinstance(v, bool): return v
    if isinstance(v, (int, float)): return v != 0
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "available", "enabled", "selectable"}


def scalar(obj: dict, names: list[str]):
    lower = {str(k).lower(): v for k, v in obj.items()}
    for name in names:
        if name.lower() in lower and not isinstance(lower[name.lower()], (dict, list)):
            return lower[name.lower()]
    return None


def unavailable(obj: dict) -> bool:
    # Negative flags always override positive availability.
    for field in [
        "disabled", "isDisabled", "blocked", "isBlocked", "restricted", "isRestricted",
        "unavailable", "isUnavailable", "occupied", "isOccupied", "reserved", "isReserved",
        "held", "isHeld", "broken", "isBroken",
    ]:
        v = scalar(obj, [field])
        if v is not None and truthy(v): return True

    for field in ["enabled", "isEnabled", "selectable", "isSelectable"]:
        v = scalar(obj, [field])
        if v is not None and not truthy(v): return True

    status = scalar(obj, ["status", "availability", "seatStatus", "state", "displayStatus"])
    if status is not None:
        text = re.sub(r"[^a-z]", "", str(status).lower())
        if any(word in text for word in UNAVAILABLE_WORDS): return True
    return False


def is_available(obj: dict) -> bool:
    if unavailable(obj): return False
    explicit = scalar(obj, ["available", "isAvailable"])
    if explicit is not None: return truthy(explicit)
    status = scalar(obj, ["status", "availability", "seatStatus", "state", "displayStatus"])
    if status is not None:
        text = re.sub(r"[^a-z]", "", str(status).lower())
        return any(word in text for word in AVAILABLE_WORDS)
    # Safety rule: unknown is unavailable.
    return False


def special_only(obj: dict) -> bool:
    for field in [
        "wheelchairOnly", "isWheelchairOnly", "wheelchair", "companionOnly",
        "isCompanionOnly", "companion", "obstructed", "isObstructed",
    ]:
        v = scalar(obj, [field])
        if v is not None and truthy(v): return True
    kind = scalar(obj, ["type", "seatType", "category", "description", "label"])
    text = str(kind or "").lower()
    return any(x in text for x in ["wheelchair", "companion", "invalid", "broken", "obstruct", "gap", "aisle"])


def walk(value: Any, inherited: dict | None = None):
    inherited = inherited or {}
    if isinstance(value, list):
        for item in value: yield from walk(item, inherited)
        return
    if not isinstance(value, dict): return
    row = scalar(value, ["row", "rowName", "rowLabel"])
    area = scalar(value, ["area", "areaName", "section", "sectionName"])
    context = dict(inherited)
    if row is not None: context["row"] = row
    if area is not None: context["area"] = area
    number = scalar(value, ["seatNumber", "seatNo", "number", "seatLabel", "label"])
    x = scalar(value, ["x", "column", "columnIndex", "position", "seatIndex"])
    if number is not None and (row is not None or context.get("row") is not None):
        yield value, context, number, x
    for child in value.values():
        if isinstance(child, (dict, list)): yield from walk(child, context)


def normalise(payload: Any):
    seats = []
    for obj, ctx, number, x in walk(payload):
        if not is_available(obj) or special_only(obj): continue
        row = str(scalar(obj, ["row", "rowName", "rowLabel"]) or ctx.get("row") or "")
        area = str(scalar(obj, ["area", "areaName", "section", "sectionName"]) or ctx.get("area") or "")
        if not row: continue
        try: xpos = float(x) if x is not None else None
        except (TypeError, ValueError): xpos = None
        seats.append({"area": area, "row": row, "number": str(number), "x": xpos})
    return seats


def numeric(s: str):
    m = re.search(r"\d+", s)
    return int(m.group()) if m else None


def find_pair(seats):
    rows = defaultdict(list)
    for s in seats: rows[(s["area"], s["row"])].append(s)
    for _, group in rows.items():
        group.sort(key=lambda s: (s["x"] is None, s["x"] if s["x"] is not None else numeric(s["number"]) or 9999))
        for a, b in zip(group, group[1:]):
            # Prefer physical coordinates. Without them require consecutive seat numbers.
            if a["x"] is not None and b["x"] is not None:
                adjacent = 0 < b["x"] - a["x"] <= 1.5
            else:
                na, nb = numeric(a["number"]), numeric(b["number"])
                adjacent = na is not None and nb is not None and abs(nb - na) == 1
            if adjacent: return a, b
    return None


async def inspect_event(browser, event, diagnostic=False):
    context = await browser.new_context(locale="cs-CZ", timezone_id="Europe/Prague")
    page = await context.new_page()
    payloads = []

    async def capture(response):
        try:
            ct = response.headers.get("content-type", "").lower()
            if "json" in ct:
                data = await response.json()
                if normalise(data): payloads.append((response.url, data))
        except Exception:
            pass
    page.on("response", capture)
    try:
        await page.goto(event["booking"], wait_until="domcontentloaded", timeout=60000)
        # Guest flow may expose a continue button before the map. Click only generic
        # navigation controls; never elements that look like seats.
        for pattern in [r"pokračovat jako host", r"continue as guest", r"pokračovat", r"continue"]:
            try:
                btn = page.get_by_role("button", name=re.compile(pattern, re.I)).first
                if await btn.count() and await btn.is_visible():
                    await btn.click(timeout=3000)
                    await page.wait_for_timeout(1500)
                    break
            except Exception:
                pass
        await page.wait_for_timeout(4000)

        seats = []
        for _, data in payloads: seats.extend(normalise(data))

        # DOM fallback. Disabled/blocked/occupied classes and ancestors are rejected.
        if not seats:
            raw = await page.locator('[data-seat], [data-seat-number], button[aria-label*="seat" i], [role="button"][aria-label*="seat" i]').evaluate_all("""els => els.map(e => ({
                number: e.dataset.seatNumber || e.dataset.seat || e.getAttribute('data-seat-number') || e.getAttribute('aria-label') || '',
                row: e.dataset.row || e.getAttribute('data-row') || '',
                x: e.dataset.column || e.getAttribute('data-column') || null,
                disabled: !!e.disabled || e.getAttribute('aria-disabled') === 'true' || e.hasAttribute('disabled') ||
                  /disabled|unavailable|blocked|occupied|reserved|held/.test(String(e.className).toLowerCase()) ||
                  !!e.closest('[aria-disabled="true"], .disabled, .unavailable, .blocked, .occupied, .reserved, .held'),
                wheelchairOnly: /wheelchair|companion|invalid|broken|obstruct/.test((e.getAttribute('aria-label') || '').toLowerCase()),
                available: true,
                selectable: !(!!e.disabled || e.getAttribute('aria-disabled') === 'true')
            }))""")
            seats = normalise(raw)

        pair = find_pair(seats)
        if diagnostic:
            Path("artifacts").mkdir(exist_ok=True)
            await page.screenshot(path=f"artifacts/{event['id']}.png", full_page=True)
            Path(f"artifacts/{event['id']}.json").write_text(json.dumps({"event": event, "seats": seats, "pair": pair}, ensure_ascii=False, indent=2), encoding="utf-8")
        return pair
    finally:
        await context.close()


def load(path, default):
    try: return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError: return default


def gh_output(**values):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in values.items(): f.write(f"{k}={v}\n")


async def main_async(args):
    schedule = load(args.schedule_state, {"events": {}}).get("events", {})
    previous = load(args.seat_state, {"events": {}}).get("events", {})
    current = {}
    news = []
    now = datetime.now().isoformat()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            for eid, event in sorted(schedule.items(), key=lambda kv: kv[1].get("datetime", "")):
                if event.get("soldOut") or event.get("datetime", "") <= now: continue
                try: pair = await inspect_event(browser, event, args.diagnostic)
                except Exception as exc:
                    print(f"::warning::Seat check {eid} failed: {exc}")
                    continue
                rec = {"available": bool(pair), "checked": now, "pair": pair}
                current[eid] = rec
                if pair and not previous.get(eid, {}).get("available"):
                    news.append((event, pair))
        finally:
            await browser.close()

    Path(args.seat_state).parent.mkdir(parents=True, exist_ok=True)
    Path(args.seat_state).write_text(json.dumps({"updated": now, "events": current}, ensure_ascii=False, indent=2), encoding="utf-8")
    if not news:
        gh_output(has_news="false")
        print("Žádná nová dvojice sousedních sedadel.")
        return
    lines = ["### Nalezena 2 sousední volná sedadla", ""]
    for event, pair in news:
        a, b = pair
        lines += [f"- **{event['cinema']}** — {event['datetime']} · {event['auditorium']}",
                  f"  - řada **{a['row']}**, sedadla **{a['number']} + {b['number']}**",
                  f"  - [Otevřít rezervaci]({event['booking']})"]
    Path(args.report).write_text("\n".join(lines) + "\n", encoding="utf-8")
    Path(args.title).write_text("🎟️ Odyssea IMAX: 2 sousední sedadla\n", encoding="utf-8")
    gh_output(has_news="true")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--schedule-state", default="state/seen.json")
    ap.add_argument("--seat-state", default="state/seats.json")
    ap.add_argument("--report", default="seat-report.md")
    ap.add_argument("--title", default="seat-title.txt")
    ap.add_argument("--diagnostic", action="store_true")
    args = ap.parse_args()
    asyncio.run(main_async(args))

if __name__ == "__main__": main()
