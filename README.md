# codemap

A spatial map of a code base, to see what the code is and where to review first. One static HTML page, nothing to
serve.

- **Platforms** are the classic tiers (presentation, application, domain, data, integration), stacked. Each file is
  sorted into one by what its code uses (UI, HTTP or a command line, nothing but logic, storage, another program's
  API); where the code shows nothing, its path decides; `[tiers]` in the config overrides both. A platform's size comes
  in steps and grows with the files on it.
- **Rooms**, two ways. *By dependencies*: files of a tier that depend on each other and change together (Louvain).
  *By folders*: the folders the code is filed in, a big folder split into its sub-folders. Switching moves every file
  from one to the other; the panel says how far the two agree and where they do not.
- **Pits** below each room, as deep as its code, one band per file; corridors and tubes for coupling; pins, hatching
  and discs for magic values, duplication and swallowed errors; hotspots, cycles, clones.
- **Views**: top, axonometric, perspective, front; a cross-section slider; a class diagram per room (ELK).
- **Metrics** per room and the coupling matrix below the map.

Languages: Python, C#, C++, TypeScript, JavaScript (tree-sitter). Everything is derived anew on each run, with fixed
seeds; nothing is pinned.

## What the analysis rests on

- **Syntax** (every language): [tree-sitter](https://tree-sitter.github.io) parses each file; size, functions,
  complexity (McCabe: decisions per function), parameters, magic values, swallowed errors and clones (a rolling hash
  over normalised runs of 60 tokens) are counted on the syntax trees.
- **References, resolved** where a compiler-grade indexer covers the language (`[index]` in the config): the
  [SCIP](https://github.com/sourcegraph/scip) indexers [scip-python](https://github.com/sourcegraph/scip-python)
  (Pyright), [scip-typescript](https://github.com/sourcegraph/scip-typescript) (the TypeScript compiler) and
  [scip-dotnet](https://github.com/sourcegraph/scip-dotnet) (Roslyn; each project restored, Windows-only target
  frameworks allowed, so a Revit add-in indexes on Linux) record every definition and every reference with the
  symbol it means. Which file depends on which, which class derives
  from or uses which, come from those; nothing is guessed by name.
- **References, matched by name** elsewhere: imports resolved to files, and names one file defines that another uses
  (only names defined in at most two files). Good hints, not proof: a common name can link the wrong file.
- **History**: `git log` for recent changes, what changes together (co-change) and hotspots.
- **Rooms and layout**: Louvain communities and spring layouts (networkx), with fixed seeds and a fixed order.

The page says how much of the code is resolved (overall, per room, a *Resolved* column), so it is always clear which
numbers rest on the compiler and which on names. C++ (scip-clang) is next.

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
model as JSON), `index` (default `true`: run the indexers in `[index]`), `setup` (default `true`: run the `[index]`
setup commands first). Output: `page`, the path of `index.html`.

## Locally

```sh
uv run codemap.py --config path/to/codemap.toml --root path/to/repo --out /tmp/map
open /tmp/map/index.html
```

`uv` reads the dependencies from the script's header (tree-sitter-language-pack, networkx, numpy). With `--index` the
indexers in `[index]` run through `npx` (Node 18 or newer) and, for C#, `dotnet` (the .NET 8 SDK), on the
environments already there; `--setup` runs the
`[index]` setup commands first, as the action does. `--scip PATH[=FOLDER]` reads an index made beforehand.

## The config

See [examples/codemap.toml](examples/codemap.toml): `title`, `rooms` (how many to aim for), `include` and `exclude`
(globs), `generated` (counted, not judged), `[anchors]` (pull a component toward a side), `[tiers]` (a file's
tier by hand) and `[index]` (which folders to index, and how to set them up).

## Vendored

three.js r146 with OrbitControls (MIT) and elkjs 0.9.3 (EPL-2.0), embedded in every page; their licences are in
`vendor/`.
