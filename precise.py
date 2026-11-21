"""Precise references from compiler-grade indexers, where the config asks for them ([index]): scip-python (Pyright)
and scip-typescript (the TypeScript compiler) write SCIP indexes; here they are run, read, and turned into what the
map needs: which file depends on which (resolved references, not names), and which class derives from or uses
which. Files no index covers keep the name-based heuristic; the map says how much of the code is resolved.

    [index]
    python = ["."]            # folders to index with scip-python (a .venv in the root or the folder is used)
    typescript = ["web"]      # folders with a tsconfig.json
    csharp = ["src/App/App.csproj"]   # projects or solutions, each restored and indexed on its own (scip-dotnet)
    setup = ["uv sync --all-packages", "npm ci --prefix web"]   # run first with --setup (as in CI)
"""

from __future__ import annotations

import math
import os
import shlex
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

import scip

INDEXERS = {  # pinned: the same map from the same code
    "python": "@sourcegraph/scip-python@0.6.8",
    "typescript": "@sourcegraph/scip-typescript@0.6.1",
    "csharp": "scip-dotnet@0.2.16",  # a .NET tool (Roslyn); needs the .NET SDK
}


def _log(msg: str) -> None:
    print(f"codemap: {msg}", file=sys.stderr, flush=True)


def _env(root: Path, folder: Path) -> dict:
    """The environment an indexer runs in: the project's virtual environment first on PATH, so Pyright resolves
    the packages the project uses."""
    env = dict(os.environ)
    for venv in (folder / ".venv", root / ".venv"):
        if (venv / "bin").is_dir() or (venv / "Scripts").is_dir():
            env["VIRTUAL_ENV"] = str(venv)
            env["PATH"] = os.pathsep.join([str(venv / "bin"), str(venv / "Scripts"), env.get("PATH", "")])
            break
    return env


def run(root: Path, cfg: dict, setup: bool) -> tuple[scip.Index | None, list[str]]:
    """Run the configured indexers; returns the merged index (None when nothing was indexed) and notes."""
    conf = cfg.get("index") or {}
    jobs = [(lang, d) for lang in INDEXERS for d in conf.get(lang, [])]
    if not jobs:
        return None, []
    notes: list[str] = []
    if setup:
        for cmd in conf.get("setup", []):
            _log(f"setup: {cmd}")
            r = subprocess.run(cmd, shell=True, cwd=root)
            if r.returncode:
                notes.append(f"setup failed: {cmd}")
    ix = scip.Index()
    with tempfile.TemporaryDirectory(prefix="codemap-scip-") as tmp:
        for n, (lang, d) in enumerate(jobs):
            folder = (root / d).resolve()
            out = Path(tmp) / f"{lang}-{n}.scip"
            if lang == "csharp":
                ok = _csharp(root, folder, out, Path(tmp), notes, d)
                if ok:
                    scip.read(out.read_bytes(), ".", ix, scope=f"{lang}-{n}")
                continue
            inferred = None
            if lang == "python":
                name = folder.name if folder != root else root.name
                cmd = ["npx", "-y", INDEXERS[lang], "index", "--project-name", name, "--output", str(out)]
            else:  # plain JavaScript without a tsconfig.json: the indexer infers one
                cmd = ["npx", "-y", INDEXERS[lang], "index", "--output", str(out)]
                if not (folder / "tsconfig.json").exists():
                    cmd.append("--infer-tsconfig")
                    inferred = folder / "tsconfig.json"  # written by the indexer: removed again below
            _log(f"indexing {d} ({lang}): {shlex.join(cmd)}")
            t0 = time.time()
            try:
                r = subprocess.run(
                    cmd, cwd=folder, env=_env(root, folder), capture_output=True, text=True, timeout=3600
                )
            except (OSError, subprocess.TimeoutExpired) as e:
                notes.append(f"{lang} index of {d} failed ({e.__class__.__name__}): names used there")
                continue
            finally:
                if inferred is not None and inferred.exists():
                    inferred.unlink()
            if r.returncode or not out.exists():
                tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
                _log(f"{lang} indexer failed in {d}: " + " | ".join(tail))
                notes.append(f"{lang} index of {d} failed: names used there")
                continue
            scip.read(out.read_bytes(), os.path.relpath(folder, root), ix)
            _log(f"indexed {d} ({lang}) in {time.time() - t0:.0f} s")
    return (ix if ix.files else None), notes


def _csharp(root: Path, project: Path, out: Path, tmp: Path, notes: list[str], d: str) -> bool:
    """Restore one .NET project (or solution) and index it with scip-dotnet; Windows-only target frameworks are
    allowed (EnableWindowsTargeting), so a Revit add-in restores on Linux."""
    name, version = INDEXERS["csharp"].split("@")
    tools = tmp / "dotnet-tools"
    env = dict(os.environ, EnableWindowsTargeting="true", DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1")
    exe = tools / name
    try:
        if not exe.exists():
            subprocess.run(
                ["dotnet", "tool", "install", name, "--version", version, "--tool-path", str(tools)],
                env=env,
                capture_output=True,
                text=True,
                check=True,
                timeout=900,
            )
        _log(f"indexing {d} (csharp): dotnet restore, {name} index")
        t0 = time.time()
        subprocess.run(
            ["dotnet", "restore", str(project)],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=1800,
        )
        r = subprocess.run(
            [
                str(exe),
                "index",
                str(project),
                "--skip-dotnet-restore",
                "--working-directory",
                str(root),
                "--output",
                str(out),
            ],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=3600,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        detail = getattr(e, "stderr", "") or getattr(e, "stdout", "") or str(e)
        _log(f"csharp index of {d} failed: " + " | ".join(str(detail).strip().splitlines()[-3:]))
        notes.append(f"csharp index of {d} failed: names used there")
        return False
    if r.returncode or not out.exists():
        _log(f"csharp indexer failed in {d}: " + " | ".join((r.stderr or r.stdout).strip().splitlines()[-3:]))
        notes.append(f"csharp index of {d} failed: names used there")
        return False
    _log(f"indexed {d} (csharp) in {time.time() - t0:.0f} s")
    return True


def read_files(specs: list[str]) -> scip.Index | None:
    """Indexes made beforehand: PATH or PATH=FOLDER (the folder it was made in, relative to the root)."""
    ix = scip.Index()
    for n, spec in enumerate(specs):
        path, _, prefix = spec.partition("=")
        scip.read(Path(path).read_bytes(), prefix or ".", ix, scope=f"file-{n}")
    return ix if ix.files else None


def covered(recs: dict, ix: scip.Index | None) -> set[str]:
    """The mapped files an index covers (and whose language it speaks)."""
    if ix is None:
        return set()
    return {p for p in recs if p in ix.files}


def dependencies(recs: dict, ix: scip.Index, heuristic: dict) -> dict[tuple[str, str], float]:
    """(a, b) -> how strongly a depends on b. From a file an index covers: its resolved references to symbols
    defined in other mapped files (1 + log2 of their count, so the scale matches the heuristic's); from the other
    files: the heuristic's edges, as they were."""
    cov = covered(recs, ix)
    n: Counter = Counter()
    for path, _, sym, _ in ix.refs:
        if path not in cov:
            continue
        d = ix.defs.get(sym)
        if d and d[0] != path and d[0] in recs:
            n[(path, d[0])] += 1
    edges = {k: w for k, w in heuristic.items() if k[0] not in cov}
    for k, c in n.items():
        edges[k] = 1 + math.log2(1 + c)
    return edges


def classes(recs: dict, ix: scip.Index) -> dict[str, dict]:
    """Per covered file, per class (by name): its bases and the classes it uses, as (name, file) of classes defined
    in mapped files; from the index's relationships and the references inside the class's lines."""
    cov = covered(recs, ix)
    types: dict[str, tuple[str, str]] = {}  # type symbol -> (name, file)
    for sym, (path, _) in ix.defs.items():
        if sym.endswith("#") and path in recs:
            types[sym] = (scip.type_name(sym), path)
    out: dict[str, dict] = defaultdict(dict)
    spans: dict[str, list] = defaultdict(list)
    for p in cov:
        for k in recs[p].classes:
            spans[p].append((k.line, k.line + max(1, k.lines) - 1, k.name))
            out[p][k.name] = {"bases": [], "uses": set()}
    for sym, (name, path) in types.items():
        if path in cov and name in out[path]:
            for b in sorted(ix.impl.get(sym, ())):
                if b in types and b != sym:
                    out[path][name]["bases"].append(types[b])
    by_file: dict[str, list] = defaultdict(list)
    for path, line, sym, _ in ix.refs:
        if path in cov:
            by_file[path].append((line, sym))
    for path, refs in by_file.items():
        for line, sym in refs:
            t = scip.owner_type(sym)
            if t not in types:
                continue
            inside = [s for s in spans[path] if s[0] <= line <= s[1]]
            if not inside:
                continue
            cname = min(inside, key=lambda s: s[1] - s[0])[2]  # the innermost class
            target = types[t]
            if target != (cname, path) and target not in out[path][cname]["bases"]:
                out[path][cname]["uses"].add(target)
    return out
