#!/usr/bin/env node
/**
 * A latency proxy that makes local Lighthouse runs mean something.
 *
 * Serving a build from localhost puts observed first paint at ~150ms, which collapses
 * Lantern's pre-paint window: nearly the whole page lands inside it, simulated LCP
 * degenerates into "time to download everything", and byte reductions can move the
 * local score the WRONG way. Adding a realistic per-request delay restores a request
 * graph shaped like the deployed one, so local deltas transfer.
 *
 * Usage:
 *   npx --yes serve build/client -l 4173 &
 *   node latency_proxy.mjs --target http://localhost:4173 --port 4174 --delay 120
 *   python3 lh_report.py --url http://localhost:4174/ --runs 3
 *
 * Calibrate: adjust --delay until this rig's observedLargestContentfulPaint lands
 * within ~10% of the deployed site's. ~120ms typically reproduces a GitHub Pages-class
 * host. Then trust DELTAS from the rig, never its absolute numbers; it carries
 * constant per-request think-time production does not have.
 *
 * Two honest caveats to repeat in any report built on this rig:
 *   - Absolute numbers are inflated; only deltas transfer.
 *   - This is HTTP/1.1 while production is probably HTTP/2, so changes that cut
 *     REQUEST COUNT (chunk merging) look better here than they will in production.
 *     Byte reductions transfer cleanly; request-count reductions do not.
 *
 * Node standard library only.
 */

import http from "node:http";
import { argv, exit } from "node:process";

function arg(name, fallback) {
  const i = argv.indexOf(`--${name}`);
  return i !== -1 && argv[i + 1] ? argv[i + 1] : fallback;
}

const target = arg("target", "http://localhost:4173");
const port = Number(arg("port", "4174"));
const delay = Number(arg("delay", "120"));

let targetUrl;
try {
  targetUrl = new URL(target);
} catch {
  console.error(`invalid --target: ${target}`);
  exit(1);
}

if (!Number.isFinite(port) || !Number.isFinite(delay)) {
  console.error("--port and --delay must be numbers");
  exit(1);
}

let inFlight = 0;

const server = http.createServer((req, res) => {
  inFlight += 1;
  // Delay before forwarding, so the cost lands on request latency the way a real
  // round trip does, rather than on transfer time.
  setTimeout(() => {
    const upstream = http.request(
      {
        hostname: targetUrl.hostname,
        port: targetUrl.port || 80,
        path: req.url,
        method: req.method,
        headers: { ...req.headers, host: targetUrl.host },
      },
      (upstreamRes) => {
        res.writeHead(upstreamRes.statusCode ?? 502, upstreamRes.headers);
        upstreamRes.pipe(res);
        upstreamRes.on("end", () => { inFlight -= 1; });
      },
    );

    upstream.on("error", (err) => {
      inFlight -= 1;
      // Surface upstream failures instead of hanging the audit on a dead server.
      if (!res.headersSent) res.writeHead(502, { "content-type": "text/plain" });
      res.end(`latency-proxy: upstream error: ${err.message}\n`);
    });

    req.pipe(upstream);
  }, delay);
});

server.on("error", (err) => {
  console.error(`latency-proxy: ${err.message}`);
  exit(1);
});

server.listen(port, () => {
  console.log(`latency-proxy: :${port} -> ${target}  (+${delay}ms per request)`);
  console.log("calibrate --delay until observed LCP matches the deployed site, then trust deltas only");
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    console.log(`\nlatency-proxy: shutting down (${inFlight} in flight)`);
    server.close(() => exit(0));
  });
}
