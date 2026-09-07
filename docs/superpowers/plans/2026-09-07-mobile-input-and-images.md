# Mobile input and screenshots implementation plan

> Execute the approved design continuously using subagent-driven development for the relay and local implementation for the web integration. The project requires working on this fork's main and publishing after verification.

**Goal:** Remove the terminal's upper safe-area padding, alternate native typing with custom keyboard panels, and send a screenshot with a prompt to the selected local agent.

**Approved design:** The user approved edge-to-edge terminal output with reachable controls, ABC / Keys / 123 / Commands input modes occupying one keyboard area, and a plus button with image selection, preview and removal before sending.

**Architecture:** Keep the existing authenticated WebSocket and namespaced pane routing. A new bounded image-message operation stores the image privately on the relay host and submits its absolute path with the user's text through `herdr agent prompt`. The web page owns preview and request acknowledgement, while a small input controller keeps the native keyboard and custom panels mutually exclusive.

**Constraints:** No credential values or debug.log reads. No upstream changes. Tests use synthetic data. No new runtime dependencies. Local agent sessions are the screenshot target; SSH-hosted panes must return an explicit unsupported error rather than an inaccessible local path. A production relay restart is needed to activate new protocol handling; determine authorization and exact service target after all changes are verified.

## 1. Authenticated screenshot delivery

Files: `relay/attachments.py`, `relay/herdr_relay.py`, `tests/test_attachments.py`, `tests/test_herdr_relay.py`.

- [x] Add tests for valid PNG/JPEG/WebP, malformed or oversize payloads, safe generated filenames and private storage, quota, unknown or remote pane, selected named-session routing, and CLI failure without success acknowledgement.
- [x] Implement a `send_attachment` request with `request_id`, `pane_id`, optional `text` (up to 1000 characters), and `attachment: {name, mime, data}` (base64, at most 5 MiB decoded). Return matching `command_result` with `command: send_attachment`, `ok: true` only after the prompt is accepted; matching `error` on failure.
- [x] Save files with generated names in a dedicated owner-only directory outside public web assets. Bound storage; never log image bytes. Use the actual selected target and `run_herdr_result('agent', 'prompt', ...)` in a worker thread. Reject SSH targets explicitly. Configure a bounded WebSocket message size for one 5 MiB attachment.
- [x] Run focused relay tests and review the changes.

## 2. Web input modes and edge-to-edge output

Files: `web/index.html`, `tests/check_terminal_layout.py`, `tests/test_terminal_viewport.js` where useful.

- [x] Exercise native typing vs custom keyboard focus/visibility in browser tests before the fix; test reading mode with a simulated nonzero upper inset.
- [x] Remove upper safe-area padding from output; keep menu and revealed controls accessible around cutouts.
- [x] Replace separate dock buttons/tabs with a compact ABC / Keys / 123 / Commands selector. ABC focuses the input and closes custom panels. Other modes blur editable inputs, hide the native keyboard when supported, and show just their own panel. Clicking the text field always returns to ABC. Retain keyboard draft and use the last observed keyboard height, bounded by available space, for custom panels.
- [x] Preserve hidden-controls entry behavior and visual viewport adaptation, including orientation and fullscreen exit.

## 3. Web image composer and integrated validation

Files: `web/index.html`, `tests/check_terminal_layout.py`, focused synthetic end-to-end fixture, `docs/TAILSCALE.md`, `docs/HANDOFF.md`, `tests/run.sh` as required.

- [x] Add plus/file input, local image preview with removal, size/type errors, per-pane draft ownership, and send acknowledgement. Disable duplicate submission while pending. Keep draft on failure/disconnection and never send a second automatic submission after uncertain acknowledgement.
- [x] Integrate `send_attachment` with `handleMessage` and the existing Send action. Do not send a separate Enter for an image prompt.
- [x] Test actual local file upload, preview/removal, outgoing payload, success/error states, pane switching, and complete relay-to-synthetic-agent delivery. Test both panel states over phone/tablet portrait/landscape and constrained keyboard space.
- [x] Run project suite, Python lint, JS syntax/lint comparison and browser checks. Update docs, get final review, commit only task files and push this fork's main. Activate the verified relay change if authorized; otherwise report the exact remaining restart boundary.
