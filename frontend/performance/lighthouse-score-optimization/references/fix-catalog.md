# Fix catalog

Concrete fixes for moving a mobile Lighthouse score, ordered roughly by impact per unit of risk.
Each entry states the mechanism, because the mechanism is what transfers to the next site.

## Contents

- [1. Resource priority inversions](#1-resource-priority-inversions)
- [2. Framework hydration preloads](#2-framework-hydration-preloads)
- [3. Build-time font subsetting](#3-build-time-font-subsetting)
- [4. Inlining render-blocking CSS](#4-inlining-render-blocking-css)
- [5. Hero images: srcset and the inert-sizes bug](#5-hero-images-srcset-and-the-inert-sizes-bug)
- [6. Server-only code and namespace imports in the client bundle](#6-server-only-code-and-namespace-imports-in-the-client-bundle)
- [7. Code-splitting interaction-gated UI](#7-code-splitting-interaction-gated-ui)
- [8. Animation libraries on the critical path](#8-animation-libraries-on-the-critical-path)
- [Cache lifetimes](#cache-lifetimes)
- [Postbuild scripts: make them fail loudly](#postbuild-scripts-make-them-fail-loudly)

## 1. Resource priority inversions

**Symptom.** LCP phase breakdown shows `Render Delay` at 70%+ while the LCP element itself loaded in
under 200 ms. In the network list, fonts sit at `VeryHigh` and the render-blocking stylesheet
finishes *after* them.

**Mechanism.** Chrome assigns a font discovered from CSS `VeryHigh` priority, because at the moment
of discovery it is blocking text rendering. Six font files at `VeryHigh` will beat your stylesheet
through a 1.6 Mbps pipe, and nothing paints until the stylesheet lands.

**Fix, and it reads backwards:** add `<link rel="preload" as="font" type="font/woff2" crossorigin>`
for those fonts. A *preloaded* font is fetched at `High`, one rung below the render path, so CSS wins
the race. Preload is normally taught as "make this more urgent"; here it is the only available handle
to make something *less* urgent.

Measured effect on a real site: FCP 2.8 s → 1.74 s from this change alone.

**Check the priority column, not the waterfall shape.** `python3 scripts/lh_report.py --parse` prints
priority and end time per request for exactly this reason.

## 2. Framework hydration preloads

**Symptom.** Dozens of `<link rel="modulepreload">` in `<head>`, all `High`, all ahead of the
stylesheet. Common in React Router 7 framework mode (`<Scripts/>` emits one per matched route module:
44 on a typical page, ~180 KB gz) and in Next.js app router.

**Mechanism.** These preloads are correct for an app whose first paint requires hydration. On a
**prerendered** page, first paint needs zero JavaScript, so ~180 KB of hydration code is racing the
28 KB of CSS the browser actually needs in order to paint. React hoists the tags into `<head>`, where
the preload scanner fires them all before the stylesheet is discovered.

**Fix.** Stamp them `fetchpriority="low"` in a postbuild pass over the emitted HTML:

```js
const MODULEPRELOAD = /<link rel="modulepreload"(?! fetchpriority)/g;
html.replace(MODULEPRELOAD, '<link rel="modulepreload" fetchpriority="low"');
```

**Do not delete them instead.** Deleting measures better on a zero-latency localhost (the modules
arrive late enough that Lighthouse stops counting them) and materially worse on a real host: module
discovery collapses into a three-deep import waterfall, costing more FCP than the preloads ever did
and pushing hydration several round trips later. Deprioritizing keeps the flat one-round-trip fan-out
*and* the render path.

## 3. Build-time font subsetting

**Applicability.** Static/prerendered sites, where the exact rendered glyph set is knowable at build
time.

**Method.** Union every character in the built HTML plus a floor of printable ASCII and typographic
punctuation (`' ' ' " – … € © ™ →`, the em dash U+2014, etc.), then subset with `subset-font`
(harfbuzz via WASM, no native dependency). Preserve variable-font axes.

**The non-obvious win is OpenType features, not glyphs.** Restricting features to
`kern/liga/clig/ccmp/mark/mkmk/locl/rlig` took a JetBrains Mono variable face from 40 KB to 13.5 KB;
its coding-ligature apparatus was two-thirds of the file. Typical whole-set result: 182 KB → 101 KB
across six faces.

**Boundaries.** Subsetting is a byte fix and safe. *Dropping* a family or weight the design uses is a
design change. Check actual usage before pruning, and if a weight is genuinely used, report it as
headroom rather than deleting it. Narrowing variable-font `wght` axes silently clamps weights outside
the range: also a design change.

Leave non-Latin faces (Arabic, CJK) alone unless you can verify the full required glyph set; the
failure mode is invisible tofu on pages you did not check.

## 4. Inlining render-blocking CSS

**Payoff.** Removes an entire round trip from the render path; FCP drops by roughly one RTT plus the
stylesheet transfer.

**Do it at the source, not by rewriting HTML.** With React (and React Router's `<Links/>`), a
`<link rel="stylesheet">` without a `precedence` prop is an ordinary host fiber in `<head>`. If the
static HTML no longer contains it, `canHydrateInstance` finds no match and throws a hydration
mismatch, which discards the server HTML and client-renders the entire document, a catastrophic
regression that a screenshot will not reveal.

The clean version is to emit the link **only during SSR**:

```js
export function links() {
  return typeof document === "undefined"
    ? [{ rel: "stylesheet", href: appCss }, { rel: "stylesheet", href: fontsCss }]
    : [];
}
```

Then inline the CSS into the prerendered HTML in a postbuild pass. The client tree has no `<link>`
fiber to reconcile, and `<head>` is a HostSingleton whose extra children React tolerates.

Use `typeof document`, **not** `import.meta.env.SSR`: the latter is a build-time constant, so the
client bundle would dead-code-eliminate the CSS imports and the bundler would stop emitting the
stylesheets entirely.

**Ordering.** This pass must run after any pass that rewrites asset URLs or hashes inside the CSS
(font subsetting, for one), or you inline stale references.

**Trades to state out loud:** every page now carries its own copy of the CSS (cross-page HTTP caching
of the stylesheet is gone: irrelevant for client-side navigation, real for full page loads), and
parsing the CSS inside the document parse can add ~20 ms of TBT.

## 5. Hero images: srcset and the inert-sizes bug

`sizes` without `srcset` is **inert**, a very common latent bug that silently ships a
desktop-resolution image to phones. Check for it whenever a large image sits near the fold.

Generate a mobile variant (~750–768 w) and provide a real `srcset`. Measured: 70 KB → 27 KB on the
audited mobile viewport.

Before lazy-loading anything near the fold, **check the element's actual bounding box at 375–412 px**.
An image that is obviously below the fold on desktop is frequently above it on a phone. Also note that
an entrance animation starting at `opacity: 0` disqualifies an element from LCP candidacy, which can
make the reported LCP element something unexpected (a small header logo, say). Worth knowing before
optimizing the wrong thing.

## 6. Server-only code and namespace imports in the client bundle

Two distinct leaks, both large, both invisible without inspecting the emitted chunks:

**Module-scope side effects defeat tree-shaking.** A framework can strip server-only *exports*
(`action`, `loader`) from a route module, but it cannot remove a top-level constructor call:

```js
const ses = new SESv2Client({ region: process.env.AWS_REGION });  // pulls in the whole SDK
```

Rollup treats the constructor as opaque and retains it, dragging 44 KB gz of AWS SDK into the client
bundle. The fix is to move the construction into a server-only module (`*.server.ts` in React
Router/Remix, which the build *enforces*) or behind a lazy `await import()` inside the action. Prefer
the enforced version: it converts a tree-shaking accident into a build-time guarantee that fails loudly
if server code ever becomes client-reachable again.

Note that moving only the client is not enough if the route also exports other non-route helpers that
keep the import alive; move the whole pipeline.

**Namespace imports pull entire libraries.** `import * as Icons from "lucide-react"` for a
string→component lookup is opaque to tree-shaking and shipped ~1,600 icons: 161 KB gz → 1.9 KB after
replacing it with an explicit registry object. Grep for `import \* as` across the app; each hit in a
client module is a candidate.

Detect both by inspecting emitted chunks:

```bash
ls -la build/client/assets | sort -k5 -n | tail -20
grep -rl "aws-sdk\|SESv2\|<some server symbol>" build/client && echo "LEAK"
```

## 7. Code-splitting interaction-gated UI

Mobile nav drawers, dropdown menus and dialogs typically render only a trigger until the user acts,
yet their Radix/Headless machinery ships in the initial chunk. Dynamically import the panel on
`pointerdown` (or `pointerenter` for hover-opened desktop menus) and keep the trigger eager so the
first tap still works.

Two things to verify: the closed-state SSR markup must stay identical (no hydration mismatch), and a
fast double-tap must not break the open state. Measured: ~19 KB gz off the initial fan for a drawer
plus a desktop nav menu.

## 8. Animation libraries on the critical path

A provider mounted in the root layout loads its feature chunks on **every** page. If above-the-fold
animation can be expressed in CSS (a rotating headline, a fade-in), converting it lets the library
move to the routes that actually need it: ~30 KB gz off the home page's pre-LCP graph.

When converting, keep the first frame painted and opaque in the static HTML: an element that starts at
`opacity: 0` cannot be the LCP candidate and, if it is your main heading, you have moved the problem
rather than fixed it. Honor `prefers-reduced-motion`, and gate discrete-property animation behind
`@supports (transition-behavior: allow-discrete)` so older browsers get a static first frame.

## Cache lifetimes

Lighthouse's "efficient cache lifetimes" audit is **unscored**: it affects repeat-visit speed, not
the number. Fix it because it is right, not to move the score.

Content-hashed assets (`app-B4nK2x.js`) are immutable by construction and can take
`max-age=31536000, immutable`. A month (`max-age=2592000`) captures essentially all of the practical
benefit and is a reasonable default when someone prefers a shorter horizon. HTML should stay
revalidated (`no-cache` or a short max-age) so deploys are picked up.

Note that some static hosts do not allow header configuration at all (GitHub Pages hard-codes
`max-age=600`), in which case the audit is unfixable there and the right move is to say so rather
than hunt for a workaround.

## Postbuild scripts: make them fail loudly

Every postbuild HTML/asset rewrite depends on markup a framework version can change. A regex that
silently matches nothing turns into an invisible performance regression months later. Throw instead:

```js
if (rewritten === 0) {
  throw new Error(
    'postbuild: no <link rel="modulepreload"> found in build/client. ' +
    "React Router's <Scripts/> output shape changed; update the pattern."
  );
}
```

Same for the re-run case: a pass that has already been applied should fail rather than double-apply.
