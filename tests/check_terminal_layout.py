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

            async def check_reading_view():
                # The controls are on when a terminal opens, so reach the
                # reading view the way an operator does: by asking for it.
                hide = page.get_by_role('button', name='Hide terminal controls', exact=True)
                if await hide.is_visible():
                    await hide.click()
                for selector in ['#termHeader', '.term-input', '#termKeys', '#quickDock',
                                 '#quickActions', '#termSearch']:
                    assert not await page.locator(selector).is_visible(), selector
                output = await page.locator('#termContent').bounding_box()
                view = await page.locator('#terminalView').bounding_box()
                assert abs(output['height'] - view['height']) < 2, (output, view)
                button = page.get_by_role('button', name='Show terminal controls', exact=True)
                assert await button.is_visible()
                assert await button.get_attribute('aria-expanded') == 'false'
                bounds = await button.bounding_box()
                assert bounds['x'] >= view['x'] and bounds['y'] >= view['y'], bounds
                assert bounds['x'] + bounds['width'] <= view['x'] + view['width'], bounds

            await check_reading_view()
            cdp = await context.new_cdp_session(page)
            await cdp.send('Emulation.setSafeAreaInsetsOverride', {'insets': {'top': 32}})
            await check_reading_view()
            await cdp.send('Emulation.setSafeAreaInsetsOverride', {'insets': {'top': 0}})
            await page.get_by_role('button', name='Show terminal controls', exact=True).click()
            await page.evaluate("""() => handleMessage({type: 'pane_content', pane_id: 'demo:workspace:p1',
              content: '\\x1b[32mTerminale di prova\\x1b[0m\\n' +
                Array.from({length: 30}, (_, i) => `${i + 1}. Output leggibile sul dispositivo: ` +
                  'parole lunghe e informazioni della sessione '.repeat(4)).join('\\n') +
                '\\n' + 'abcdef0123456789'.repeat(30)})""")
            screenshots = Path(tempfile.mkdtemp(prefix="herdr-terminal-layout-"))

            async def check_layout(width, height, label):
                await page.set_viewport_size({"width": width, "height": height})
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
                      .map(b => ({right: b.getBoundingClientRect().right, bottom: b.getBoundingClientRect().bottom})),
                    // Everything the composer stacks below the input, which the
                    // input's own bottom says nothing about.
                    below: [...document.getElementById('terminalView').children]
                      .filter(el => !el.hidden
                        && getComputedStyle(el).display !== 'none'
                        && getComputedStyle(el).position === 'static')
                      .map(el => ({id: el.id, bottom: el.getBoundingClientRect().bottom}))};
                }""")
                assert metrics["headerHidden"], metrics
                assert metrics["view"]["x"] >= -1 and metrics["view"]["y"] >= -1, metrics
                assert metrics["input"]["bottom"] <= height + 1, metrics
                # A row under the input is just as unreachable when it falls off
                # the screen, and the input's own bottom cannot reveal that.
                for row in metrics["below"]:
                    assert row["bottom"] <= height + 1, (row, metrics)
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
                await page.get_by_role('button', name='Hide terminal controls', exact=True).click()
                await check_reading_view()
                if label in {"phone portrait", "phone landscape", "tablet landscape"}:
                    await page.screenshot(path=str(screenshots / f"{label.replace(' ', '-')}.png"))
                await page.get_by_role('button', name='Show terminal controls', exact=True).click()

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
              openTerminal('demo:workspace:p1');
            }""")
            await page.get_by_role('button', name='Keys', exact=True).click()
            await check_layout(852, 190, "keyboard plus action keys")
            await page.locator('#termInput').fill('draft command')
            assert not await page.locator('#termKeys').is_visible()
            for mode, panel in [('Keys', '#keysPad'), ('123', '#digitsPad'), ('Commands', '#quickDock')]:
                await page.get_by_role('button', name=mode, exact=True).click()
                assert await page.locator(panel).is_visible(), mode
                assert not await page.evaluate("document.activeElement.id === 'termInput'")
                panel_box = await page.locator('#termKeys' if mode != 'Commands' else '#quickDock').bounding_box()
                assert panel_box['height'] >= 26, (mode, panel_box)
                assert panel_box['y'] + panel_box['height'] <= 191, (mode, panel_box)
            await page.get_by_role('button', name='ABC', exact=True).click()
            assert await page.evaluate("document.activeElement.id === 'termInput'")
            assert not await page.locator('#termKeys').is_visible()
            assert not await page.locator('#quickDock').is_visible()
            await page.get_by_role('button', name='Hide terminal controls', exact=True).click()
            await check_reading_view()
            assert not await page.evaluate("document.activeElement.id === 'termInput'")
            await page.evaluate("openTerminal('demo:workspace:p1')")
            await check_reading_view()
            await page.get_by_role('button', name='Show terminal controls', exact=True).click()
            assert await page.locator('#termInput').input_value() == 'draft command'
            assert not await page.locator('#termKeys').is_visible()
            await page.evaluate("openTerminal('demo:workspace:p1')")
            assert await page.locator('#termInput').is_visible()
            await page.get_by_role('button', name='Commands', exact=True).click()
            await page.get_by_role('button', name='Agent commands', exact=True).click()
            panel = await page.locator('#cmdPalette > div').last.bounding_box()
            assert panel["y"] >= 0 and panel["y"] + panel["height"] <= 191, panel
            await page.locator('#cmdPalette button').click()
            await page.get_by_role("button", name="Back to agent list", exact=True).click()
            assert not await page.evaluate("document.body.classList.contains('terminal-open')")
            await page.locator('[data-pane-id="demo:workspace:p1"]').click()
            await check_reading_view()
            await page.get_by_role('button', name='Show terminal controls', exact=True).click()
            await page.evaluate("""() => {
              agents[0].status = 'working';
              window.sentMessages = [];
              ws = {readyState: 1, send(raw) { window.sentMessages.push(JSON.parse(raw)); }};
            }""")
            await page.locator('#termInput').fill('Run from mobile')
            await check_layout(852, 190, 'text with keyboard space')
            await page.evaluate('ws = null')
            await page.evaluate("sendText()")
            assert 'Not connected' in await page.locator('#composerStatus').inner_text()
            assert await page.locator('#termInput').input_value() == 'Run from mobile'
            assert not await page.get_by_role('button', name='Send', exact=True).is_disabled()
            await page.evaluate("""() => {
              ws = {readyState: 1, send(raw) { window.sentMessages.push(JSON.parse(raw)); }};
            }""")
            await page.get_by_role('button', name='Send', exact=True).click()
            await page.wait_for_function("sentMessages.filter(m => m.type === 'agent_prompt').length === 1")
            requests = await page.evaluate("sentMessages.filter(m => m.type === 'agent_prompt')")
            assert len(requests) == 1, requests
            request = requests[0]
            assert request['pane_id'] == 'demo:workspace:p1'
            assert request['text'] == 'Run from mobile'
            assert request['request_id']
            assert not await page.evaluate("sentMessages.some(m => m.type === 'send_text')")
            assert not await page.evaluate("sentMessages.some(m => m.type === 'send_keys')")
            assert await page.locator('#termInput').input_value() == 'Run from mobile'
            assert await page.get_by_role('button', name='Send', exact=True).is_disabled()
            await page.evaluate("sendText()")
            assert len(await page.evaluate("sentMessages.filter(m => m.type === 'agent_prompt')")) == 1
            await page.evaluate("id => handleMessage({type:'error', request_id:id, message:'Synthetic delivery failure'})", request['request_id'])
            assert await page.locator('#termInput').input_value() == 'Run from mobile'
            assert 'Synthetic delivery failure' in await page.locator('#composerStatus').inner_text()
            await check_layout(852, 190, 'text error with keyboard space')
            await page.screenshot(path=str(screenshots / 'text-error-keyboard.png'))

            # A file adds a preview row and a percentage to the composer, which
            # is the column that already has the least room in landscape.
            # The socket lives in its own global: resizing the viewport lets the
            # real connection controller replace `ws` underneath a long test.
            await page.evaluate("""() => {
              window.uploadSent = [];
              window.uploadSocket = {readyState: 1, bufferedAmount: 0,
                send(raw) { window.uploadSent.push(JSON.parse(raw)); }};
              TerminalUpload.choose({
                name: 'una-fotografia-con-un-nome-molto-lungo.png',
                type: 'image/png', size: 2048,
                arrayBuffer: async () => new ArrayBuffer(2048),
              });
            }""")
            assert await page.locator('#attachmentPreview').is_visible()
            await check_layout(852, 190, 'attachment with keyboard space')
            await page.screenshot(path=str(screenshots / 'attachment-keyboard.png'))

            # Mid-upload: the percentage is filled in and the file is still there.
            # The promise is not returned, because it settles only once the whole
            # upload is acknowledged and evaluate would wait for it.
            await page.evaluate(
                "() => { TerminalUpload.send(window.uploadSocket, 'demo:workspace:p1', 'guarda'); }"
            )
            await page.wait_for_function("uploadSent.some(m => m.type === 'attachment_begin')")
            await page.evaluate("""() => {
              const begin = uploadSent.find(m => m.type === 'attachment_begin');
              TerminalUpload.handleMessage({type: 'command_result',
                command: 'attachment_begin', request_id: begin.request_id,
                upload_id: 'layout-1', ok: true});
            }""")
            await check_layout(852, 190, 'attachment uploading with keyboard space')

            # And the state that broke the previous composer: an error message
            # under a preview that is still on screen.
            await page.evaluate("""() => {
              const begin = uploadSent.find(m => m.type === 'attachment_begin');
              TerminalUpload.handleMessage({type: 'error', request_id: begin.request_id,
                message: 'Synthetic upload failure that is long enough to wrap on a narrow screen'});
            }""")
            assert await page.locator('#uploadStatus').is_visible()
            await check_layout(852, 190, 'attachment error with keyboard space')
            await page.screenshot(path=str(screenshots / 'attachment-error-keyboard.png'))
            await page.evaluate("() => { TerminalUpload.cancel(window.uploadSocket); }")
            assert not await page.locator('#attachmentPreview').is_visible()
            assert not await page.get_by_role('button', name='Send', exact=True).is_disabled()
            # The dashboard's real connection controller may update the global
            # socket while this long synthetic test changes the viewport.
            # Reinstall the recording socket before exercising the retry.
            await page.evaluate("""() => {
              ws = {readyState: 1, send(raw) { window.sentMessages.push(JSON.parse(raw)); }};
            }""")
            await page.get_by_role('button', name='Send', exact=True).click()
            await page.wait_for_function("sentMessages.filter(m => m.type === 'agent_prompt').length === 2")
            retries = await page.evaluate("sentMessages.filter(m => m.type === 'agent_prompt')")
            retry_state = {
                'requests': retries,
                'status': await page.locator('#composerStatus').inner_text(),
                'activePane': await page.evaluate('activePane'),
                'disabled': await page.get_by_role('button', name='Send', exact=True).is_disabled(),
            }
            assert len(retries) == 2, retry_state
            latest = retries[-1]
            # A result for another pane must not clear the current draft.
            await page.evaluate("openTerminal('demo:workspace:p2')")
            assert await page.get_by_role('button', name='Hide terminal controls', exact=True).is_visible()
            await page.locator('#termInput').fill('Other pane draft')
            handled = await page.evaluate("id => TerminalComposer.handleMessage({type:'command_result', command:'agent_prompt', request_id:id, ok:true})", latest['request_id'])
            assert handled, latest
            assert await page.locator('#termInput').input_value() == 'Other pane draft'
            await page.evaluate("openTerminal('demo:workspace:p1')")
            assert await page.locator('#termInput').input_value() == ''
            assert not errors, errors
            print(f"Screenshots: {screenshots}", flush=True)
            print("PASS: layout, wrapping, drawers, palette and return to list.", flush=True)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
