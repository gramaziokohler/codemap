# /// script
# requires-python = ">=3.11"
# ///
# ruff: noqa: E501 (the script for the browser)
"""Work on template.html live: serves it on localhost with models made beforehand, and follows every save.

    uv run codemap.py --config examples/codemap.toml --root REPO --out codemap/dev/NAME --json codemap/dev/NAME.json
    uv run dev.py [--port 8765] [--models codemap/dev]

Each NAME.json is a page of its own (/NAME). A change inside <style> is swapped in place, the camera untouched; any
other change reloads the page and puts the view, cut, strata, rooms, selection and camera back. Standard library only.
The three.js and ELK scripts are linked instead of embedded, so line numbers in the browser are the template's.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "template.html"
VENDOR = ("three.min.js", "OrbitControls.js", "elk.bundled.js")
STYLE = re.compile(r"(<style>)(.*?)(</style>)", re.S)
MODELS = HERE / "codemap" / "dev"

# Runs after the page's own script, whose top-level variables and functions it reads.
CLIENT = """<script>
(() => {
  const KEY = "codemap-dev:" + location.pathname;
  let [css, page] = "__REV__".split(" ");
  const save = () => { try {
    sessionStorage.setItem(KEY, JSON.stringify({ viewKind, colorBy, cut, grouping, selected, scroll: scrollY,
      panel: $("#panel").scrollTop, zoom: camera.zoom, pos: camera.position.toArray(), target: controls.target.toArray() }));
  } catch (e) { /* the page's script failed: keep what was saved before */ } };
  try { // what was saved goes only once it is back, so it survives a save that breaks the script
    const s = JSON.parse(sessionStorage.getItem(KEY) || "null");
    if (s) {
      const press = (id, attr, v) => document.querySelectorAll(`#${id} button`).forEach((b) => b.setAttribute("aria-pressed", b.dataset[attr] === v));
      useView(s.grouping); colorBy = s.colorBy; cut = Math.min(s.cut, TIERS.length - 1);
      press("view-tabs", "v", s.viewKind); press("color-tabs", "c", s.colorBy); press("group-tabs", "g", s.grouping);
      build(); legend(); cutUI(); table(); coupling(); setView(s.viewKind);
      camera.position.fromArray(s.pos); camera.zoom = s.zoom; camera.updateProjectionMatrix();
      controls.target.fromArray(s.target); controls.update(); select(s.selected);
      scrollTo(0, s.scroll); $("#panel").scrollTop = s.panel; needs = true;
    }
    sessionStorage.removeItem(KEY);
  } catch (e) { console.warn("codemap dev: the view was not restored", e); }
  // asks now and then, in a tab that is looked at: a connection kept open per tab would use up the few a browser has
  let busy = false;
  const check = async () => {
    if (busy || document.hidden) return;
    busy = true;
    try {
      const r = await fetch("/__rev" + location.pathname, { signal: AbortSignal.timeout(3000) });
      const [c, p] = r.ok ? (await r.text()).split(" ") : [css, page];
      if (p !== page) { save(); location.reload(); return; }
      if (c !== css) {
        document.querySelector("style").textContent = await (await fetch("/__css")).text();
        css = c;
        try { for (const l of labelEls) l.w = 0; resize(); } catch (err) { /* the page's script failed */ }
      }
    } catch (e) { /* the server is away: ask again */ }
    busy = false;
  };
  setInterval(check, 500);
  document.addEventListener("visibilitychange", check);
})();
</script>
"""


def parts(model: Path | None = None) -> tuple[str, str, str]:
    """The template, its style, and a revision: a hash of the style and one of the rest (and of the model)."""
    html = TEMPLATE.read_text(encoding="utf-8")
    if not html.strip():
        raise OSError("the template is empty: in the middle of a save")
    m = STYLE.search(html)
    css = m.group(2) if m else ""
    rest = STYLE.sub(r"\1\3", html, count=1)
    rest += str(model.stat().st_mtime_ns) if model else ""
    return html, css, " ".join(hashlib.sha1(s.encode()).hexdigest()[:12] for s in (css, rest))


def models() -> list[Path]:
    return sorted(MODELS.glob("*.json"), key=lambda p: -p.stat().st_size)


def page(model: Path) -> str:
    html, _, rev = parts(model)
    data = json.dumps(json.loads(model.read_text(encoding="utf-8")), separators=(",", ":")).replace("</", "<\\/")
    tags = "".join(f'<script src="/vendor/{f}"></script>' for f in VENDOR)
    html = html.replace("<script>/*__VENDOR__*/</script>", tags).replace("/*__DATA__*/null", data)
    head, body, tail = html.rpartition("</body>")
    return head + CLIENT.replace("__REV__", rev) + body + tail if body else html + CLIENT.replace("__REV__", rev)


class Handler(BaseHTTPRequestHandler):
    def send(self, text: str, kind: str = "text/html", status: int = 200) -> None:
        raw = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        try:
            self.answer(unquote(urlsplit(self.path).path).strip("/"))
        except ConnectionError:  # the browser went away
            pass
        except (OSError, ValueError) as e:  # caught in the middle of a save: the page asks again
            self.send(f"<script>setTimeout(() => location.reload(), 1000)</script>waiting for a file being written: {e}", status=503)

    def answer(self, path: str) -> None:
        found = {p.stem: p for p in models()}
        if path == "__css":
            return self.send(parts()[1], "text/css")
        if path.startswith("__rev/") and path[6:] in found:
            return self.send(parts(found[path[6:]])[2], "text/plain")
        if path == "__events":  # tabs opened when this was a lasting connection: reload them, once
            return self.send("data: reload reload\n\n", "text/event-stream")
        if path.startswith("vendor/") and path[7:] in VENDOR:
            return self.send((HERE / path).read_text(encoding="utf-8"), "text/javascript")
        if path in found:
            return self.send(page(found[path]))
        if path == "" and found:
            self.send_response(302)
            self.send_header("Location", "/" + next(iter(found)))
            self.send_header("Content-Length", "0")
            return self.end_headers()
        self.send(f"no model here; the models in {MODELS}: {', '.join(found) or 'none'}", "text/plain", 404)

    def log_message(self, *args: object) -> None:
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # on Windows it lets a second server take the same port


def main() -> int:
    global MODELS
    ap = argparse.ArgumentParser(prog="dev", description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--models", default=str(MODELS), help="the folder of the models (codemap.py --json)")
    args = ap.parse_args()
    MODELS = Path(args.models).resolve()
    if not models():
        print(f"no models in {MODELS}: make one first, see the top of dev.py")
        return 1
    try:
        server = Server(("127.0.0.1", args.port), Handler)
    except OSError as e:
        print(f"port {args.port} is taken ({e.strerror}): try --port {args.port + 1}")
        return 1
    for m in models():
        print(f"http://localhost:{args.port}/{m.stem}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
