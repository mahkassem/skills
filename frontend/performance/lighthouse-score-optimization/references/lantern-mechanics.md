# Lantern simulation mechanics

How Lighthouse's simulated (default) mobile throttling turns a real trace into a score, and how to
verify any of this against the version actually installed.

## Contents

- [Why this matters](#why-this-matters)
- [Where the source lives](#where-the-source-lives)
- [The metric formulas](#the-metric-formulas)
- [Graph construction: what gets counted](#graph-construction-what-gets-counted)
- [The observed-trace anchor](#the-observed-trace-anchor)
- [The localhost degeneracy](#the-localhost-degeneracy)
- [Building a calibrated latency rig](#building-a-calibrated-latency-rig)
- [Score weights](#score-weights)

## Why this matters

Default Lighthouse mobile does not throttle the network while loading. It loads the page as fast as
the machine allows, records a trace, then **replays that trace through a model** of a 1.6 Mbps,
150 ms-RTT connection with a 4× CPU slowdown. Every reported metric except the `observed*` family is
an output of that model.

This is why intuition fails. The model has explicit inclusion rules and per-resource-type weights,
and a change only shows up if it alters what the model counts. Reading the rules takes fifteen
minutes and tells you which of ten candidate optimizations are worth doing.

## Where the source lives

Read the installed version rather than trusting this document; coefficients have changed across
releases:

```
node_modules/lighthouse/core/computed/metrics/lantern-speed-index.js
node_modules/lighthouse/core/computed/metrics/lantern-largest-contentful-paint.js
node_modules/lighthouse/core/computed/metrics/lantern-first-contentful-paint.js
node_modules/@paulirish/trace_engine/models/trace/lantern/metrics/SpeedIndex.js
node_modules/@paulirish/trace_engine/models/trace/lantern/metrics/LargestContentfulPaint.js
node_modules/@paulirish/trace_engine/models/trace/lantern/metrics/FirstContentfulPaint.js
```

If Lighthouse came from `npx`, it is under `~/.npm/_npx/<hash>/node_modules/lighthouse`. Find it with:

```bash
ls -d ~/.npm/_npx/*/node_modules/lighthouse 2>/dev/null | head -1
```

The metric classes each expose `coefficients`, `getOptimisticGraph`, `getPessimisticGraph`, and
`getEstimateFromSimulation`. Those four things are the entire contract.

## The metric formulas

Each metric blends an optimistic and a pessimistic simulation:

```
estimate = intercept + optimistic × optimisticSim + pessimistic × pessimisticSim
```

**Speed Index** (Lighthouse 12, at the default 150 ms RTT):

```
coefficients = { intercept: 0, optimistic: 1.4, pessimistic: 0.4 }
optimisticSim  = observedSpeedIndex          # real wall-clock, NOT simulated
pessimisticSim = layout-based simulation     # weighted mean of Layout task end times, floored at FCP
result         = max(blend, simulatedFCP)
```

The optimistic term being a *real observed number multiplied by 1.4* is the single most important
fact in this file. If browser startup delays first paint by 2 s, Speed Index inherits ~2.8 s of that
and no page change can remove it. Coefficients are interpolated by RTT via `getScaledCoefficients`
(at 30 ms RTT they converge toward 0.5/0.5), so a different throttling config changes the weighting.

**LCP** and **FCP** blend two simulations of their respective dependency graphs, with no observed
term. They respond to page changes in the way you would expect, which is exactly why a page that
improves FCP and LCP but not Speed Index is usually an instrument problem, not a page problem.

## Graph construction: what gets counted

`getOptimisticGraph` / `getPessimisticGraph` decide which network and CPU nodes enter each
simulation. The asymmetries that matter in practice:

- **Everything completing before the observed paint timestamp is a candidate.** The trace is the
  universe; requests that finished after the observed LCP are pruned.
- **Scripts count at full weight in both graphs.** A 30 KB JS chunk is worth roughly twice a 30 KB
  low-priority image.
- **Low-priority images are excluded from the LCP optimistic graph.** They appear only in the
  pessimistic graph, so at the 0.5-ish blended weight. Below-the-fold imagery is therefore a poor
  optimization target even when it is a large share of page bytes. Verified empirically: removing
  46 KB of lazy logos moved LCP within noise, and in one run moved it the *wrong* way (fewer bytes
  meant remaining requests finished earlier, pulling more of them inside the fixed observed window).
- **Scripts whose evaluation starts after the LCP timestamp are pruned from both graphs.** This is
  the mechanism that makes "delay hydration" such an effective and dishonest score hack.
- **`Layout`-tagged CPU tasks drive the Speed Index pessimistic term**, weighted by `log2(duration)`.

## The observed-trace anchor

`observedFirstContentfulPaint`, `observedLargestContentfulPaint`, `observedSpeedIndex` and
`observedLoad` are real, unthrottled wall-clock times from the recording machine. They are visible in
the JSON at `audits.metrics.details.items[0]`.

Use them as your instrument health check. On a prerendered static page where every byte lands within
~500 ms, the observed paint should be a few hundred milliseconds. If it is 2 s+:

- Suspect a cold-start artifact: fresh headless Chrome profile creation, extension/profile init,
  first-run GPU shader compilation.
- Verify by re-running against a persistent Chrome over `--port`. A drop from ~2.3 s to ~0.3 s
  confirms it.
- Verify again against a control site. If a completely different site shows the same stall on the
  same machine, it is the machine.

A cold-start stall costs roughly `1.4 × stall` of Speed Index and inflates the LCP graph by pulling
more requests inside the pre-paint window. It is worth several points and it is not your page.

## The localhost degeneracy

Serving from `localhost` with no added latency puts observed paint at roughly 150 ms. Two things
break:

1. Almost the entire page finishes before that timestamp, so the LCP graph contains everything and
   simulated LCP collapses into "time to download the whole page at 1.6 Mbps". The reported LCP
   element becomes irrelevant to the number.
2. Because the window is fixed and tiny, *removing* bytes can pull more requests inside it and make
   the score worse. Observed in practice: cutting 28 KB of images dropped the local score from 91 to
   88 while helping the deployed site.

Localhost is fine for checking that a build works, has no console errors, and holds TBT/CLS at zero.
It is not fine for deciding whether an optimization helped.

## Building a calibrated latency rig

To get a local loop whose deltas transfer to production, insert a latency proxy between Lighthouse
and the static server, then tune the delay until the rig's `observedLargestContentfulPaint` matches
the deployed site's.

`scripts/latency_proxy.mjs` in this skill does this. Typical calibration:

```bash
npx --yes serve build/client -l 4173 &
node scripts/latency_proxy.mjs --target http://localhost:4173 --port 4174 --delay 120 &
python3 scripts/lh_report.py --url http://localhost:4174/ --runs 3
```

Adjust `--delay` until observed LCP lands within ~10% of the deployed number. Around 120 ms typically
reproduces a GitHub Pages-class host.

Two caveats worth stating in any report built on a rig:

- **Absolute numbers are inflated** by per-request server think-time that production does not have.
  Only deltas transfer, and only for changes that alter bytes or request counts rather than
  connection behavior.
- **The rig is usually HTTP/1.1** while production is HTTP/2. Optimizations that reduce *request
  count* (chunk merging, spriting) look far better on the rig than they will in production, because
  H2 multiplexing already absorbs most of that cost. Byte reductions transfer cleanly; request-count
  reductions do not.

## Score weights

Performance category weights (Lighthouse 10–12): FCP 10%, Speed Index 10%, LCP 25%, TBT 30%, CLS 25%.

TBT and CLS dominate on paper, but on a prerendered static site they are usually already zero, which
concentrates all available headroom in LCP (25%) with FCP and Speed Index (10% each) along for the
ride. That is why the byte-cutting work in the fix catalog targets the pre-LCP window: on this class
of site it is the only 45 points still in play.

Check the weights in `node_modules/lighthouse/core/config/default-config.js` if precision matters.
