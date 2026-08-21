#!/usr/bin/env python3
"""Smoke-test the admin console in a real browser.

The console is the part of this project with no unit tests and the most room to
break quietly: a render bug doesn't raise, it just silently drops half a view or
throws away what you were typing. These are the properties that actually broke
in practice, asserted against a running instance:

  * every view renders, in both themes, with no console errors;
  * saving a spend limit is reflected immediately, not after a page refresh;
  * a live update never overwrites a field being edited;
  * live updates patch the DOM instead of rebuilding it (nodes keep identity).

    pip install playwright && playwright install chromium
    python scripts/ui-smoke.py http://127.0.0.1:8090

Exits non-zero on the first failure.
"""
from __future__ import annotations

import os
import sys
import time

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8090"
VIEWS = ["overview", "requests", "clients", "upstreams", "settings"]
# Honoured so this can run against a preinstalled browser in CI.
CHROME = os.environ.get("CHROME_PATH") or None

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    if not ok:
        failures.append(name)


with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=CHROME)

    for theme in ("dark", "light"):
        print(f"\n== {theme} theme ==")
        errors: list[str] = []
        page = browser.new_page(viewport={"width": 1500, "height": 980})
        # Bind the current list into the closure; `errors` is rebound each loop.
        page.on("pageerror", lambda e, sink=errors: sink.append(str(e)))
        page.on("console", lambda m, sink=errors: sink.append(f"console.{m.type}: {m.text}")
                if m.type == "error" else None)
        page.add_init_script(f"try{{localStorage.setItem('cp-theme','{theme}')}}catch(e){{}}")
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1500)
        for view in VIEWS:
            page.click(f'#nav button[data-view="{view}"]')
            page.wait_for_timeout(700)
            body = page.locator(f"#view-{view}").inner_text()
            check(f"{view} renders", len(body.strip()) > 40, f"{len(body)} chars")
        check("no JS errors", not errors, "; ".join(dict.fromkeys(errors))[:200])
        page.close()

    print("\n== clients: selection and saving ==")
    errors = []
    page = browser.new_page(viewport={"width": 1500, "height": 980})
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE, wait_until="networkidle")
    page.click('#nav button[data-view="clients"]')
    page.wait_for_timeout(1200)

    rows = page.locator(".cl-row").count()
    check("client list populates", rows > 0, f"{rows} rows")
    if rows:
        name = page.locator(".cl-row").nth(min(1, rows - 1)).get_attribute("data-name")
        page.click(f'.cl-row[data-name="{name}"]')
        page.wait_for_timeout(500)
        check("selection opens the detail pane",
              page.locator(".cl-detail h3").inner_text() == name)
        check("selection is deep-linked", f"clients/{name}" in page.url, page.url)

        # A save must show up without a page refresh — the original complaint.
        worst = 0.0
        for amount in (11, 22, 33):
            page.fill('.lim-input[data-period="hour"]', str(amount))
            t0 = time.time()
            page.click(".cl-section-foot .btn.primary")
            try:
                page.wait_for_function(
                    """(a) => document.querySelector('.cl-detail .badge')?.textContent.includes('$' + a)
                             || document.querySelector('.lim-usage .lim-text')?.textContent.includes('of $' + a)""",
                    arg=f"{amount}.00", timeout=5000)
                worst = max(worst, time.time() - t0)
            except Exception as e:  # noqa: BLE001 - any failure here is a failed check
                check(f"limit ${amount} reflected without refresh", False, str(e)[:80])
                break
            page.wait_for_timeout(400)
        else:
            check("limit saves reflected without refresh", True, f"slowest {worst * 1000:.0f}ms")

        # A live frame must not clobber a field mid-edit.
        page.fill('.lim-input[data-period="day"]', "123.45")
        page.wait_for_timeout(4000)
        check("edits survive live updates",
              page.input_value('.lim-input[data-period="day"]') == "123.45")

        # Nodes must be reused, not rebuilt — this is what "stable" means here.
        page.evaluate("document.querySelector('.cl-row.sel').__probe = 1")
        page.wait_for_timeout(4500)
        check("live updates patch rather than rebuild",
              page.evaluate("document.querySelector('.cl-row.sel')?.__probe === 1"))

        # Clean up so a repeat run starts from the same place.
        page.fill('.lim-input[data-period="hour"]', "")
        page.fill('.lim-input[data-period="day"]', "")
        page.click(".cl-section-foot .btn.primary")
        page.wait_for_timeout(700)

    check("no JS errors", not errors, "; ".join(dict.fromkeys(errors))[:200])
    page.close()
    browser.close()

print()
if failures:
    print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
    sys.exit(1)
print("all UI checks passed")
