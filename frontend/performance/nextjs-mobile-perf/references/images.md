# Image Weight & Fetch Priority

## The alpha-channel trap (webp)

A webp with transparency carries a separate alpha plane, and `cwebp`'s default `-alpha_q 100` encodes it near-losslessly. For images with large soft shadows or glows baked into the alpha (hero device composites, floating cards), **the alpha plane can be 3–4× the size of the visible pixels** — a 264KB file whose color data is ~60KB. Quality sliders in design tools don't touch it, which is why "export at 60%" barely shrinks these files.

Diagnose: `webpinfo file.webp` (shows `Alpha: 1`), and the tell is that re-encoding at q65 vs q50 barely changes the size.

Fix (visually safe for decorative composites):

```bash
dwebp input.webp -o /tmp/x.png
cwebp -q 70 -alpha_q 60 -m 6 -sharp_yuv /tmp/x.png -o output.webp
```

Typical result: 264KB → 72KB at identical dimensions. Verify with a side-by-side at render size over the page's real background (banding in the shadow gradient is the failure mode; q70/alpha_q 60 is usually clean, q60/alpha_q 50 usually still clean).

## The `/_next/image` re-encoding trap

The Next.js optimizer decodes the source and re-encodes with sharp at `alphaQuality: 100` — **it restores the alpha bloat you just removed**. Measured: a 72KB hand-tuned source came back as a 107KB `w=750` variant. The `quality` prop only moves the color plane.

For the one or two hand-tuned hero assets, serve the file verbatim:

```jsx
<Image src="/hero.webp" width={1156} height={1200} priority unoptimized ... />
```

Per-image `unoptimized` keeps the rest of the site on the optimizer. You lose responsive variants for that asset — acceptable when the tuned file is already smaller than any variant the optimizer would emit. Note the GitHub-Pages/static-export target (`images.unoptimized: true`) always serves sources verbatim, so tuning the source file is the only lever there anyway.

## Priority: the preload that starves your CSS

`priority` on `next/image` emits a `<link rel="preload" as="image">`; adding `fetchPriority="high"` makes that preload compete at High priority with the render-blocking stylesheet and font preloads. On a narrow mobile pipe this delays **first paint of everything** — measured: CSS finished at 2.8s behind a high-priority hero image, FCP 2.9s.

Decision rule:
- Image is the LCP element and sits beside/above the text (desktop hero split): `priority` + high hint can be right.
- Mobile layout stacks the image *below* the copy (the common responsive hero): keep `priority` (early discovery via preload) but **omit `fetchPriority="high"`** — the browser upgrades in-viewport images at layout, after CSS/fonts have won the first bandwidth window. Both FCP and LCP improve.

`sizes` should tell the truth about the rendered slot (`(min-width: 1024px) 578px, 100vw`); a missing `sizes` on a `fill`/responsive image fetches desktop-width variants on phones.

## Reading the waterfall honestly

- Get real transfer sizes from the network-requests audit of a **devtools-throttled** Lighthouse run, or from the RUM proxy's server log — not from simulated-mode estimates.
- Check what the optimizer actually serves: `curl -H "Accept: image/webp" "<host>/_next/image?url=...&w=750&q=75" -o /dev/null -w "%{size_download}"`. Without the Accept header you get the jpeg fallback and a misleading number.
- Below-fold images: `next/image` defaults to `loading="lazy"` — confirm none carry stray `priority`, then stop; they are not the initial-load problem.
- Fonts: each preloaded `next/font` face is ~14KB × weights on the critical path. Only the above-the-fold face earns preload; the rest take `preload: false`.
