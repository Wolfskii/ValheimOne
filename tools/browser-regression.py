#!/usr/bin/env python3
"""Read-only public-map checks against an explicitly supplied native fixture."""
import json
import os
from pathlib import Path
import sys
from playwright.sync_api import sync_playwright

if os.environ.get('ATLAS_QA_REMOTE') != '1':
    raise SystemExit('Run this check on the authorized remote browser worker.')
base = sys.argv[1].rstrip('/')
out = Path(os.environ.get('ATLAS_QA_ARTIFACT_OUT', 'artifacts/browser-regression'))
out.mkdir(parents=True, exist_ok=True)
results = []

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    try:
        for width in (1280, 390):
            context = browser.new_context(viewport={'width': width, 'height': 900})
            context.add_init_script("localStorage.setItem('vo-livemap-layers-v2', JSON.stringify({fog:false}));")
            # Exercise the native HTTP polling fallback so later status changes
            # can be injected without writing server configuration.
            context.add_init_script("window.EventSource = undefined;")
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            response = page.goto(base, wait_until='domcontentloaded')
            assert response.status == 200, 'Native map HTML must load'
            page.wait_for_function("document.querySelector('.fog-overlay')?.naturalWidth > 0", timeout=120000)
            assert page.locator('input[data-layer-key="fog"]').count() == 0, 'Required fog must have no disable control'
            assert page.locator('.fog-overlay').is_visible(), 'Saved fog=false must not disable required fog'
            assert page.locator('.map-cover:not(.is-hidden)').count() == 0, 'Loaded fog must release the loading cover'
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), 'Page must fit its viewport'
            page.screenshot(path=str(out / f'public-locked-{width}.png'))
            # Only the change notification is simulated below; initial HTML,
            # JavaScript, status, tiles and fog came from the actual native mod.
            locked = [False]
            def status_change(route):
                real = route.fetch()
                payload = real.json()
                payload['map']['fog']['hide'] = locked[0]
                route.fulfill(response=real, json=payload)
            page.route('**/api/status*', status_change)
            page.wait_for_selector('input[data-layer-key="fog"]', state='attached', timeout=30000)
            assert not page.locator('input[data-layer-key="fog"]').is_checked(), 'Unlock restores the saved preference'
            locked[0] = True
            page.wait_for_function("!document.querySelector('input[data-layer-key=fog]')", timeout=30000)
            page.wait_for_function("document.querySelector('.fog-overlay')?.naturalWidth > 0", timeout=30000)
            assert page.locator('.fog-overlay').is_visible(), 'Relocking restores required fog'
            assert errors == [], errors
            results.append({'width': width, 'native_locked_map': 'pass', 'simulated_lock_changes': 'pass'})
            context.close()

        context = browser.new_context(viewport={'width': 1280, 'height': 900})
        page = context.new_page()
        page.route('**/fog.png*', lambda route: route.abort())
        page.goto(base, wait_until='domcontentloaded')
        page.wait_for_selector('.map-cover:not(.is-hidden)', state='attached', timeout=30000)
        page.wait_for_timeout(9000)
        assert page.locator('.map-cover:not(.is-hidden)').is_visible(), 'Failed required fog must remain covered after eight seconds'
        page.screenshot(path=str(out / 'required-fog-network-failure.png'))
        results.append({'injected_fog_network_failure': 'pass'})
        context.close()
    finally:
        browser.close()
        (out / 'result.json').write_text(json.dumps(results, indent=2))
print(json.dumps(results))
