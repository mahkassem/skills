// RUM proxy — on-device performance measurement for any HTTP upstream.
//
//   UPSTREAM_HOST=127.0.0.1 UPSTREAM_PORT=3000 LISTEN_PORT=9090 node rum-proxy.mjs
//
// Forwards :LISTEN_PORT -> UPSTREAM, injecting a timing beacon into HTML
// responses and logging every request (with timings) to rum.log next to
// this file. The beacon posts back to /__rum ~4s after `load` and again on
// pagehide. Query-param bisection modes rewrite the served HTML so you can
// strip one suspect at a time from a real device without rebuilding:
//   ?nojs      remove all app <script>s (beacon survives)
//   ?noinline  remove inline <script>s only
//   ?nocv      neuter content-visibility utility classes (default: "cv-auto")
//   ?noanim    strip entrance-animation classes + reveal attributes
//   ?noblur    strip Tailwind blur/backdrop-blur utilities
// Adapt the regexes in the mode block to the project's class names.
//
// Safari HTTPS-First probes show up as `CLIENT-ERR ... firstByte=0x16`
// (a TLS ClientHello on the plain-HTTP port).
import http from "node:http";
import { appendFileSync } from "node:fs";

const UPSTREAM = process.env.UPSTREAM_HOST || "127.0.0.1";
const UPSTREAM_PORT = Number(process.env.UPSTREAM_PORT || 3000);
const LISTEN_PORT = Number(process.env.LISTEN_PORT || 9090);
const CV_CLASS = process.env.CV_CLASS || "cv-auto";

const LOG = new URL("./rum.log", import.meta.url).pathname;
const t0 = Date.now();
const log = (line) => {
  const s = `[+${((Date.now() - t0) / 1000).toFixed(2)}s] ${line}`;
  console.log(s);
  appendFileSync(LOG, s + "\n");
};

const BEACON = `<script __rumkeep>(function(){try{
var D={ua:navigator.userAgent,url:location.href,lcp:null,res:[]};
try{new PerformanceObserver(function(l){var e=l.getEntries();var last=e[e.length-1];if(last)D.lcp={t:Math.round(last.startTime),tag:last.element&&last.element.tagName,sz:last.size}}).observe({type:'largest-contentful-paint',buffered:true})}catch(e){}
var GAPS=[];var hb=performance.now();setInterval(function(){var n=performance.now();if(n-hb>300)GAPS.push({from:Math.round(hb),to:Math.round(n)});hb=n},50);
function send(){try{
D.gaps=GAPS;D.mode=location.search;
var n=performance.getEntriesByType('navigation')[0]||{};
D.nav={dns:Math.round(n.domainLookupEnd-n.domainLookupStart),connect:Math.round(n.connectEnd-n.connectStart),ttfb:Math.round(n.responseStart),htmlDone:Math.round(n.responseEnd),domInteractive:Math.round(n.domInteractive),dcl:Math.round(n.domContentLoadedEventEnd),load:Math.round(n.loadEventEnd)};
D.paint=performance.getEntriesByType('paint').map(function(p){return{n:p.name,t:Math.round(p.startTime)}});
D.res=performance.getEntriesByType('resource').map(function(r){return{u:r.name.replace(location.origin,'').slice(0,80),s:Math.round(r.startTime),d:Math.round(r.duration),b:r.transferSize||0}}).sort(function(a,b){return a.s-b.s}).slice(0,60);
navigator.sendBeacon('/__rum',JSON.stringify(D));
}catch(e){navigator.sendBeacon('/__rum','{"err":"'+e.message+'"}')}}
addEventListener('load',function(){setTimeout(send,4000)});
addEventListener('pagehide',send);
}catch(e){}})();</script>`;

const server = http.createServer((req, res) => {
  const start = Date.now();
  if (req.url === "/__rum") {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      log("BEACON " + body);
      res.writeHead(204).end();
    });
    return;
  }
  const wantsHtml = (req.headers.accept || "").includes("text/html");
  const headers = { ...req.headers, host: `${UPSTREAM}:${UPSTREAM_PORT}` };
  if (wantsHtml) headers["accept-encoding"] = "identity"; // plain body so we can inject
  const up = http.request(
    { host: UPSTREAM, port: UPSTREAM_PORT, path: req.url, method: req.method, headers },
    (ur) => {
      const isHtml = (ur.headers["content-type"] || "").includes("text/html");
      if (isHtml && wantsHtml) {
        let buf = "";
        ur.setEncoding("utf8");
        ur.on("data", (c) => (buf += c));
        ur.on("end", () => {
          const q = req.url.split("?")[1] || "";
          let page = buf;
          if (q.includes("nojs"))
            page = page.replace(/<script(?![^>]*__rumkeep)[^>]*>[\s\S]*?<\/script>/g, "");
          if (q.includes("noinline"))
            page = page.replace(/<script(?![^>]*src=)[^>]*>[\s\S]*?<\/script>/g, "");
          if (q.includes("nocv")) page = page.replaceAll(CV_CLASS, "cv-off");
          if (q.includes("noanim"))
            page = page
              .replace(/animate-[\w-]+/g, "anim-off")
              .replace(/data-reveal="[^"]*"/g, "");
          if (q.includes("noblur"))
            page = page.replace(/(?:backdrop-)?blur-\[?[\w.\]]*\]?/g, "");
          const out = page.replace("</head>", BEACON + "</head>");
          const h = { ...ur.headers };
          delete h["content-length"];
          delete h["content-encoding"];
          res.writeHead(ur.statusCode, h);
          res.end(out);
          log(`REQ ${req.method} ${req.url} -> ${ur.statusCode} html+beacon ${Date.now() - start}ms`);
        });
      } else {
        res.writeHead(ur.statusCode, ur.headers);
        ur.pipe(res);
        ur.on("end", () =>
          log(`REQ ${req.method} ${req.url} -> ${ur.statusCode} ${ur.headers["content-length"] || "?"}B ${Date.now() - start}ms`),
        );
      }
    },
  );
  up.on("error", (e) => {
    log(`UPSTREAM-ERR ${req.url} ${e.message}`);
    res.writeHead(502).end("upstream error");
  });
  req.pipe(up);
});

server.on("clientError", (err, socket) => {
  const first = err.rawPacket ? err.rawPacket[0] : null;
  log(`CLIENT-ERR ${err.code} firstByte=0x${first ? first.toString(16) : "?"}${first === 0x16 ? " <-- TLS ClientHello on plain HTTP port (HTTPS-First probe)" : ""}`);
  socket.destroy();
});
server.on("connection", (s) => log(`CONN from ${s.remoteAddress}`));
server.listen(LISTEN_PORT, "0.0.0.0", () =>
  log(`rum-proxy listening on :${LISTEN_PORT} -> ${UPSTREAM}:${UPSTREAM_PORT}`),
);
