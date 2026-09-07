"""Optional browser checks: run with Playwright and its Chromium installed.

All HTTP requests use local web assets and synthetic agent data.
Usage: uv run --with playwright python tests/check_terminal_layout.py
"""

import asyncio
import json
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

from playwright.async_api import async_playwright


WEB = Path(__file__).resolve().parents[1] / "web"


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            context = await browser.new_context(
                viewport={"width": 393, "height": 852},
                is_mobile=True, has_touch=True, device_scale_factor=1,
                service_workers="block",
            )

            async def asset(route):
                path = WEB / (urlsplit(route.request.url).path.lstrip("/") or "index.html")
                if path.resolve().is_relative_to(WEB) and path.is_file():
                    await route.fulfill(path=str(path))
                    return
                await route.abort()

            await context.route("**/*", asset)
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto("https://terminal.example/", wait_until="load")
            await page.evaluate("document.fonts.ready")
            await page.evaluate("""() => {
              agents = [{pane_id: 'demo:workspace:p1', session_name: 'demo',
                herdr_pane_id: 'p1', project: 'Terminal layout test', agent: 'codex', status: 'working'}];
              ws = {readyState: 1, send() {}};
              document.getElementById('agentListView').innerHTML = agentCard(agents[0]);
            }""")
            await page.locator('[data-pane-id="demo:workspace:p1"]').click()
            await page.wait_for_function("document.body.classList.contains('terminal-open')")
            await page.evaluate("""() => handleMessage({type: 'pane_content', pane_id: 'demo:workspace:p1',
              content: '\\x1b[32mTerminale di prova\\x1b[0m\\n' +
                Array.from({length: 30}, (_, i) => `${i + 1}. Output leggibile sul dispositivo: ` +
                  'parole lunghe e informazioni della sessione '.repeat(4)).join('\\n') +
                '\\n' + 'abcdef0123456789'.repeat(30)})""")
            screenshots = Path(tempfile.mkdtemp(prefix="herdr-terminal-layout-"))

            async def check_layout(width, height, label):
                # Chromium's desktop host window cannot be resized while fullscreen.
                # Set the emulated orientation, then re-enter through the actual button.
                if await page.evaluate("Boolean(document.fullscreenElement)"):
                    await page.evaluate("document.exitFullscreen()")
                await page.set_viewport_size({"width": width, "height": height})
                await page.locator('#terminalFullscreenButton').click()
                await page.wait_for_function("Boolean(document.fullscreenElement)")
                await page.wait_for_function(
                    """([w, h]) => {
                      const rect = document.getElementById('terminalView').getBoundingClientRect();
                      return Math.abs(rect.width - w) < 2 && Math.abs(rect.height - h) < 2;
                    }""", arg=[width, height], timeout=5000,
                )
                metrics = await page.evaluate("""() => {
                  const rect = id => {
                    const r = document.getElementById(id).getBoundingClientRect();
                    return {x: r.x, y: r.y, width: r.width, height: r.height, right: r.right, bottom: r.bottom};
                  };
                  const output = document.getElementById('termContent');
                  return {view: rect('terminalView'), input: rect('termInput'), output: rect('termContent'),
                    wrap: getComputedStyle(output).whiteSpace,
                    scrollWidth: output.scrollWidth, clientWidth: output.clientWidth,
                    font: parseFloat(getComputedStyle(output).fontSize),
                    pageWidth: document.documentElement.scrollWidth,
                    headerHidden: getComputedStyle(document.querySelector('body > .header')).display === 'none',
                    controls: [...document.querySelectorAll('.term-header button')].filter(b => !b.hidden)
                      .map(b => ({right: b.getBoundingClientRect().right, bottom: b.getBoundingClientRect().bottom}))};
                }""")
                assert metrics["headerHidden"], metrics
                assert metrics["view"]["x"] >= -1 and metrics["view"]["y"] >= -1, metrics
                assert metrics["input"]["bottom"] <= height + 1, metrics
                assert metrics["input"]["right"] <= width + 1, metrics
                assert metrics["output"]["height"] >= 26, metrics
                assert metrics["scrollWidth"] <= metrics["clientWidth"] + 1, metrics
                assert metrics["pageWidth"] <= width + 1, metrics
                assert metrics["font"] >= 13, metrics
                assert all(c["right"] <= width + 1 and c["bottom"] <= height for c in metrics["controls"]), metrics
                print(json.dumps({"case": label, "size": [width, height], "output_height": metrics["output"]["height"], "result": "pass"}), flush=True)

            for width, height, label in [
                (393, 852, "phone portrait"), (852, 393, "phone landscape"),
                (320, 568, "small phone"), (768, 1024, "tablet portrait"),
                (1024, 768, "tablet landscape"), (1280, 800, "wide tablet"),
                (393, 310, "phone keyboard space"), (852, 190, "landscape keyboard space"),
                (1024, 350, "tablet keyboard space"),
            ]:
                await check_layout(width, height, label)
                if label in {"phone portrait", "phone landscape", "tablet landscape"}:
                    await page.screenshot(path=str(screenshots / f"{label.replace(' ', '-')}.png"))

            await page.locator('.search-btn').click()
            await check_layout(852, 190, "landscape keyboard search")
            await page.locator('.search-btn').click()

            await page.get_by_role("button", name="Keep columns", exact=True).click()
            assert await page.evaluate("termContent.scrollWidth > termContent.clientWidth")
            await page.get_by_role("button", name="Keep columns", exact=True).click()

            # A real question rebuilds the existing terminal, without a new user gesture.
            await page.evaluate("""() => {
              agents[0].status = 'blocked';
              agents[0].options = Array.from({length: 12}, (_, i) => `Option ${i + 1}`);
              openTerminal('demo:workspace:p1', false);
            }""")
            await page.locator('.term-input button[aria-label="Keys"]').click()
            await check_layout(852, 190, "keyboard plus action keys")
            await page.locator('.term-input button[aria-label="Commands"]').click()
            panel = await page.locator('#cmdPalette > div').last.bounding_box()
            assert panel["y"] >= 0 and panel["y"] + panel["height"] <= 191, panel
            await page.locator('#cmdPalette button').click()
            await page.get_by_role("button", name="Back to agent list", exact=True).click()
            assert not await page.evaluate("document.body.classList.contains('terminal-open')")
            await page.wait_for_function("!document.fullscreenElement")
            assert not errors, errors
            print(f"Screenshots: {screenshots}", flush=True)
            print("PASS: layout, wrapping, drawers, palette and return to list.", flush=True)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
