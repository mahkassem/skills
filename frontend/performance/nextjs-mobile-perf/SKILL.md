---
name: nextjs-mobile-perf
description: Diagnose and fix slow initial page loads on mobile for Next.js sites — especially iPhone Safari, where emulated audits (Lighthouse/Chrome DevTools) routinely miss the real bottleneck because real WebKit behaves nothing like emulated Blink. Use this skill whenever a user says their site/page is slow on mobile, slow on iPhone/Safari, has bad LCP/FCP/Core Web Vitals, "loads fast on desktop but slow on my phone", wants to lazy-load sections or optimize a hero image, or asks why Lighthouse looks good while the phone feels slow. Also use it when they want to test a Next.js build "like production" locally, from a phone, or on the LAN — the measurement traps alone (firewall, dev-vs-prod server, throttling modes) invalidate naive tests.
---

# Next.js Mobile Page-Speed Optimization

## The one rule that pays for everything else

**Measure on the real device before changing code.** Emulated mobile audits run Blink (Chrome's engine) with throttling; iPhones run WebKit. They disagree in ways that invert your priorities — a page can score 95 in Lighthouse while a physical iPhone stalls for 10+ seconds on the same build, because the stall lives in WebKit's renderer (giant blur rasterization, `content-visibility` layout thrash), which Blink shrugs off. Optimizing from an emulated report alone routinely produces a "1% improvement" on the device that matters.

Practical hierarchy of evidence, best first:
1. A real phone loading the page through the RUM proxy in `scripts/rum-proxy.mjs` (on-device paint/LCP/load timings + full waterfall + main-thread stall detection).
2. iOS Simulator Safari (real WebKit, but desktop-class CPU/GPU — catches engine bugs, hides device-scale stalls; a fix that helps here usually helps the phone more).
3. `lighthouse --throttling-method=devtools` (real applied throttling; trustworthy LCP phase attribution).
4. Default Lighthouse (simulated throttling) — scores are comparable run-to-run, but its LCP phase breakdown and filmstrip timings are estimates; do not root-cause from them.

## Workflow

### 1. Reproduce with trustworthy numbers

Run Lighthouse with real throttling against the production build (never `next dev`):

```bash
npx lighthouse <url> --throttling-method=devtools --output=json --output-path=./lh.json --quiet --chrome-flags="--headless=new"
```

Pull the LCP element and its phase breakdown (`largest-contentful-paint-element` audit). If the user has a phone available, put `scripts/rum-proxy.mjs` in front of the server and have them load it once — see `references/measurement.md` for setup, the beacon fields, and the LAN traps that produce fake 6–14s readings (macOS firewall silently dropping the port, `npm run build & npm start` racing, HTTP/1.1 vs CDN H2, Low Power Mode).

### 2. Attribute — the four-bucket decision table

Where the milliseconds sit tells you which fix family applies. From LCP phases + the beacon:

| Signal | Meaning | Go to |
|---|---|---|
| TTFB large | Server/CDN/cache problem, not frontend | Fix hosting/caching first |
| LCP "Load Time" dominates (image still downloading) | Too many bytes, or bandwidth contention | `references/images.md` |
| LCP "Render Delay" dominates in *simulated* mode | Probably misattributed — re-run with devtools throttling before believing it | — |
| Everything downloaded fast but load/LCP fires seconds later with a silent network gap | **Main-thread or renderer stall** — on iPhone, almost always a WebKit hazard | `references/webkit-hazards.md` |
| FCP itself late while TTFB is fine | Render-blocking CSS starved by competing high-priority requests (often the hero image preload) | `references/images.md` § priority |

The killer diagnostic for the stall bucket: server log shows all requests answered in <1s, browser reports FCP early, but `load` fires at 10s+ with `performance` showing no pending resources. That is never network; it's the renderer.

### 3. Bisect instead of guessing

The RUM proxy rewrites HTML per query param so you can strip one suspect at a time **without rebuilding**: `?nojs` (all app scripts), `?noinline` (inline scripts only), `?nocv` (content-visibility classes), `?noanim` (entrance animations), `?noblur` (blur utilities). Load each variant on the real device; the fastest variant names the culprit. Two runs from a user's phone can settle what hours of reading code cannot.

### 4. Fix by cause

- **WebKit renderer stalls** (content-visibility, giant Gaussian blurs, animated filters, backdrop-filter): `references/webkit-hazards.md` — includes the blur→radial-gradient conversion recipe with the box-sizing formula that keeps the design pixel-equivalent.
- **Image weight and fetch priority** (alpha-channel bloat, `/_next/image` re-encoding traps, `fetchPriority` starving CSS, `sizes`): `references/images.md`.
- **JS weight**: prefer moving work off the critical chunk over deleting features — e.g. `LazyMotion` with a dynamically-imported features file removes the motion runtime from first load with zero behavior change (the static first frame renders; the engine arrives before the first timed animation). The framework+hydration floor (~70KB gz) is not worth fighting.
- **Below-the-fold sections**: `next/image` already lazy-loads below-fold images — verify with the waterfall before adding machinery. Do **not** reach for `content-visibility: auto` (see hazards reference) or JS mount-on-scroll (breaks SEO/a11y for marketing pages). If below-fold weight is genuinely the problem it will show as bytes in the waterfall, not vibes.

### 5. Verify like you measured

Re-run the same instruments on the same conditions: devtools-throttled Lighthouse, then the device beacon. For any visual-adjacent change (blur→gradient, image recompression) capture before/after screenshots at render size and compare — `xcrun simctl openurl booted <url>` + `simctl io booted screenshot` gives consistent frames. Report the numbers movement, not adjectives.

## Common Next.js-specific traps (fast checklist)

- Testing `next dev` or a build still in progress (`npm run build & npm start` — the single `&` races them; use `&&`).
- `next start` warns under `output: "standalone"` (verified in Next 16 source — it warns rather than errors, so don't relitigate this) and is not the production artifact either way: run `node .next/standalone/.../server.js` with `.next/static` + `public/` copied in beside it, or build the project's own Dockerfile.
- Hero `<Image priority fetchPriority="high">` on a mobile layout where the image sits *below* the copy — the preload starves render-blocking CSS and fonts; keep `priority`, drop the explicit high hint, and shrink the bytes instead.
- A "260KB webp" whose pixels compress to 60KB — the alpha channel (soft shadows/glows) encoded near-losslessly. See `references/images.md`.
- Six `next/font` families: only the face used above the fold should preload (`preload: false` on the rest).
- Blaming entrance animations for late LCP: CSS-driven `opacity` entrances delay *paint recording* by their duration (~0.7s), not by seconds. A/B with `prefers-reduced-motion: reduce` emulation before touching them.

## Scripts

- `scripts/rum-proxy.mjs` — instrumenting reverse proxy: injects an on-device timing beacon into HTML, logs every request server-side with timings, detects TLS-on-plain-HTTP attempts (Safari HTTPS-First), and serves the `?nojs/?nocv/?noanim/?noblur` bisection modes. Run: `UPSTREAM_PORT=3000 LISTEN_PORT=9090 node rum-proxy.mjs`; usage details in `references/measurement.md`.
