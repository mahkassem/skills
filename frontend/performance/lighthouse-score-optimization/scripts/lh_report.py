#!/usr/bin/env python3
"""Run Lighthouse audits and report medians plus the diagnostics that actually matter.

Two modes:

    # run N audits against a URL, then diagnose
    python3 lh_report.py --url https://example.com/ --runs 3

    # diagnose runs you already have
    python3 lh_report.py --parse lh-*.json

Why this exists: the HTML report tells you what is slow, but the JSON tells you why:
LCP phase breakdown, render-blocking resources, per-request priority, and the observed
(unthrottled) timings that reveal whether your measurement harness is healthy at all.
Extracting those by hand every time is the tax this script removes.

Cold vs warm: by default each run starts a fresh Chrome, which on some machines stalls
1-2s before first paint and silently inflates Speed Index for every page you measure.
Pass --warm to reuse one persistent Chrome instead; if observed FCP drops dramatically,
that gap was your harness, not the page. Report both when they disagree.

Standard library only. Requires `npx lighthouse` to be resolvable.
"""

import argparse
import glob
import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import time

METRICS = [
    ("score", "Score", ""),
    ("fcp", "FCP", "ms"),
    ("lcp", "LCP", "ms"),
    ("tbt", "TBT", "ms"),
    ("cls", "CLS", ""),
    ("si", "SI", "ms"),
]

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]


def extract(report):
    """Pull the numbers worth looking at out of one Lighthouse JSON report."""
    audits = report["audits"]

    def num(key, default=0.0):
        item = audits.get(key) or {}
        value = item.get("numericValue")
        return default if value is None else value

    observed = {}
    metrics_audit = audits.get("metrics") or {}
    items = (metrics_audit.get("details") or {}).get("items") or [{}]
    if items:
        observed = items[0]

    return {
        "score": round((report["categories"]["performance"]["score"] or 0) * 100),
        "fcp": round(num("first-contentful-paint")),
        "lcp": round(num("largest-contentful-paint")),
        "tbt": round(num("total-blocking-time")),
        "cls": round(num("cumulative-layout-shift"), 3),
        "si": round(num("speed-index")),
        "obs_fcp": observed.get("observedFirstContentfulPaint"),
        "obs_lcp": observed.get("observedLargestContentfulPaint"),
        "obs_si": observed.get("observedSpeedIndex"),
        "audits": audits,
    }


def details(audits, key):
    return ((audits.get(key) or {}).get("details") or {}).get("items") or []


def print_runs(rows, labels):
    print("\n=== RUNS ===")
    for label, row in zip(labels, rows):
        parts = [f"{name}={row[key]}{unit}" for key, name, unit in METRICS]
        obs = f"obsFCP={row['obs_fcp']}" if row["obs_fcp"] is not None else ""
        print(f"  {label:<28} {'  '.join(parts)}  {obs}")

    if len(rows) > 1:
        print("\n=== MEDIAN ===")
        med = {}
        for key, name, unit in METRICS:
            med[key] = statistics.median([r[key] for r in rows])
            shown = round(med[key], 3) if key == "cls" else round(med[key])
            print(f"  {name:<6} {shown}{unit}")
        return med
    return rows[0]


def print_health(rows):
    """Why a metric is stuck: instrument, or page. Check BOTH; they look identical."""
    obs = [r["obs_fcp"] for r in rows if r["obs_fcp"] is not None]
    if not obs:
        return
    median_obs = statistics.median(obs)
    print("\n=== INSTRUMENT HEALTH ===")
    print(f"  observed FCP (unthrottled, real wall-clock): median {round(median_obs)}ms")
    if median_obs > 1500:
        print("  ^ HIGH. Often browser cold-start being charged to the page. Speed Index")
        print("    carries ~1.4x of it, so it costs points on ANY page. Confirm with --warm")
        print("    and a control site. But confirm the page-side causes below too. Finding")
        print("    a stall is not permission to stop looking.")
    else:
        print("  ^ healthy; simulated numbers should respond to real page changes.")

    print("\n=== SPEED INDEX DECOMPOSITION ===")
    print("  SI ~= max(simFCP, 1.4 x observedSI + 0.4 x layoutSim). The 1.4 term is a REAL")
    print("  observed number, so whatever share it holds is not yours to optimize.")
    print(f"  {'run':<6} {'SI':>7} {'obsSI':>7} {'1.4xobsSI':>10} {'share':>7} {'yours':>7}")
    for i, row in enumerate(rows, 1):
        si, obs_si = row["si"], row["obs_si"]
        if not si or obs_si is None:
            continue
        term = 1.4 * obs_si
        print(f"  {i:<6} {si:>7} {round(obs_si):>7} {round(term):>10} "
              f"{round(100 * term / si):>6}% {round(max(si - term, 0)):>7}")

    print("\n=== DID THE PAGE SETTLE? (page-side cause of a stuck SI) ===")
    for i, row in enumerate(rows, 1):
        audits = row["audits"]
        items = ((audits.get("metrics") or {}).get("details") or {}).get("items") or [{}]
        m = items[0] if items else {}
        last, end = m.get("observedLastVisualChange"), m.get("observedTraceEnd")
        if last is None or end is None:
            continue
        slack = end - last
        flag = "  <-- still repainting when recording stopped" if slack < 100 else ""
        print(f"  run {i}: lastVisualChange={last}ms traceEnd={end}ms slack={slack}ms{flag}")
    print("  Small slack => an above-the-fold element is animating forever. Speed Index")
    print("  scores completeness against the FINAL frame, so nothing you cut will move it.")

    print("\n=== NON-COMPOSITED ANIMATIONS (informational audit; may score 1) ===")
    audits = rows[0]["audits"]
    nca = audits.get("non-composited-animations") or {}
    entries = details(audits, "non-composited-animations")
    if not entries:
        print("  none")
    else:
        print(f"  {len(entries)} element(s) animating off the compositor (audit score={nca.get('score')})")
        reasons = set()
        for entry in entries:
            for sub in (entry.get("subItems") or {}).get("items", []):
                if sub.get("failureReason"):
                    reasons.add(sub["failureReason"])
        for reason in sorted(reasons):
            print(f"    - {reason}")
        print("  These force style/layout on the main thread every keyframe.")


def print_diagnosis(row):
    audits = row["audits"]

    print("\n=== LCP ELEMENT + PHASES ===")
    for entry in details(audits, "largest-contentful-paint-element"):
        for sub in entry.get("items", []):
            if "node" in sub:
                snippet = (sub["node"].get("snippet") or "")[:180]
                print(f"  element: {snippet}")
            elif "phase" in sub:
                print(f"    {sub['phase']:<12} {round(sub.get('timing', 0)):>6}ms  {sub.get('percent', '')}")
    print("  (Render Delay dominant => element was ready, something blocked painting:")
    print("   look at render-blocking + priority inversions below, not the element size.)")

    print("\n=== RENDER-BLOCKING ===")
    blocking = details(audits, "render-blocking-resources")
    if not blocking:
        print("  none")
    for item in blocking:
        name = item.get("url", "").split("/")[-1][:60]
        print(f"  {name:<60} {item.get('totalBytes', 0):>8}B  {round(item.get('wastedMs', 0))}ms")

    requests = details(audits, "network-requests")
    if requests:
        print("\n=== NETWORK: top 20 by transfer size ===")
        print(f"  {'TYPE':<12} {'SIZE':>8}  {'PRIORITY':<10} {'END':>7}  NAME")
        ranked = sorted(requests, key=lambda r: r.get("transferSize", 0), reverse=True)
        for item in ranked[:20]:
            name = item.get("url", "").split("/")[-1][:44]
            size_kb = round(item.get("transferSize", 0) / 1024)
            end = item.get("networkEndTime", 0)
            print(
                f"  {str(item.get('resourceType'))[:12]:<12} {str(size_kb) + 'KB':>8}  "
                f"{str(item.get('priority'))[:10]:<10} {round(end):>6}ms  {name}"
            )
        print("  (Fonts at VeryHigh ahead of a render-blocking stylesheet = priority")
        print("   inversion; preloading those fonts moves them to High. See fix-catalog.)")

    total = sum(r.get("transferSize", 0) for r in requests)
    scripts = sum(r.get("transferSize", 0) for r in requests if r.get("resourceType") == "Script")
    fonts = sum(r.get("transferSize", 0) for r in requests if r.get("resourceType") == "Font")
    images = sum(r.get("transferSize", 0) for r in requests if r.get("resourceType") == "Image")
    print("\n=== BYTE BUDGET (scripts count at full weight in the LCP graph;")
    print("    low-priority images roughly half, so cut scripts first) ===")
    for label, value in (("total", total), ("scripts", scripts), ("fonts", fonts), ("images", images)):
        print(f"  {label:<9} {round(value / 1024):>6} KB")


def find_chrome():
    for path in CHROME_CANDIDATES:
        if path and os.path.exists(path):
            return path
    return None


def run_audits(url, runs, out_dir, warm, extra_flags):
    os.makedirs(out_dir, exist_ok=True)
    paths, chrome, port = [], None, 9333

    if warm:
        binary = find_chrome()
        if not binary:
            print("warm mode: no Chrome binary found; falling back to cold runs", file=sys.stderr)
            warm = False
        else:
            profile = os.path.join(out_dir, "chrome-profile")
            chrome = subprocess.Popen(
                [binary, "--headless=new", f"--remote-debugging-port={port}",
                 f"--user-data-dir={profile}", "about:blank"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            time.sleep(4)

    try:
        for i in range(1, runs + 1):
            path = os.path.join(out_dir, f"lh-{i}.json")
            cmd = ["npx", "--yes", "lighthouse", url,
                   "--only-categories=performance", "--output=json",
                   f"--output-path={path}", "--quiet"]
            cmd += [f"--port={port}"] if warm else ["--chrome-flags=--headless=new"]
            cmd += extra_flags
            print(f"run {i}/{runs} ...", file=sys.stderr)
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0 or not os.path.exists(path):
                print(result.stderr[-2000:], file=sys.stderr)
                raise SystemExit(f"lighthouse failed on run {i}")
            paths.append(path)
    finally:
        if chrome:
            chrome.send_signal(signal.SIGTERM)

    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", help="URL to audit")
    parser.add_argument("--runs", type=int, default=3, help="number of runs (default 3; never trust 1)")
    parser.add_argument("--warm", action="store_true",
                        help="reuse one persistent Chrome instead of a fresh one per run")
    parser.add_argument("--out-dir", default="./lh-out", help="where to write run JSON")
    parser.add_argument("--parse", nargs="+", help="analyze existing JSON reports instead of running")
    parser.add_argument("--lh-flag", action="append", default=[],
                        help="extra flag passed through to lighthouse (repeatable)")
    args = parser.parse_args()

    if not args.url and not args.parse:
        parser.error("give --url to run audits, or --parse to analyze existing reports")

    if args.parse:
        paths = []
        for pattern in args.parse:
            paths.extend(sorted(glob.glob(pattern)) or [pattern])
    else:
        paths = run_audits(args.url, args.runs, args.out_dir, args.warm, args.lh_flag)

    rows = []
    for path in paths:
        with open(path) as handle:
            rows.append(extract(json.load(handle)))

    labels = [os.path.basename(p) for p in paths]
    print_runs(rows, labels)
    print_health(rows)

    # Diagnose the run closest to the median score, so the detail matches the headline.
    median_score = statistics.median([r["score"] for r in rows])
    representative = min(rows, key=lambda r: abs(r["score"] - median_score))
    print_diagnosis(representative)


if __name__ == "__main__":
    main()
