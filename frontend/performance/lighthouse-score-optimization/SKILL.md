---
name: lighthouse-score-optimization
description: Raise a site's mobile Lighthouse performance score by working from the Lantern simulation's actual mechanics: what it counts, at what weight, and which bytes move it. Use this skill whenever someone wants to improve or fix a Lighthouse/PageSpeed performance score, hit a target number ("get us to 90+", "why are we stuck at 85?"), compare the scores of two builds or frameworks, or asks why LCP/FCP/Speed Index won't budge despite optimizations. Also use it when Lighthouse numbers look noisy, contradictory, or too good or bad to believe. Most wasted performance work comes from an untrustworthy instrument, and this skill's first job is fixing that. Applies to any framework (React Router, Next.js, Astro, SvelteKit, Vite SPAs, plain static HTML). If instead the complaint is "the score looks fine but my actual iPhone feels slow", prefer the nextjs-mobile-perf skill; that's a real-device WebKit problem, not a scoring one.
---

# Lighthouse Score Optimization

## The thesis

A mobile Lighthouse score is not a measurement. It is a **simulation** (Lantern) that replays a
recorded page load through a modeled 1.6 Mbps / 150 ms-RTT pipe. Two consequences drive everything
in this skill:

1. **Optimizations only count if the simulation counts them.** Shrinking a 46 KB image that Lantern
   weights at 0.5 (or excludes entirely) moves nothing, while trimming 30 KB of script moves the
   score measurably. You cannot know which is which by intuition; you read the rules.
2. **The simulation is anchored to a real observed trace, so a sick browser produces a sick score.**
   A cold headless Chrome that stalls 2 s before first paint inflates Speed Index by ~1.4× that
   stall, on any page, forever. People burn days optimizing pages to fix what is actually their
   measurement harness.

So the order of work is: **fix the instrument, read the rules, then cut the bytes the rules count.**
Skipping to step three is the normal way perf work fails.

## Phase 0: Build an instrument you can trust

Do this before changing a single line of page code. It costs ten minutes and it is the difference
between real progress and a week of noise.

**Always take a median of 3+ runs.** Single Lighthouse runs vary by 5–15 points. A "before/after"
built from two single runs is indistinguishable from noise. `scripts/lh_report.py` does the runs
and the median for you.

**Check for a cold-start paint stall.** In the JSON, compare `observedFirstContentfulPaint` against
when resources actually finished downloading. On a prerendered static page whose bytes all land by
~500 ms, an observed FCP of 2.3 s is not the page; it is browser startup being counted as page
time. The tell is that the number barely changes when you make the page dramatically lighter.

Confirm it by running the same audit against a **persistent Chrome** instead of a fresh one:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new \
  --remote-debugging-port=9333 --user-data-dir=/tmp/lh-profile about:blank &
sleep 4
npx --yes lighthouse <url> --only-categories=performance --port=9333 --quiet \
  --output=json --output-path=warm.json
```

If observed FCP drops from ~2.3 s to ~0.3 s, you have found a harness artifact worth several points
of Speed Index. Report cold and warm numbers separately from then on: cold is comparable to your
own earlier runs, warm is closer to what PageSpeed Insights' datacenter Chrome and real repeat
visitors see. Neither is "the real one"; the honest move is showing both and saying which is which.

**Sanity-check against a control.** Measure a second site the same way (a competitor, or the
previous version of the site). If the control shows the same anomaly, it is your machine, not the
page.

**Then keep looking.** A cold-start stall is a common cause of a stuck Speed Index, not the only
one, and finding one is not permission to stop. Two page-side causes produce the same symptom and
must be ruled out explicitly:

- **The filmstrip never settles.** Compare `observedTraceEnd` against `observedLastVisualChange`. If
  the gap is small (tens of milliseconds), the viewport was still changing when recording stopped.
  Speed Index scores visual completeness *relative to the final frame*, so a permanently-animating
  above-the-fold element makes every earlier frame "incomplete" by construction, and no byte change
  can fix it. Visual completeness *decreasing* between frames is the signature.
- **Non-composited animations.** Check the `non-composited-animations` audit. Animating `display`,
  `width`, `top` or similar forces style and layout on the main thread every keyframe. The audit is
  informational and may still score 1, so it does not surface in a summary; read it directly.

**Use the warm/cold contrast to separate causes, not just to detect them.** A defect present in both
the warm and cold runs cannot explain the difference between them. If a warm run scores well *with
the animation still present*, then the stall explains the run-to-run delta and the animation is a
separate, real, additional issue. Both can be true; report both, and attribute each honestly.

**Do not trust localhost.** With zero network latency the observed paint happens at ~150 ms, so
essentially the entire page falls inside Lantern's pre-paint window and simulated LCP degenerates
into "time to download everything". Changes that genuinely help the deployed site can *lower* the
localhost score, and vice versa. If you need a local loop, put a latency proxy in front of the
static server and tune its delay until observed LCP matches the deployed site's observed LCP
(`scripts/latency_proxy.mjs` does this). Then trust deltas from that rig, not its absolute numbers.

## Phase 1: Diagnose from the JSON, not the HTML report

The summary report tells you what is slow; the JSON tells you *why*, which is what you need.

```bash
python3 scripts/lh_report.py --url <url> --runs 3        # run + median + full diagnosis
python3 scripts/lh_report.py --parse lh-*.json           # analyze runs you already have
```

Read these four things:

- **LCP element + its phases** (`largest-contentful-paint-element`). The phase split is the whole
  diagnosis. `Load Time` dominant means the element itself is too big; compress it. **`Render Delay`
  dominant (say 74%) means the element was ready and something else blocked painting**. That is a
  bandwidth-contention or render-blocking problem, and shrinking the element accomplishes nothing.
- **Render-blocking resources.** Usually stylesheets, and usually the thing losing the bandwidth race.
- **Network requests with priority and end time.** Priority inversions live here and they are
  invisible in the HTML report. Sort by size and look at what is competing with your critical path.
- **Observed vs simulated metrics**: the Phase 0 sanity check, every time.

A caution the evals for this skill surfaced: an agent handed a ready explanation tends to stop at the
first sufficient one. In a real comparison, a run that correctly diagnosed a cold-start stall never
looked further and missed eight elements animating `display` that were also degrading the same
metric; a second run on a different report ran the same check, correctly *rejected* the instrument
hypothesis (`observedFirstVisualChange` was 316 ms, so the browser was painting fine), and found the
page cause. The check is only worth running if you are equally prepared for either answer. Enumerate
the candidate causes for a stuck metric before testing any of them, and say which ones you ruled out
and how.

## Phase 2: Know what the simulation actually counts

Do not take this from memory or blog posts; the rules change between Lighthouse versions and they
are readable in the user's own `node_modules`:

```
node_modules/lighthouse/core/computed/metrics/lantern-*.js
node_modules/@paulirish/trace_engine/models/trace/lantern/metrics/*.js
```

The load-bearing rules, as of Lighthouse 12 (verify before relying on them):

- **Speed Index** `= max(FCP, 1.4 × observedSpeedIndex + 0.4 × layoutBasedSimulation)` at 150 ms RTT.
  That 1.4 coefficient on a *real observed wall-clock number* is why a cold-start stall is
  unbeatable by page changes, and why Phase 0 exists.
- **LCP** counts every request finishing before the observed LCP timestamp. **Scripts count at full
  weight; low-priority images are excluded from the optimistic graph** and so count at roughly half.
  This single asymmetry redirects most optimization effort: cutting 46 KB of lazy brand logos is
  nearly worthless, cutting 30 KB of hydration JavaScript is not.
- **FCP** counts render-blocking resources and the document. It is usually the cheapest metric to
  fix and it drags Speed Index down with it.

The practical translation: **the lever is bytes in the pre-LCP window, weighted by resource type.**
Everything in the fix catalog is a way to remove bytes from that window, or move them out of it.

For the detailed mechanics, graph-construction rules, and how to verify them against a specific
Lighthouse version, read `references/lantern-mechanics.md`.

## Phase 3: Cut the bytes that count

`references/fix-catalog.md` holds the full catalog with implementation detail. Work in this order,
because it is roughly descending impact per unit of risk:

1. **Fix resource priority inversions.** The counterintuitive one: fonts discovered from CSS fetch at
   `VeryHigh` and starve the render-blocking stylesheet. *Adding* `<link rel="preload" as="font">`
   moves them to `High`, below the render path, and speeds up first paint. Preloading as a
   deprioritization tool is not how preload is usually taught, but it is what the priority table does.
2. **Get framework hydration preloads off the critical path.** Next.js and React Router emit a
   `modulepreload` per route module: dozens of them, hoisted into `<head>`, fetched at `High` before
   the stylesheet is even discovered. On a prerendered page that needs no JS to paint, stamping them
   `fetchpriority="low"` is a large FCP win. Deleting them instead is worse: discovery collapses into
   an import waterfall on real latency.
3. **Subset fonts at build time.** On a static site the rendered glyph set is knowable. Trimming
   *OpenType features* is often the bigger win; coding-ligature tables can be two-thirds of a
   monospace font.
4. **Inline render-blocking CSS into prerendered HTML**, done at the source (emit the `<link>` only
   during SSR), not by post-processing HTML, or React hydration will find a missing element and
   discard the entire server-rendered tree.
5. **Give hero images a real `srcset`.** A `sizes` attribute without `srcset` is inert, a common
   latent bug that silently ships desktop-sized images to phones.
6. **Evict server-only code and namespace imports from the client bundle.** Module-scope side effects
   (`const client = new SomeSDK()` at top level) defeat tree-shaking entirely, and
   `import * as Icons from "lucide-react"` ships all ~1,600 icons. Both are pure waste and often huge.
7. **Code-split interaction-gated UI.** Drawers, dropdowns and dialogs only need their machinery on
   first tap or hover.
8. **Move animation libraries off the critical path** for above-the-fold content that CSS can drive.

## Phase 4: Verify honestly

**Confirm the deploy is actually live before measuring it.** Grab a content hash or a distinctive
string from your local build and check it appears in the deployed HTML. Measuring a stale deploy and
reporting the delta as your improvement is the easiest mistake to make here, and it invalidates
everything downstream.

**Re-diagnose after each round.** The bottleneck moves. Fixing FCP frequently exposes a completely
different LCP constraint, and continuing to fight the old bottleneck wastes the round.

**Know and state the ceiling.** When the remaining pre-LCP graph is framework runtime (react-dom +
router ≈ 100 KB gz) plus host TTFB, say so plainly and quantify what is left, rather than grinding
out diminishing returns. "95, and the remaining 5 points are TTFB and react-dom" is a more useful
result than a number with no explanation.

## What not to do

These raise the score while making the site worse. They are the reason perf work gets distrusted, so
name the trade explicitly rather than quietly taking it:

- **Delaying or stripping hydration** to drop scripts from Lantern's graph. It works spectacularly
  on the metric and leaves navigation and interactive components dead on slow connections.
- **Lazy-loading a real LCP element**, or one that is above the fold on the audited viewport. Check
  the element's actual bounding box at 375–412 px before assuming "below the fold".
- **Deleting fonts or weights the design visibly uses.** Subsetting is a byte fix; dropping a family
  is a design change wearing a build flag.
- **Tuning against localhost numbers.** Phase 0 explains why they invert.
- **Reporting a single lucky run.** Median of 3+, or it did not happen.

If a change of this kind is genuinely the only remaining path to the target, present it as a decision
for the user with the trade named. Do not take it unilaterally because the number went up.

## Reporting results

Give a table of medians before and after (score, FCP, LCP, TBT, CLS, SI), then the changes in
descending impact with one line of mechanism each, because "we subset the fonts" teaches nothing
while "the six font files were fetching at VeryHigh and starving the stylesheet" is reusable.

Close with what is left: the remaining bottleneck, its estimated headroom, and anything you
deliberately declined to do and why. If cold and warm instruments disagree, show both and explain
which reflects real users.
