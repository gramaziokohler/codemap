# Frontend work: where it stands

Last worked on: 2026-11-24. Branch `platform-layout`, off `main` at `fc7673e`. **Nothing is committed yet.**

The task: review and improve the frontend of codemap. The whole page is `template.html` (CSS, HTML and JS in one file,
no build step); `codemap.py` analyses a repository, works out the layout and fills the template with JSON.

## Picking it up again

```sh
uv run dev.py        # then http://localhost:8765/timber?form=buildings
```

- `template.html`: edits show up by themselves. CSS is swapped in place; anything else reloads and keeps the view.
- `codemap.py`: edits need the map made again:

  ```sh
  uv run codemap.py --config examples/codemap.toml --root ../compas_timber --out codemap/dev/timber --json codemap/dev/timber.json
  ```

- Sample maps in `codemap/dev/`: `timber` (the main test case), `compas`, `compas_fab`, `compas_model`, `self`.
- For comparison: `codemap/before/*.html` (the original pages, as on `main`; open as files) and `codemap/organic/*.json`
  (the layout before the street grid; data only). All of `codemap/` is ignored by git.

## What is done

| What | Where |
|---|---|
| Live-edit server, standard library only | `dev.py` (new) |
| Rooms spread over the platform; platform sizes in steps of 8, one per tier | `codemap.py`: `spread`, `_spread_on` |
| Street grid that keeps the clusters | `codemap.py`: `streets`, `_grid`, `_settle` |
| Tier names written flat on the slabs, one size everywhere | `template.html`: `letters`, `lettering`, `platform` |
| Form switch, Pits / Buildings (`?form=buildings`) | `template.html`: `building`, `marks`, `form-tabs` |
| Pipes in street lanes, for buildings | `template.html`: `pipeRoutes`, `pipe`, `pipes` |
| By folders: a component's plots tinted as a district | `template.html`: `wings` |
| Axonometric view fitted to the whole stack | `template.html`: `setView`, `bounds` |

The JSON has three new fields: `platforms` (x, y, side per tier), `streets` (the grid per view) and `plot` per room.
README and the page's help text have short notes on the new layout.

### How the layout works

1. The original layout: rooms that depend on each other attract.
2. `spread`: stretched over the platform, then each room drifts to the middle of the free floor around it.
3. `streets`: each room takes a plot near its spot and near the rooms it is linked to. A big room takes several plots.
   The coarsest grid is used that leaves about 30% of the plots open, so clusters keep apart.
4. Street lines are the same on every platform of a view. By dependencies and by folders have a grid each.

Settings, at the top of those functions: `STEP`, `SMALLEST`, `EDGE`, `TITLE`, `FILL`, `DRIFT`, `PLOTS`, `TOGETHER`.

### Decisions

- Tier names: not floating labels; flat, in the corner, the same size on all platforms, not too big. The size is the one
  at which "APPLICATION" fits the smallest platform (16). `SMALLEST` and `TITLE` are in `codemap.py` **and**
  `template.html` and must match.
- Grid: natural clustering, ordered by a grid. Not equal cells: one big room then sets the size of all (`compas` went
  from 88 to 200).
- Pits and buildings share one layout, so rooms stay put when the form is switched.
- Platform sizes are one per tier; filling every platform matters less than a folder standing at the same spot on each.

## Checked

In headless Chrome, on all five sample maps and both room groupings: no JavaScript errors, every same-platform pipe finds
a route, none goes through a building, none overlaps another. Views, cross-section, colours, hover, selection and the
room-switch animation work. Building the scene takes up to about 150 ms the first time. The same run twice gives the
same layout for `compas_timber`. There are no automated tests in the repository for any of this.

## Known problems

- **Pipes got longer and cross more on the grid** than before it. Domain, by dependencies, crossings: `compas` 143 to
  153, `compas_timber` 324 to 394, `compas_fab` 537 to 673, `compas_model` 59 to 52. `compas_timber` is still dense.
- Pits: the flat corridors still cross other rooms (the lanes are for buildings only).
- Pipes between tiers that are not neighbours drop straight down, through platforms or buildings in between.
- Platforms grew with the grid in `compas` (Domain 88 to 96, Integration 72 to 88, Application 48 to 64, Data 24 to 40)
  and `compas_fab` (Domain 64 to 72, Integration 40 to 56, Application 24 to 40).
- Application still covers part of Domain in `compas`.
- Short pipes hide between buildings in the axonometric view; Top shows them best.
- Roof hatching is on most buildings, so it says little; it needs a threshold.
- Room labels float over the lettering on the smallest platforms; only the top tier's rooms are named.
- The lettering reads upside down from behind, and is small on big maps.

### Not from this work, for whoever owns the analysis

- `compas` is not repeatable: rooms differ between hash seeds, on `main` too, though the README says otherwise.
- Serialization lands in Domain. The storage check does not know `open(...)`, `json_dump`, XML or protobuf, and Domain is
  where everything unrecognised goes. For one map, `[tiers]` in the config can fix it:
  `"src/compas_timber/proto/*.py" = "data"` and `"src/compas_timber/btlx/*.py" = "data"`.

## Next

1. **Pipe-strength slider**: the strongest pipes by default, all of a building's pipes on hover. In `compas_timber` 29 of
   62 pipes carry 80% of the coupling; in `compas` 16 of 41.
2. Room names written flat on the roofs.
3. A collapsible key: it covers the lowest platform.
4. Decide whether buildings become the default form (pits are, for now).
5. Commit. Leave `uv.lock` out: it is a stray file. Decide whether `dev.py` and this file go in.

From the first review, still open: the overlays cover the map (worst at small widths; unusable on a phone), selecting a
room barely shows in 3D, low text contrast (muted text 3.4:1, tier chips 2.4 to 3:1), the metrics table's beige zeros and
all-zero "Resolved" column, no keyboard access to the table, the class diagram is not a real dialog, "1 rooms", and fonts
named but not bundled.
