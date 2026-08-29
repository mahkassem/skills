# Measurement: Getting Numbers You Can Trust

## Lighthouse: simulated vs devtools throttling

Default Lighthouse observes an *unthrottled* trace and simulates slow-4G/4×CPU math on top. Scores are fine for run-to-run comparison, but two artifacts will send you down wrong roads:

- The **LCP phase breakdown** (TTFB / Load Delay / Load Time / Render Delay) can be wildly misattributed — a pure download-bound LCP was reported as "Render Delay: 4341ms" in simulated mode and correctly as "Load Time: 4661ms, Render Delay: 30ms" with devtools throttling.
- The **filmstrip timings** don't line up with the simulated metrics, and a busy host machine (parallel builds/audits) contaminates the observed trace.

Root-cause runs therefore use:

```bash
npx lighthouse <url> --throttling-method=devtools --output=json --output-path=./lh.json --quiet --chrome-flags="--headless=new"
```

Run audits one at a time on an otherwise-idle machine. Useful jq pulls: `.audits["largest-contentful-paint-element"].details.items[1].items[]` (phases), `.audits["network-requests"].details.items[]` (real start/end/transferSize/priority per request).

Remember what devtools-throttled Lighthouse still is: **Blink**. It proves network/bytes stories and clears CPU stories for Chrome — it cannot see WebKit renderer stalls (see webkit-hazards.md). Green here + slow iPhone = go on-device.

## On-device measurement: the RUM proxy

`scripts/rum-proxy.mjs` is a ~120-line reverse proxy. Point it at any HTTP upstream (the prod container, `next start`, staging):

```bash
UPSTREAM_HOST=127.0.0.1 UPSTREAM_PORT=3000 LISTEN_PORT=9090 node rum-proxy.mjs
```

What it does:
- **Injects a beacon** into HTML responses. ~4s after `load` (and on `pagehide` — expect duplicate sends per visit) the page POSTs to `/__rum`: UA, nav timings (dns/connect/ttfb/htmlDone/dcl/load), paint entries, LCP (`element tag + size` — and note: Safari 26+ DOES expose `largest-contentful-paint` via PerformanceObserver, so don't repeat the stale "Safari has no LCP API" claim; the field is null only on older engines), first ~60 resources (start/duration/transferSize — `d:0,b:0` rows mean cache hits), and **main-thread gaps** — a 50ms heartbeat that records any >300ms silence, the direct fingerprint of a renderer stall.
- **Logs server-side** to `rum.log`: every request with arrival time and serve duration, connection origins, and `CLIENT-ERR firstByte=0x16` lines = a TLS ClientHello hit the plain-HTTP port (Safari HTTPS-First probing — explains mystery seconds on `http://` URLs with ports).
- **Bisection modes** via query param on the page URL, no rebuild needed: `?nojs` strips app scripts (beacon survives), `?noinline` inline scripts only, `?nocv` neuters content-visibility classes, `?noanim` entrance animations + reveal attributes, `?noblur` blur utilities. Have the device load each; fastest variant names the culprit. Adapt the class-name regexes to the project's utilities before relying on `?nocv/?noanim`.

Interpreting the classic patterns:
- All requests served <1s, then silence, then `load` fires at 10s+ → renderer/main-thread stall (gaps array will show it) → webkit-hazards.md.
- Requests trickle for seconds → bandwidth/priority story → images.md.
- No connection ever arrives from the phone's IP → the phone never reached you (see traps below).

## LAN / local-test traps (each produced a bogus multi-second reading in the wild)

1. **`npm run build & npm start`** — single `&` runs them concurrently: the server starts on a half-written `.next` and the build pegs the CPU during the test. Use `&&`, wait for the build, then test.
2. **macOS firewall silently drops LAN connections to a bare `node` listener** while Docker-published ports pass (approved network path). Phone "loads for 14s then fails/falls back"? Check `/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate`; run the proxy inside Docker (`-p 9090:9090`, upstream `host.docker.internal`) or approve node.
3. **`next start` vs `output: "standalone"`** — run `node .next/standalone/.../server.js` with `.next/static` and `public/` copied in beside it, or build the repo's Dockerfile (`docker compose up --build`) for the true production artifact. In a git worktree the standalone tree nests under the worktree's relative path — `find .next/standalone -name server.js`.
4. **Plain-HTTP LAN ≠ production**: HTTP/1.1 with 6 connections and no CDN vs H2/H3 + edge cache. LAN numbers are directionally useful; absolute production numbers come from a deployed URL.
5. **The device itself**: Low Power Mode throttles Safari hard; content blockers tax every request; a hot phone thermal-throttles. Note device state alongside any reading, and treat run 1 (cold connection + local-network permission prompt) separately from runs 2+.
6. **Backgrounded pages suspend** — IntersectionObserver callbacks, timers, and rendering pause when the page/tab is hidden. A tester who switches apps mid-load reports fake seconds; automated hidden-tab/pane runs show reveal-on-scroll "not firing" when nothing is wrong.

## Verification pass

Fix applied → re-run the *same* instruments: devtools Lighthouse (expect the targeted phase to shrink), then a device beacon (expect gaps=[] and load ≈ FCP + image time). For visual-adjacent fixes, before/after screenshots at render size (`xcrun simctl openurl booted <url>` + `xcrun simctl io booted screenshot out.png`). Report deltas with numbers: "load 12.4s → 0.35s on-device", not "much faster".
