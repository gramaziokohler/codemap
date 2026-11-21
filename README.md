# codemap

A spatial map of a code base, to see what the code is and where to review first. One static HTML page, nothing to
serve.

- **Platforms** are the classic tiers (presentation, application, domain, data, integration), stacked. Each file is
  sorted into one by what its code uses (UI, HTTP or a command line, nothing but logic, storage, another program's
  API); where the code shows nothing, its path decides; `[tiers]` in the config overrides both.
- **Rooms**, two ways. *By dependencies*: files of a tier that depend on each other and change together (Louvain).
  *By folders*: the folders the code is filed in, a big folder split into its sub-folders. Switching moves every file
  from one to the other; the panel says how far the two agree and where they do not.
- **Pits** below each room, as deep as its code, one band per file; corridors and tubes for coupling; pins, hatching
  and discs for magic values, duplication and swallowed errors; hotspots, cycles, clones.
- **Views**: top, axonometric, perspective, front; a cross-section slider; a class diagram per room (ELK).
- **Metrics** per room and the coupling matrix below the map.

Languages: Python, C#, C++, TypeScript, JavaScript (tree-sitter). Everything is derived anew on each run, with fixed
seeds; nothing is pinned.

## As a GitHub Action

```yaml
jobs:
  codemap:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0          # the history: churn, co-change, hotspots
      - uses: gramaziokohler/codemap@main
        with:
          config: .github/codemap.toml
          out: codemap
      - uses: actions/upload-artifact@v4
        with:
          name: codemap
          path: codemap/
```

Inputs: `config` (default `codemap.toml`), `root` (default `.`), `out` (default `codemap`), `json` (optional, the
model as JSON). Output: `page`, the path of `index.html`.

## Locally

```sh
uv run codemap.py --config path/to/codemap.toml --root path/to/repo --out /tmp/map
open /tmp/map/index.html
```

`uv` reads the dependencies from the script's header (tree-sitter-language-pack, networkx, numpy).

## The config

See [examples/codemap.toml](examples/codemap.toml): `title`, `rooms` (how many to aim for), `include` and `exclude`
(globs), `generated` (counted, not judged), `[anchors]` (pull a component toward a side) and `[tiers]` (a file's
tier by hand).

## Vendored

three.js r146 with OrbitControls (MIT) and elkjs 0.9.3 (EPL-2.0), embedded in every page; their licences are in
`vendor/`.
