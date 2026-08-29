# WebKit Renderer Hazards (iPhone Safari + every iOS browser)

All iOS browsers run WebKit, so "works in Chrome on iOS" does not clear a hazard — Chrome-on-iOS is WebKit with a different shell. Blink-based emulation (Lighthouse, DevTools device mode) does not reproduce any of these; that is why they survive audits.

The measured signature of every hazard here: resources fully downloaded in well under a second, FCP early, then a multi-second silent gap before the page actually renders/settles (`load` and LCP fire together, seconds late, with an empty network waterfall in between).

## 1. `content-visibility: auto` — do not use it on real-device WebKit

The theory (skip style/layout/paint for off-screen sections) works beautifully in Blink and benchmarks well in emulation. On physical iPhones it can produce a **10+ second main-thread stall at load**: WebKit thrashes layout as each section's `contain-intrinsic-size` estimate is swapped for its real height, scroll anchoring shifts, more sections cross the render margin, and the cycle repeats. An M-series Mac (Simulator) chews through the same thrash fast enough to look merely "meh", which hides it from Simulator testing too — the cost scales catastrophically down the CPU curve.

- Measured on a production marketing page: adding `content-visibility: auto; contain-intrinsic-size: auto 900px` to seven below-fold sections took on-device load from ~1s to 12.4s. Reverting restored ~0.35s.
- If a codebase already uses it, removing it is the single highest-leverage change to test first (`?nocv` proxy mode).
- If the Blink-side render savings genuinely matter, they must be re-earned some other way (usually they don't: below-fold render cost is small next to a WebKit stall).

## 2. Large-radius Gaussian blurs (`filter: blur(...)`, Tailwind `blur-[NNpx]`, `blur-2xl/3xl`)

Cost ≈ element area × radius, paid in renderer rasterization on the mobile GPU/CPU at first paint. One 300×300px div with `blur-[125px]` is a huge blur surface; a full-section `inset-0 blur-3xl` wash is worse. Several of these in the initial viewport add whole seconds on-device.

**The worst case is an animated transform on a blurred element** — `scale()`/`translate` keyframes on a `blur-3xl` glow re-rasterize the blur every frame, permanently pinning the renderer. (Transform animations are only compositor-cheap on *unfiltered* layers.)

### The fix: radial-gradient glows (visually equivalent, zero filter)

Decorative soft glows — the dominant use of big blurs — are just an ellipse of color fading to transparent. A radial gradient renders the same look with no filter:

```jsx
// BEFORE — renderer-hostile
<div className="absolute top-[35%] left-[34%] h-[28%] w-[35%] rounded-full bg-[#7550FF]/30 blur-[126px]" />

// AFTER — same look, no filter
<div
  className="absolute top-[7%] left-[-1%] h-[85%] w-[106%]"
  style={{ background: "radial-gradient(closest-side, rgba(117,80,255,0.3), transparent 70%)" }}
/>
```

Sizing rule so the gradient's falloff lands where the blur's spread used to: grow each box dimension to

```
new = (old + 2 × blurRadius) / 0.7
```

(the `/0.7` matches the `transparent 70%` stop), keeping the same center. Resolve Tailwind color tokens to rgba at the same alpha. Two special cases:

- **Blur on top of an already-smooth gradient** (a `bg-gradient-to-br` wash, or a div whose fill is already a radial-gradient): the blur is a visual no-op — delete the blur class, change nothing else, do NOT grow the box.
- Keep any transform animation (wander/float) on the converted div — it becomes compositor-cheap the moment the filter is gone.

Always verify with before/after screenshots at render size on the dark/light background it sits on; at typical glow opacities the two are pixel-indistinguishable.

## 3. `backdrop-filter` (`backdrop-blur-*`)

Forces a compositing layer and re-filters everything behind the element whenever it changes. Cost scales with the *element's* area. Triage rather than purge:

- **Fine**: small surfaces (pills, chips, dropdown panels), and anything gated behind interaction or scroll (`data-[scrolled=true]:backdrop-blur` on a header is the correct pattern — nothing paid at first load).
- **Suspect**: full-width bars or phone-sized panels present at first paint, stacks of nested backdrop-filters, and any backdrop-filter combined with `mix-blend-*` on the same element (the expensive combination).
- Changing these alters a real glass aesthetic — measure first (`?noblur` mode strips them too), and prefer shrinking what's *behind* them (converting glows per §2) before touching the glass itself.

## 4. Infinite animations at load

`transform`/`opacity`-only keyframes on unfiltered elements are compositor-cheap — a floating hero image is fine. The hazards are: animating a filtered element (§2), animating `width/height/top/left` (layout per frame), and un-gated animation on elements that are off-screen or in background tabs (gate with IntersectionObserver + `document.hidden`, and respect `prefers-reduced-motion`).

## Ruling animations in or out in one run

Emulate `prefers-reduced-motion: reduce` (Playwright `page.emulateMedia`) against the same build: well-built sites disable entrance/looping animation under it. If timings don't move, animations are innocent — look at §1/§2. This A/B takes two page loads and has settled arguments that code-reading could not.
