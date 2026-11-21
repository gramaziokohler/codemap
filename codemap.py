# /// script
# requires-python = ">=3.11"
# dependencies = ["tree-sitter-language-pack==1.25.2", "networkx==3.7", "numpy>=2"]
# ///
"""codemap: a spatial map of a code base, to see what the code is and where to look.

    uv run codemap.py [--config codemap.toml] [--root REPO] [--out DIR] [--json FILE]

Reads every source file (tree-sitter: Python, C#, C++, TypeScript, JavaScript; no docs, no tests), sorts each file
into a classic architectural tier by what its code uses (presentation, application, domain, data, integration),
groups the files of a tier into rooms twice: by how they depend on each other and change together, and by the
folders they are filed in; measures how far the two agree. Places the rooms on their tier's platform (a component
sits at the same spot on every platform and in both views; anchoring hints in the config pull a component toward a
side), measures each room (size, complexity, coupling, duplication, magic values, swallowed errors, hotspots) and
writes one static HTML page (three.js and ELK, embedded). Everything is derived anew on each run, with fixed seeds.
Nothing in here knows about one repository: that is the config.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import math
import os
import re
import subprocess
import sys
import time
import tomllib
from collections import Counter, defaultdict
from pathlib import Path

import networkx as nx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import precise  # noqa: E402
from extract import FileRecord, read  # noqa: E402

HERE = Path(__file__).resolve().parent

# ----------------------------------------------------------------------------------------------- layers and tiers
# Semi-standard layers, detected from what a file uses, not declared per repository. A file gets the layer of the
# strongest concern it shows; a file that shows several I/O concerns at once is "mixed" (a layer violation).
LAYERS = ["presentation", "interface", "application", "domain", "infrastructure", "adapter", "test"]
# The classic tiers the map's platforms stand for, top to bottom, and the layers each one gathers.
TIERS = [
    ("presentation", "Presentation", "what people see and click: UI, panels, the viewer", ["presentation"]),
    (
        "application",
        "Application",
        "entry points and services: HTTP APIs and clients, CLIs, orchestration",
        ["interface", "application"],
    ),
    ("domain", "Domain", "the model and its rules: logic that touches no I/O", ["domain"]),
    ("data", "Data", "persistence: databases, files, stores", ["infrastructure"]),
    ("integration", "Integration", "adapters to other programs' APIs (CAD hosts, IFC toolkits)", ["adapter"]),
]
TIER_OF_LAYER = {layer: tid for tid, _, _, layers in TIERS for layer in layers}
# where the code says nothing about I/O, a file's path can still tell (a store, a server, a view)
PATH_HINTS = [
    (
        "data",
        re.compile(
            r"(^|[/_.])(store|storage|db|database|repository|repo|dao|persist\w*|migrat\w*|cache)s?([/_.]|$)", re.I
        ),
    ),
    ("application", re.compile(r"(^|[/_.])(server|api|routes?|controllers?|cli|main|app|service)s?([/_.]|$)", re.I)),
    ("presentation", re.compile(r"(^|/)(ui|views?|components?|pages?|panels?|widgets?)(/|$)", re.I)),
]


def tier_of(rec: FileRecord, layer: str) -> str:
    tier = TIER_OF_LAYER.get(layer, "domain")
    if tier == "domain":
        for hint, rx in PATH_HINTS:
            if rx.search(rec.path):
                return hint
    return tier


SIGNS = {
    "storage": re.compile(
        r"\b(sqlite3|pymysql|psycopg|sqlalchemy|SqlConnection|DbContext|Dolt|DOLT_|INSERT INTO|SELECT .* FROM|"
        r"CREATE TABLE|write_text|write_bytes|File\.Write|File\.Create|ofstream|fwrite|fs\.write|writeFile|"
        r"IndexedDB|indexedDB|localStorage|caches\.open|shutil\.(copy|move|rmtree)|os\.remove|unlink\()"
    ),
    "network": re.compile(
        r"\b(http\.server|BaseHTTPRequestHandler|urllib\.request|urlopen|requests\.|httpx|HttpClient|HttpWebRequest|"
        r"fetch\(|XMLHttpRequest|WebSocket|socket\.|aiohttp|ThreadingHTTPServer|curl_|HttpListener)"
    ),
    "ui": re.compile(r"(?!)"),  # per language, below (UI_SIGNS): `document.` means the DOM only in a browser
    "cli": re.compile(r"\b(argparse|sys\.argv|click\.|typer|ArgumentParser|main\(argv)"),
    "host": re.compile(
        r"\b(Rhino\.|RhinoCommon|RhinoDoc|Autodesk\.Revit|ACAPI_|API_Element|GS::|VisualArq|bpy\.|ifcopenshell|"
        r"Eto\.Forms|UIApplication|ExternalEvent|(import|from) (Rhino|bpy|bmesh|Grasshopper)\b|scriptcontext|"
        r"rhinoscriptsyntax|ghpythonlib)"
    ),
}


UI_SIGNS = {
    "typescript": r"\b(document\.|innerHTML|addEventListener|querySelector|createElement|getElementById|React)\b",
    "tsx": r"\b(document\.|innerHTML|addEventListener|querySelector|createElement|getElementById|React)\b",
    "javascript": r"\b(document\.|innerHTML|addEventListener|querySelector|createElement|getElementById)\b",
    "csharp": r"\b(Eto\.Forms|WebView2?|System\.Windows|Dockable\w*|UserControl|ExecuteScript)\b",
    "cpp": r"\b(DG::\w+|DGModeless|UI::|Browser\b)",
    "python": r"\b(tkinter|PySide\d?|PyQt\d?|QtWidgets|bpy\.types\.(Panel|Operator|UIList))\b",
}


def layer_of(rec: FileRecord, text: str) -> tuple[str, list[str]]:
    p = rec.path.lower()
    signs = sorted(k for k, rx in SIGNS.items() if rx.search(text))
    if re.search(UI_SIGNS.get(rec.lang, r"(?!)"), text):
        signs = sorted(set(signs) | {"ui"})
    if re.search(r"(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]*$|_test\.|\.test\.|Tests?/", rec.path):
        return "test", signs
    if "ui" in signs and "host" not in signs and rec.lang in ("typescript", "tsx", "javascript"):
        return "presentation", signs
    if "host" in signs:
        return "adapter", signs
    if "ui" in signs:
        return "presentation", signs
    if "network" in signs:
        return "interface", signs
    if "storage" in signs:
        return "infrastructure", signs
    if "cli" in signs or p.endswith(("cli.py", "__main__.py", "main.py")):
        return "application", signs
    return "domain", signs


def violations(layer: str, signs: list[str]) -> list[str]:
    """Concerns a file of this layer should not carry itself."""
    io = [s for s in signs if s in ("storage", "network", "ui")]
    out = []
    if layer == "domain" and io:
        out.append("domain code with " + ", ".join(io))
    if layer == "presentation" and "storage" in signs:
        out.append("presentation code that stores data")
    if layer in ("interface", "presentation", "adapter") and "storage" in signs and "network" in signs:
        out.append("storage and network in one file")
    if len(io) >= 3:
        out.append("storage, network and UI in one file")
    return out


# ----------------------------------------------------------------------------------------------- reading


def load_config(path: Path) -> dict:
    cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    cfg.setdefault("title", "Code map")
    cfg.setdefault("include", ["**/*"])
    cfg.setdefault("exclude", [])
    cfg.setdefault("generated", [])
    cfg.setdefault("rooms", 18)
    return cfg


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True).stdout
    return out.split("\n")


def selected(path: str, cfg: dict) -> bool:
    if not any(fnmatch.fnmatch(path, p) for p in cfg["include"]):
        return False
    return not any(fnmatch.fnmatch(path, p) for p in cfg["exclude"])


def read_all(root: Path, cfg: dict) -> tuple[dict[str, FileRecord], dict[str, str]]:
    recs, texts = {}, {}
    for rel in tracked_files(root):
        if not rel or not selected(rel, cfg) or is_test(rel):
            continue
        p = root / rel
        if not p.is_file() or p.stat().st_size > 2_000_000:
            continue
        rec = read(p, rel)
        if rec is None:
            continue
        recs[rel] = rec
        texts[rel] = p.read_text(encoding="utf-8", errors="replace")
    return recs, texts


# ----------------------------------------------------------------------------------------------- dependencies


def dependencies(recs: dict[str, FileRecord]) -> dict[tuple[str, str], float]:
    """(a, b) -> how strongly a depends on b: imports resolved to files (weight 2) and names a defines that b uses
    (weight 1 each, only for names defined in at most two files of the same language)."""
    edges: dict[tuple[str, str], float] = defaultdict(float)
    by_lang: dict[str, list[FileRecord]] = defaultdict(list)
    for r in recs.values():
        by_lang[r.lang if r.lang not in ("tsx", "javascript") else "typescript"].append(r)
    # module index for Python, basenames for includes and relative imports
    modules: dict[str, str] = {}
    for r in recs.values():
        if r.lang != "python":
            continue
        parts = r.path[:-3].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        for i in range(len(parts)):
            modules.setdefault(".".join(parts[i:]), r.path)
    stems: dict[str, list[str]] = defaultdict(list)
    for r in recs.values():
        stems[Path(r.path).name].append(r.path)
    for r in recs.values():
        for imp in r.imports:
            targets: list[str] = []
            if r.lang == "python":
                m = re.match(r"\s*from\s+(\.*)([\w.]*)\s+import\s+(.+)", imp, re.S)
                if m:
                    dots, mod, names = m.groups()
                    if dots:
                        base = r.path.split("/")[:-1]
                        base = base[: len(base) - (len(dots) - 1)] if len(dots) > 1 else base
                        mod = ".".join(base + ([mod] if mod else []))
                    cands = [mod] + [f"{mod}.{n.strip()}" for n in re.split(r"[,()\s]+", names) if n.strip()]
                else:
                    cands = [c.strip().split(" as ")[0] for c in imp.replace("import", "", 1).split(",")]
                for c in cands:
                    parts = c.strip(".").split(".")
                    for i in range(len(parts)):
                        hit = modules.get(".".join(parts[i:])) or modules.get(".".join(parts[: len(parts) - i]))
                        if hit:
                            targets.append(hit)
                            break
            elif r.lang == "cpp":
                m = re.search(r'#include\s+"([^"]+)"', imp)
                if m:
                    targets += [p for p in stems.get(Path(m.group(1)).name, [])]
            elif r.lang in ("typescript", "tsx", "javascript"):
                m = re.search(r"from\s+['\"](\.[^'\"]+)['\"]|import\s+['\"](\.[^'\"]+)['\"]", imp)
                if m:
                    spec = m.group(1) or m.group(2)
                    base = os.path.normpath(os.path.join(os.path.dirname(r.path), spec))
                    for ext in ("", ".ts", ".tsx", ".js", ".mjs", "/index.ts", "/index.js"):
                        if base + ext in recs:
                            targets.append(base + ext)
                            break
            for t in targets:
                if t != r.path:
                    edges[(r.path, t)] += 2.0
    # types: a file uses a class or type another file of its language declares. Python is left to its imports
    # (exact); C# has no per-file imports (namespaces), C++ and TypeScript use types beyond their includes.
    for lang, rs in by_lang.items():
        if lang == "python":
            continue
        owners: dict[str, list[str]] = defaultdict(list)
        for r in rs:
            for k in r.classes:
                if len(k.name) >= 3 and not k.name.startswith("_"):
                    owners[k.name].append(r.path)
        for r in rs:
            for u in r.uses:
                own = owners.get(u)
                if own and len(own) <= 2:
                    for o in own:
                        if o != r.path:
                            edges[(r.path, o)] += 1.0
    return edges


def cochange(root: Path, files: set[str], max_files: int = 12) -> tuple[dict[tuple[str, str], float], Counter, dict]:
    """Files that change in the same commits (weight 1/(n-1) per commit), commits per file in the last 30 days,
    and each file's last change (unix time)."""
    log = subprocess.run(
        ["git", "log", "--name-only", "--no-merges", "--format=@%ct"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    pairs: dict[tuple[str, str], float] = defaultdict(float)
    recent: Counter = Counter()
    last: dict[str, int] = {}
    now = time.time()
    for block in log.split("@")[1:]:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        t = int(lines[0])
        changed = [f for f in lines[1:] if f in files]
        for f in changed:
            last.setdefault(f, t)
            if now - t < 30 * 86400:
                recent[f] += 1
        if 1 < len(changed) <= max_files:
            w = 1.0 / (len(changed) - 1)
            for i, a in enumerate(changed):
                for b in changed[i + 1 :]:
                    pairs[(min(a, b), max(a, b))] += w
    return pairs, recent, last


def clones(recs: dict[str, FileRecord], k: int = 60) -> tuple[dict[str, float], dict[tuple[str, str], int], dict]:
    """Token runs of length k that occur in more than one place: the share of each file's tokens inside such a run,
    shared runs per pair of files, and per file the line ranges and the other files of its largest clones."""
    seen: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for r in recs.values():
        toks = r.tokens
        if len(toks) < k:
            continue
        h = 0
        base, mod = 1_000_003, (1 << 61) - 1
        power = pow(base, k - 1, mod)
        for i, t in enumerate(toks):
            if i >= k:
                h = (h - toks[i - k] * power) % mod
            h = (h * base + t) % mod
            if i >= k - 1 and i % 4 == 0:  # sample every 4th window: plenty for runs of 60 tokens
                seen[h].append((r.path, i - k + 1))
    dup_tokens: dict[str, set[int]] = defaultdict(set)
    pairs: dict[tuple[str, str], int] = defaultdict(int)
    where: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for occ in seen.values():
        if len(occ) < 2 or len(occ) > 12:  # boilerplate repeated everywhere is not a clone worth showing
            continue
        for f, i in occ:
            dup_tokens[f].update(range(i, i + k))
        fs = sorted({f for f, _ in occ})
        for a_i, a in enumerate(fs):
            for b in fs[a_i + 1 :]:
                pairs[(a, b)] += 1
        for f, i in occ:
            line = recs[f].token_lines[i]
            for g, _ in occ:
                if g != f:
                    where[f][g].append(line)
    share = {f: len(s) / max(1, len(recs[f].tokens)) for f, s in dup_tokens.items()}
    return share, pairs, where


# ----------------------------------------------------------------------------------------------- rooms


def is_test(path: str) -> bool:
    return bool(re.search(r"(^|/)(tests?|__tests__)/|(^|/)test_[^/]*$|\.test\.|Tests?/", path))


def graph(files: list[str], deps, co) -> nx.Graph:
    g = nx.Graph()  # built in a fixed order: Louvain depends on it, and set order changes from run to run
    g.add_nodes_from(sorted(files))
    for (a, b), w in sorted(deps.items()):
        if a in g and b in g:
            g.add_edge(a, b, weight=g.get_edge_data(a, b, {}).get("weight", 0) + min(w, 6))
    for (a, b), w in sorted(co.items()):
        if a in g and b in g:
            g.add_edge(a, b, weight=g.get_edge_data(a, b, {}).get("weight", 0) + 0.6 * w)
    # a prior: files of one folder, and less so of one component, belong together unless the code says otherwise
    for key, w in ((lambda p: str(Path(p).parent), 3.0), (component, 1.5)):
        groups: dict[str, list[str]] = defaultdict(list)
        for p in g.nodes:
            groups[key(p)].append(p)
        for fs in groups.values():
            fs.sort()
            for a, b in zip(fs, fs[1:]):
                g.add_edge(a, b, weight=g.get_edge_data(a, b, {}).get("weight", 0) + w)
    return g


def derive_rooms(g: nx.Graph, target: int) -> list[set[str]]:
    """Louvain communities at the resolution that comes closest to ``target`` rooms of three files or more; the
    smaller ones folded into the room they are most tied to (or kept, when nothing ties them)."""
    if g.number_of_nodes() <= 3 or target <= 1:
        return [set(g.nodes)] if g.number_of_nodes() else []
    best = None
    for res in (0.3, 0.5, 0.7, 1.0, 1.4, 2.0, 3.0):
        comms = [set(c) for c in nx.community.louvain_communities(g, weight="weight", resolution=res, seed=7)]
        score = abs(sum(1 for c in comms if len(c) >= 3) - target)
        if best is None or score < best[0]:
            best = (score, comms)
    comms = sorted(best[1], key=lambda c: (-len(c), min(c)))
    rooms = [c for c in comms if len(c) >= 3] or comms[:1]
    for small in (c for c in comms if len(c) < 3 and c not in rooms):
        for f in sorted(small):
            ties = Counter()
            for n, d in g[f].items():
                for i, r in enumerate(rooms):
                    if n in r:
                        ties[i] += d["weight"]
            if ties:
                rooms[ties.most_common(1)[0][0]].add(f)
            else:
                rooms.append({f})
    return [r for r in rooms if r]


ADDIN_ROOTS = ("clients", "plugins", "addons", "extensions")


def component(path: str) -> str:
    """A file's component: its folder three levels deep (ir/packages/hil_fed, web/src/hub); an add-in's is its
    program's folder (clients/rhino, clients/blender)."""
    parts = Path(path).parent.parts
    return "/".join(parts[: 2 if parts and parts[0] in ADDIN_ROOTS else 3]) or "."


def room_name(files: set[str], main_pkg: str = "") -> tuple[str, str]:
    """The room's component folder, with a second one when it holds a quarter of the files; a subfolder when the
    first holds most of them. A folder of another package than the main one carries its package
    (compas_rhino/scene beside compas's own scene)."""
    comp = Counter(component(f) for f in files)
    (top, n_top), *rest = comp.most_common()
    label = _short(top, main_pkg)
    sub = Counter("/".join(Path(f).parent.parts[:4]) for f in files if f.startswith(top + "/"))
    if sub:
        s, n_s = sub.most_common(1)[0]
        if s != top and n_s >= 0.6 * n_top and len(Path(s).parts) > 3 and Path(s).name not in GENERIC:
            label += "/" + Path(s).name
            top = s
    if rest and rest[0][1] >= 0.25 * len(files):
        label += " + " + _short(rest[0][0], main_pkg)
    return label, top


GENERIC = {"src", "Src", "include", "packages", "hil", "lib", "tests", "Tests"}


def _short(path: str, main_pkg: str = "") -> str:
    """A folder's short name: the program for an add-in (clients/<program>/...), else its last distinctive part,
    after its package when that is not the main one."""
    raw = path.split("/")
    if len(raw) > 1 and raw[0] in ADDIN_ROOTS:
        return raw[1]
    parts = [p for p in raw if p not in GENERIC]
    name = parts[-1] if parts else path
    pkg = _package(path)
    return f"{pkg}/{name}" if main_pkg and pkg not in (main_pkg, name) else name


def _package(path: str) -> str:
    """The package a folder belongs to: its first distinctive part below src/packages (compas_rhino, hil_fed)."""
    raw = path.split("/")
    if len(raw) > 1 and raw[0] in ADDIN_ROOTS:
        return raw[1]
    parts = [p for p in raw if p not in GENERIC]
    if "src" in raw or "packages" in raw:
        after = raw[max(i for i, p in enumerate(raw) if p in ("src", "packages")) + 1 :]
        parts = [p for p in after if p not in GENERIC] or parts
    return parts[0] if parts else path


def _words(path: str) -> set[str]:
    stem = Path(path).stem.replace(".g", "")
    return {w for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", stem) if len(w) > 2} - {"Hil", "hil", "Hub", "hub"}


def make_rooms(recs, tiers: dict[str, str], deps, co, target: int) -> tuple[dict, dict]:
    """Rooms per tier: the tier's files clustered on their own, so a room never spans two tiers. Each tier aims at
    its share of ``target`` rooms by its share of the code."""
    total = sum(r.loc for r in recs.values()) or 1
    groups: list[tuple[str, set[str]]] = []
    for tid, *_ in TIERS:
        fs = sorted(p for p in recs if tiers[p] == tid)
        if not fs:
            continue
        share = sum(recs[p].loc for p in fs) / total
        found = derive_rooms(graph(fs, deps, co), max(1, round(target * share)))
        # a room of one or two files joins a room of its own component on this tier, where there is one
        main = lambda files_: Counter(component(f) for f in files_).most_common(1)[0][0]  # noqa: E731
        big = [r for r in found if len(r) >= 3]
        for small in [r for r in found if len(r) < 3]:
            home = next((r for r in sorted(big, key=len, reverse=True) if main(r) == main(small)), None)
            if home is not None:
                home |= small
            else:
                big.append(small)  # the next small room of this component joins it

        groups += [(tid, files_) for files_ in big]
    main_pkg = main_package(f for _, files_ in groups for f in files_)
    named = [(tid, files_) + room_name(files_, main_pkg) for tid, files_ in groups]
    count = Counter((tid, label) for tid, _, label, _ in named)
    df = Counter(w for _, files_ in groups for w in {w for f in files_ for w in _words(f)})
    rooms, member, used = {}, {}, Counter()
    for tid, files_, label, path in named:
        if count[(tid, label)] > 1:  # the word that sets this room's file names apart from the others'
            tf = Counter(w for f in files_ for w in _words(f))
            word = max(sorted(tf), key=lambda w: (tf[w] / df[w], tf[w]), default="")
            label = f"{label} · {word}" if word else label
        used[(tid, label)] += 1
        n = used[(tid, label)]
        rid = tid + "-" + re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") + (f"-{n}" if n > 1 else "")
        rooms[rid] = {"name": label + (f" {n}" if n > 1 else ""), "path": path, "tier": tid, "files": files_}
        rooms[rid]["area"] = max(12, len(files_) * 4.5)
        for f in files_:
            member[f] = rid
    return rooms, member


def main_package(paths) -> str:
    """The package most of the code is in (compas in compas_2x): its folders are named plainly, others' carry
    their package."""
    return Counter(_package(component(f)) for f in paths).most_common(1)[0][0]


FOLDER_MAX = 14  # a folder with more files than this, and sub-folders, is split into them


def folder_rooms(recs, tiers: dict[str, str], main_pkg: str) -> tuple[dict, dict]:
    """Rooms as the folders have it: per tier, each component's folder, split into its sub-folders while it holds
    more than FOLDER_MAX files; a sub-folder of one or two files stays with its parent. The other view of the same
    code, to hold the derived rooms against."""
    rooms, member = {}, {}

    def label(folder: str, comp: str) -> str:
        base = _short(comp, main_pkg)
        skip = GENERIC | {_package(comp), Path(comp).name}
        rest = [p for p in Path(folder).parts[len(Path(comp).parts) :] if p not in skip]
        return "/".join([base, *rest[-2:]])

    def add(tid: str, folder: str, comp: str, files: list[str]) -> None:
        rid = "f-" + tid + "-" + (re.sub(r"[^a-z0-9]+", "-", folder.lower()).strip("-") or "root")
        rooms[rid] = {
            "name": label(folder, comp),
            "path": folder,
            "tier": tid,
            "comp": comp,
            "files": set(files),
            "area": max(12, len(files) * 4.5),
        }
        for f in files:
            member[f] = rid

    def split(tid: str, folder: str, comp: str, files: list[str]) -> None:
        while True:
            depth = len(Path(folder).parts) if folder != "." else 0
            direct, subs = [], defaultdict(list)
            for f in files:
                parts = Path(f).parent.parts
                (subs["/".join(parts[: depth + 1])] if len(parts) > depth else direct).append(f)
            if len(subs) == 1 and not direct:  # a folder holding one folder only: go down to it
                folder = next(iter(subs))
                continue
            break
        if len(files) <= FOLDER_MAX or not subs:
            return add(tid, folder, comp, files)
        rest = list(direct)
        for sub, fs in sorted(subs.items()):
            if len(fs) >= 3:
                split(tid, sub, comp, fs)
            else:
                rest += fs
        if rest:
            add(tid, folder, comp, rest)

    by = defaultdict(list)
    for p in sorted(recs):
        by[(tiers[p], component(p))].append(p)
    for (tid, comp), fs in sorted(by.items()):
        split(tid, comp, comp, fs)
    same = Counter((r["tier"], r["name"]) for r in rooms.values())
    for r in rooms.values():  # two folders of one name on a tier (src/ and include/): the full last part
        if same[(r["tier"], r["name"])] > 1 and Path(r["path"]).name not in r["name"].split("/"):
            r["name"] += "/" + Path(r["path"]).name
    return rooms, member


def component_places(rooms: dict, deps, anchors: dict[str, list[float]]) -> tuple[dict, float]:
    """The components (folders) laid out once, for all tiers and both views, by how they depend on each other;
    anchored ones pulled toward their hint. Returns their places and the scale of a platform."""
    comps = sorted({c for r in rooms.values() for c in map(component, r["files"])})
    scale = 1.3 * math.sqrt(sum(r["area"] for r in rooms.values()) / max(1, len({r["tier"] for r in rooms.values()})))
    cg = nx.Graph()
    cg.add_nodes_from(comps)
    for (a, b), w in deps.items():
        ca, cb = component(a), component(b)
        if ca != cb and ca in cg and cb in cg:
            cg.add_edge(ca, cb, weight=cg.get_edge_data(ca, cb, {}).get("weight", 0) + w)
    hints = {}
    for c in comps:
        for prefix, (hx, hy) in anchors.items():
            if c == prefix or c.startswith(prefix.rstrip("/") + "/"):
                hints[c] = (hx * scale, hy * scale)
                break
    for c, (hx, hy) in hints.items():  # an anchor is a node of its own, fixed; the component is tied to it
        cg.add_edge(c, "@" + c, weight=8.0)
    pos0 = {"@" + c: h for c, h in hints.items()}
    cpos = nx.spring_layout(
        cg,
        weight="weight",
        seed=3,
        k=scale / max(1.0, math.sqrt(len(comps))) * 1.4,
        iterations=300,
        pos=pos0 or None,
        fixed=list(pos0) or None,
        scale=None if pos0 else scale,
    )
    lim = 1.2 * scale  # a component with no ties drifts off with the repulsion: keep it on the platform
    return {c: (max(-lim, min(lim, x)), max(-lim, min(lim, y))) for c, (x, y) in cpos.items() if c in comps}, scale


def place(rooms: dict, links: dict[tuple[str, str], float], cpos: dict, scale: float) -> None:
    """Room footprints on their tier's platform, in grid units: each tier's rooms laid out by their coupling, each
    pulled toward its component's place (so a component sits at the same spot on every platform, in both views),
    and pushed apart so none overlap and a corridor fits between any two."""
    comp_of = {
        rid: r.get("comp") or Counter(component(f) for f in r["files"]).most_common(1)[0][0] for rid, r in rooms.items()
    }
    for tid in sorted({r["tier"] for r in rooms.values()}):
        ids = sorted(rid for rid, r in rooms.items() if r["tier"] == tid)
        g = nx.Graph()
        g.add_nodes_from(ids)
        for (a, b), w in links.items():
            if a in g and b in g:
                g.add_edge(a, b, weight=g.get_edge_data(a, b, {}).get("weight", 0) + w)
        fixed = {}
        for rid in ids:
            c = comp_of[rid]
            fixed["@" + c] = tuple(cpos[c])
            g.add_edge(rid, "@" + c, weight=6.0)
        pos = nx.spring_layout(
            g,
            weight="weight",
            seed=5,
            k=max(3.0, scale / 4),
            iterations=200,
            pos={**fixed, **{rid: tuple(cpos[comp_of[rid]]) for rid in ids}},
            fixed=list(fixed),
        )
        placed: list[tuple[int, int, int, int]] = []
        for rid in sorted(ids, key=lambda r: -rooms[r]["area"]):
            w, h = _footprint(rooms[rid]["area"])
            x, y = _free_spot(*pos[rid], w, h, placed, 3)
            rooms[rid].update(x=x, y=y, w=w, h=h)


def _footprint(area: float) -> tuple[int, int]:
    side = max(3, round(math.sqrt(area)))
    return side + (1 if side > 4 else 0), side


def _free_spot(cx: float, cy: float, w: int, h: int, placed: list, gap: int) -> tuple[int, int]:
    """The spot nearest (cx, cy), spiralling out, where a w × h box keeps ``gap`` from every placed one; added."""
    x, y = round(cx - w / 2), round(cy - h / 2)
    step, ang = 0.0, 0.0
    while any(
        x < px + pw + gap and px < x + w + gap and y < py + ph + gap and py < y + h + gap for px, py, pw, ph in placed
    ):
        ang += 0.5
        step += 0.15
        x, y = round(cx - w / 2 + step * math.cos(ang)), round(cy - h / 2 + step * math.sin(ang))
    placed.append((x, y, w, h))
    return x, y


def place_folders(rooms: dict, cpos: dict) -> None:
    """By folders: a component's rooms packed together in a wing (sub-folders next to each other, in path order),
    the wing at its component's place, wings kept apart."""
    for tid in sorted({r["tier"] for r in rooms.values()}):
        by: dict[str, list[str]] = defaultdict(list)
        for rid, r in rooms.items():
            if r["tier"] == tid:
                by[r["comp"]].append(rid)
        wings = []
        for comp, ids in by.items():
            ids.sort(key=lambda r: rooms[r]["path"])
            sizes = {rid: _footprint(rooms[rid]["area"]) for rid in ids}
            G = 3
            span = sum((w + G) * (h + G) for w, h in sizes.values())
            row_w = max(max(w for w, _ in sizes.values()), math.sqrt(span) * 1.25)
            x = y = row_h = bw = 0
            local = {}
            for rid in ids:
                w, h = sizes[rid]
                if x > 0 and x + w > row_w:
                    x, y, row_h = 0, y + row_h + G, 0
                local[rid] = (x, y)
                x += w + G
                row_h = max(row_h, h)
                bw = max(bw, x - G)
            wings.append((comp, ids, sizes, local, bw, y + row_h))
        placed: list[tuple[int, int, int, int]] = []
        for comp, ids, sizes, local, bw, bh in sorted(wings, key=lambda w_: -w_[4] * w_[5]):
            ox, oy = _free_spot(*cpos[comp], bw, bh, placed, 5)
            for rid in ids:
                rooms[rid].update(x=ox + local[rid][0], y=oy + local[rid][1], w=sizes[rid][0], h=sizes[rid][1])


def normalise(*views: dict) -> None:
    """Shift all views alike, so the platforms start at 3 and both views share their coordinates."""
    every = [r for rooms in views for r in rooms.values()]
    mx, my = min(r["x"] for r in every), min(r["y"] for r in every)
    for r in every:
        r["x"] -= mx - 3
        r["y"] -= my - 3


def room_links(member: dict, deps: dict, co: dict) -> dict:
    links: dict[tuple[str, str], dict] = {}
    for (a, b), w in deps.items():
        ra, rb = member.get(a), member.get(b)
        if ra and rb and ra != rb:
            k = (ra, rb)
            links.setdefault(k, {"weight": 0.0, "refs": 0.0, "cochange": 0.0})
            links[k]["refs"] += w
            links[k]["weight"] += w
    for (a, b), w in co.items():
        ra, rb = member.get(a), member.get(b)
        if ra and rb and ra != rb:
            k = (min(ra, rb), max(ra, rb))
            links.setdefault(k, {"weight": 0.0, "refs": 0.0, "cochange": 0.0})
            links[k]["cochange"] += w
            links[k]["weight"] += 0.6 * w
    return links


# ----------------------------------------------------------------------------------------------- metrics


def tiers_of(recs, overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Each file's tier: detected from its code, unless ``[tiers]`` in the config names it (a glob per tier, the
    first that matches wins)."""
    known = {tid for tid, *_ in TIERS}
    for glob, tid in (overrides or {}).items():
        if tid not in known:
            raise SystemExit(f"codemap: [tiers] {glob!r} = {tid!r}: not a tier (one of {', '.join(sorted(known))})")
    out = {}
    for p, r in recs.items():
        hit = next((tid for glob, tid in (overrides or {}).items() if fnmatch.fnmatch(p, glob)), None)
        out[p] = hit or tier_of(r, layer_of(r, r.code)[0])
    return out


def metrics(root, cfg, recs, texts, member, rooms, tiers, deps, co, recent, last, share, clone_pairs, clone_where):
    files_out = {}
    for path, r in recs.items():
        layer, signs = layer_of(r, r.code)
        cx = [f.complexity for f in r.functions] or [0]
        files_out[path] = {
            "room": member.get(path),
            "lang": r.lang,
            "loc": r.loc,
            "functions": len(r.functions),
            "classes": len(r.classes),
            "cx_max": max(cx),
            "cx_sum": sum(cx),
            "long": [f"{f.name}:{f.line} ({f.lines} lines)" for f in r.functions if f.lines > 80][:8],
            "many_params": [f"{f.name}:{f.line} ({f.params})" for f in r.functions if f.params > 6][:8],
            "complex": sorted(
                ([f.name, f.line, f.complexity] for f in r.functions if f.complexity >= 15), key=lambda x: -x[2]
            )[:8],
            "magic": r.magic[:60],
            "magic_n": len(r.magic),
            "swallowed": r.swallowed,
            "dup": round(share.get(path, 0.0), 3),
            "clones_with": {
                g: sorted(set(lines))[:6]
                for g, lines in sorted(clone_where.get(path, {}).items(), key=lambda kv: -len(kv[1]))[:6]
            },
            "layer": layer,
            "tier": tiers[path],
            "signs": signs,
            "violations": violations(layer, signs),
            "recent": recent.get(path, 0),
            "last": last.get(path),
            "generated": any(fnmatch.fnmatch(path, p) for p in cfg["generated"]),
        }

    room_out = {}
    for rid, r in rooms.items():
        fs = [files_out[p] | {"path": p} for p in files_out if files_out[p]["room"] == rid]
        code = [f for f in fs if not f["generated"]]
        loc = sum(f["loc"] for f in code)
        toks = sum(len(recs[f["path"]].tokens) for f in code) or 1
        dup = sum(f["dup"] * len(recs[f["path"]].tokens) for f in code) / toks
        funcs = sum(f["functions"] for f in code)
        cxs = [c.complexity for f in code for c in recs[f["path"]].functions] or [0]
        layers = Counter(f["layer"] for f in code)
        churn = sum(f["recent"] for f in code)
        room_out[rid] = {
            "name": r["name"],
            "path": r.get("path", ""),
            "tier": r["tier"],
            "x": r["x"],
            "y": r["y"],
            "w": r["w"],
            "h": r["h"],
            "files": sorted(f["path"] for f in fs),
            "loc": loc,
            "functions": funcs,
            "classes": sum(f["classes"] for f in code),
            "cx_avg": round(sum(cxs) / len(cxs), 1),
            "cx_max": max(cxs),
            "complex_n": sum(1 for c in cxs if c >= 15),
            "magic_n": sum(f["magic_n"] for f in code),
            "magic_kloc": round(1000 * sum(f["magic_n"] for f in code) / max(loc, 1), 1),
            "swallowed_n": sum(len(f["swallowed"]) for f in code),
            "dup": round(dup, 3),
            "layers": dict(layers),
            "violations_n": sum(len(f["violations"]) for f in code),
            "churn": churn,
            "hotspot": round(churn * sum(cxs) / 100, 1),
            "langs": dict(Counter(f["lang"] for f in code)),
        }
    links = room_links(member, deps, co)
    # coupling per room (afferent: other rooms depending on it; efferent: rooms it depends on), cycles
    dg = nx.DiGraph()
    dg.add_nodes_from(room_out)
    for (a, b), link in links.items():
        if link["refs"] > 0:
            dg.add_edge(a, b, weight=link["refs"])
    cyc = [c for c in nx.strongly_connected_components(dg) if len(c) > 1]
    in_cycle = {r: i for i, c in enumerate(cyc) for r in c}
    for rid, ro in room_out.items():
        ca = dg.in_degree(rid)
        ce = dg.out_degree(rid)
        mutual = sum(1 for o in dg.successors(rid) if dg.has_edge(o, rid))
        ro.update(
            ca=ca,
            ce=ce,
            instability=round(ce / (ca + ce), 2) if ca + ce else None,
            cycle=in_cycle.get(rid),
            mutual=mutual,
        )
        abstract = 0
        total = 0
        for p in ro["files"]:
            for k in recs[p].classes:
                total += 1
                if (
                    re.search(r"(^I[A-Z]|Base$|Abstract|Protocol|Interface)", k.name)
                    or "ABC" in " ".join(k.bases)
                    or "Protocol" in " ".join(k.bases)
                ):
                    abstract += 1
        ro["abstractness"] = round(abstract / total, 2) if total else 0.0
        ro["distance"] = (
            None if ro["instability"] is None else round(abs(ro["abstractness"] + ro["instability"] - 1), 2)
        )
    clone_links: dict[tuple[str, str], int] = defaultdict(int)
    for (a, b), n in clone_pairs.items():
        ra, rb = files_out[a]["room"], files_out[b]["room"]
        if ra and rb and ra != rb:
            clone_links[(min(ra, rb), max(ra, rb))] += n
    return files_out, room_out, links, clone_links, cyc


def class_model(recs, files_out, exact: dict | None = None) -> dict:
    """Per room: its classes (and module-level function counts), inheritance and use edges between them. For a file
    an index covers (``exact``, from precise.classes) the bases and uses are resolved symbols; elsewhere names."""
    out: dict[str, dict] = defaultdict(lambda: {"classes": [], "edges": []})
    owner: dict[str, tuple[str, str]] = {}
    for p, r in recs.items():
        for k in r.classes:
            owner.setdefault(k.name, (p, files_out[p]["room"]))
    exact = exact or {}
    for p, r in recs.items():
        room = files_out[p]["room"]
        if room is None:
            continue
        if p in exact:
            for k in r.classes:
                out[room]["classes"].append(
                    {
                        "name": k.name,
                        "file": p,
                        "line": k.line,
                        "lines": k.lines,
                        "bases": k.bases,
                        "methods": k.methods,
                        "fields": k.fields,
                        "exact": True,
                    }
                )
                rel = exact[p].get(k.name, {"bases": [], "uses": set()})
                for name, f in rel["bases"]:
                    out[room]["edges"].append({"from": k.name, "to": name, "kind": "inherits"})
                    if files_out[f]["room"] != room:
                        out[room].setdefault("ext", {})[name] = files_out[f]["room"]
                for name, f in sorted(rel["uses"]):
                    if files_out[f]["room"] == room:
                        out[room]["edges"].append({"from": k.name, "to": name, "kind": "uses"})
            continue
        for k in r.classes:
            out[room]["classes"].append(
                {
                    "name": k.name,
                    "file": p,
                    "line": k.line,
                    "lines": k.lines,
                    "bases": k.bases,
                    "methods": k.methods,
                    "fields": k.fields,
                }
            )
            for b in k.bases:
                b = b.split(".")[-1].split("::")[-1].split("<")[0]
                if b in owner and b != k.name:
                    out[room]["edges"].append({"from": k.name, "to": b, "kind": "inherits"})
                    if owner[b][1] != room:  # a base from another room: drawn as a stub that names that room
                        out[room].setdefault("ext", {})[b] = owner[b][1]
        names = {k.name for k in r.classes}
        for u in r.uses:
            if u in owner and owner[u][1] == room and u not in names:
                for k in r.classes[:1]:
                    out[room]["edges"].append({"from": k.name, "to": u, "kind": "uses"})
    for v in out.values():
        seen = set()
        v["edges"] = [e for e in v["edges"] if not ((e["from"], e["to"]) in seen or seen.add((e["from"], e["to"])))]
    return dict(out)


def _nmi(pairs: list[tuple[str, str, float]]) -> float:
    """Normalised mutual information of two groupings of the same items (weighted): 1 = the same grouping."""
    tot = sum(w for *_, w in pairs) or 1.0
    pa, pb, pab = Counter(), Counter(), Counter()
    for a, b, w in pairs:
        pa[a] += w / tot
        pb[b] += w / tot
        pab[(a, b)] += w / tot
    mi = sum(p * math.log(p / (pa[a] * pb[b])) for (a, b), p in pab.items() if p > 0)
    ha = -sum(p * math.log(p) for p in pa.values() if p > 0)
    hb = -sum(p * math.log(p) for p in pb.values() if p > 0)
    return 2 * mi / (ha + hb) if ha + hb > 0 else 1.0


def fit(files_out: dict, deps: dict, rooms: dict, folders: dict) -> dict:
    """How well the folders match what the code does: the rooms a folder is split into, the folders a room gathers,
    files whose dependencies live in another folder, and overall numbers."""
    code = {p: f for p, f in files_out.items() if f["room"] and f["folder"] and not f["generated"]}
    overall = {"agreement": round(_nmi([(f["room"], f["folder"], max(1, f["loc"])) for f in code.values()]), 2)}
    tot = in_room = in_folder = 0.0
    work: dict[str, Counter] = defaultdict(Counter)  # per file: dependency weight toward each folder room
    for (a, b), w in deps.items():
        if a in code and b in code and a != b:
            tot += w
            in_room += w if code[a]["room"] == code[b]["room"] else 0
            in_folder += w if code[a]["folder"] == code[b]["folder"] else 0
            work[a][folders[code[b]["folder"]]["path"]] += w
            work[b][folders[code[a]["folder"]]["path"]] += w
    overall["deps_in_room"] = round(in_room / tot, 2) if tot else None
    overall["deps_in_folder"] = round(in_folder / tot, 2) if tot else None

    def parts(key: str, other: str) -> dict[str, list]:
        by: dict[str, Counter] = defaultdict(Counter)
        for f in code.values():
            by[f[key]][f[other]] += max(1, f["loc"])
        out = {}
        for rid, c in by.items():
            total = sum(c.values()) or 1
            out[rid] = [[o, round(n / total, 2)] for o, n in c.most_common()]
        return out

    splits, gathers = parts("folder", "room"), parts("room", "folder")
    for rid, ps in splits.items():
        folders[rid]["fit"] = ps[0][1]
    for rid, ps in gathers.items():
        rooms[rid]["fit"] = ps[0][1]

    def worst(ps: dict, view: dict) -> list:
        many = [(rid, [p for p in v if p[1] >= 0.15]) for rid, v in ps.items()]
        many = [(rid, v) for rid, v in many if len(v) >= 2]
        return [
            {"id": rid, "parts": v} for rid, v in sorted(many, key=lambda kv: (-len(kv[1]), -view[kv[0]]["loc"]))[:12]
        ]

    def kin(a: str, b: str) -> bool:  # the same folder, or one inside the other (src/ and include/ as one)
        a, b = ("/".join(p for p in x.split("/") if p not in GENERIC) for x in (a, b))
        return a == b or a.startswith(b + "/") or b.startswith(a + "/") or "." in (a, b) or "" in (a, b)

    by_path = {(r["tier"], r["path"]): rid for rid, r in folders.items()}
    apart = []
    for p, c in work.items():
        mine = folders[code[p]["folder"]]["path"]
        own = sum(v for k, v in c.items() if kin(k, mine))
        other, w = max(((k, v) for k, v in c.items() if not kin(k, mine)), key=lambda kv: kv[1], default=(None, 0))
        if other and w >= 3 and w > 2 * own and w >= 0.6 * sum(c.values()):
            toward = by_path.get((code[p]["tier"], other)) or next(i for (_, q), i in by_path.items() if q == other)
            apart.append(
                {
                    "file": p,
                    "folder": code[p]["folder"],
                    "toward": toward,
                    "share": round(w / sum(c.values()), 2),
                    "w": w,
                }
            )
    apart.sort(key=lambda a: -a["w"])
    return {
        **overall,
        "splits": worst(splits, folders),
        "gathers": worst(gathers, rooms),
        "apart": apart[:30],
        "composition": {"room": gathers, "folder": splits},
    }


# ----------------------------------------------------------------------------------------------- main


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="codemap", description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default="codemap.toml")
    ap.add_argument("--json", help="also write the model as JSON here")
    ap.add_argument("--root", help="the repository to map (default: the one holding the config)")
    ap.add_argument("--out", help="folder for index.html (default: the config's folder)")
    ap.add_argument("--index", action="store_true", help="resolve references with the indexers in [index]")
    ap.add_argument("--setup", action="store_true", help="with --index: run [index] setup first (as in CI)")
    ap.add_argument("--scip", action="append", default=[], help="a SCIP index made beforehand: PATH or PATH=FOLDER")
    args = ap.parse_args(argv)
    cfg_path = Path(args.config).resolve()
    root = Path(git(Path(args.root).resolve() if args.root else cfg_path.parent, "rev-parse", "--show-toplevel"))
    cfg = load_config(cfg_path)
    out_dir = Path(args.out).resolve() if args.out else cfg_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    recs, texts = read_all(root, cfg)
    deps = dependencies(recs)
    ix, index_notes = precise.run(root, cfg, args.setup) if args.index else (None, [])
    if args.scip:
        ix = precise.read_files(args.scip)
    cov = precise.covered(recs, ix)
    exact = precise.classes(recs, ix) if ix else {}
    if ix:
        deps = precise.dependencies(recs, ix, deps)
    co, recent, last = cochange(root, set(recs))
    share, clone_pairs, clone_where = clones(
        {p: r for p, r in recs.items() if not any(fnmatch.fnmatch(p, g) for g in cfg["generated"])}
    )
    tiers = tiers_of(recs, cfg.get("tiers", {}))
    rooms, member = make_rooms(recs, tiers, deps, co, cfg["rooms"])
    frooms, fmember = folder_rooms(recs, tiers, main_package(recs))
    cpos, scale = component_places(rooms, deps, cfg.get("anchors", {}))
    place(rooms, {k: v["weight"] for k, v in room_links(member, deps, co).items()}, cpos, scale)
    place_folders(frooms, cpos)
    normalise(rooms, frooms)
    notes = [f"{len(cfg.get('anchors', {}))} anchoring hints", *index_notes]
    total = sum(r.loc for r in recs.values()) or 1
    resolved = round(sum(recs[p].loc for p in cov) / total, 3)
    args_m = (recent, last, share, clone_pairs, clone_where)
    files_out, room_out, links, clone_links, cycles = metrics(
        root, cfg, recs, texts, member, rooms, tiers, deps, co, *args_m
    )
    ffiles, froom_out, flinks, fclone_links, fcycles = metrics(
        root, cfg, recs, texts, fmember, frooms, tiers, deps, co, *args_m
    )
    for p, f in files_out.items():
        f["folder"] = fmember.get(p)
        f["comp"] = component(p)
        f["resolved"] = p in cov
    for ro in (*room_out.values(), *froom_out.values()):  # the share of a room's code whose references are resolved
        loc = sum(recs[p].loc for p in ro["files"]) or 1
        ro["resolved"] = round(sum(recs[p].loc for p in ro["files"] if p in cov) / loc, 2)
    for rid, r in froom_out.items():
        r["comp"] = frooms[rid]["comp"]
    fitted = fit(files_out, deps, room_out, froom_out)

    def view(rs, ls, cl, cy, fs):
        return {
            "rooms": rs,
            "links": [{"a": a, "b": b, **v} for (a, b), v in ls.items()],
            "clones": [{"a": a, "b": b, "n": n} for (a, b), n in cl.items()],
            "cycles": [sorted(c) for c in cy],
            "classes": class_model(recs, fs, exact),
        }

    model = {
        "title": cfg["title"],
        "commit": git(root, "rev-parse", "--short", "HEAD"),
        "branch": git(root, "rev-parse", "--abbrev-ref", "HEAD"),
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "notes": notes,
        "views": {
            "room": view(room_out, links, clone_links, cycles, files_out),
            "folder": view(froom_out, flinks, fclone_links, fcycles, ffiles),
        },
        "fit": fitted,
        "precision": {"resolved": resolved, "tools": sorted(set(ix.tools)) if ix else []},
        "files": files_out,
        "layers": LAYERS,
        "tiers": [{"id": tid, "label": label, "about": about} for tid, label, about, _ in TIERS],
        "routes": cfg.get("routes", {}),
    }
    if args.json:
        Path(args.json).write_text(json.dumps(model, indent=1, default=list), encoding="utf-8")
    html = (HERE / "template.html").read_text(encoding="utf-8")
    data = json.dumps(model, separators=(",", ":"), default=list).replace("</", "<\\/")
    vendor = "\n".join(
        (HERE / "vendor" / f).read_text(encoding="utf-8")
        for f in ("three.min.js", "OrbitControls.js", "elk.bundled.js")
    )
    html = html.replace("/*__VENDOR__*/", vendor)
    (out_dir / "index.html").write_text(html.replace("/*__DATA__*/null", data), encoding="utf-8")
    loc = sum(r.loc for r in recs.values())
    print(
        f"codemap: {len(recs)} files, {loc} lines, {len(room_out)} rooms, {len(froom_out)} folders, "
        f"agreement {fitted['agreement']}, {len(links)} links, {100 * resolved:.0f}% resolved "
        f"in {time.time() - t0:.1f} s -> {out_dir / 'index.html'}"
    )
    for n in notes:
        print("  " + n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
