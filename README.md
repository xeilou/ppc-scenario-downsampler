# PPC DRT Downsampler

Builds a downsampled PPC DRT scenario — population, DRT fleets, transit
vehicle capacity, network/schedule, DRT stops, service areas, and a
generated `drt-run-config.xml` — as one self-contained, plug-and-play
folder. No JVM/Maven build needed to run this tool; it's plain Python 3
(standard library only), and it has no dependency on git or on any
`matsim-project-ppc` checkout being present.

## Folder layout

```
ppc-drt-downsampler\
  downsample_pipeline.py   <- the tool
  run.bat                  <- double-click this to run it
  README.md
  input\                   <- the full-size (100%) source files, already here
    plans.xml.gz
    ppc-vehicles.xml.gz
    ppc-network-transit-mapped.xml.gz
    ppc-schedule-mapped.xml.gz
    drt-run-config-template.xml   (the baseline config this tool edits)
    drt_full\                     (20 files, full-size fleet per zone)
    drt-stops\                    (20 files, terminal stop geometry per zone)
    service-areas\                (zone boundary shapefiles)
  output\                  <- each run creates output\<label>\ here
```

The `input\` folder already has everything needed — the script's SETTINGS
already point at it. Most people won't need to touch those paths at all.

## How to use it (no command line needed)

1. Open `downsample_pipeline.py` in any text editor (Notepad works).
2. At the top of the file there's a block labeled **SETTINGS**. The file
   paths already point at the `input\` folder next to this script, so the
   main thing to change is `FRACTION` (how much to keep — `0.01` = 1%,
   `0.1` = 10%) and, if you want, `LABEL`. Each setting has a comment
   explaining it.
3. Save the file.
4. Double-click **`run.bat`**. A window opens, runs the tool, and stays
   open at the end so you can read the result (or, if something needs
   fixing, a plain-English message telling you what and where).

That's it — nothing to type.

## What it produces

One folder: **`output\<label>\`** — plug-and-play, matching the layout the
project's own GTFS pipeline uses for its staged scenarios:

```
output\<label>\
  drt-run-config.xml   <- point MATSim at this
  plans\
  network\
  pt\
  vehicles\
    drt\
  drt-stops\
  service-areas\
```

Every path inside `drt-run-config.xml` is relative to that same folder
(`./network/...`, `./pt/...`, `./drt-stops/...`, etc.) — nothing points
back out to this tool's `input\` folder or to any other project. **Copy the
whole `<label>\` folder anywhere and it still works**, as long as whatever
runs MATSim there points at its `drt-run-config.xml`.

The tool checks this itself before saying it's done: the last step reads
every file path the generated config references and confirms the file
actually exists inside the output folder, so "self-contained" isn't just
a claim — it's verified on every run.

(Running MATSim itself is a separate matter from generating the scenario —
you still need a MATSim/DRT setup — such as this project's own
`matsim-project-ppc` — with a JDK and Maven, to actually simulate it. This
tool's job stops at producing the config folder.)

## How many iterations to run

`ITERATIONS` in the settings controls how many times MATSim replans
(agents trying better routes/modes) before stopping — default 40, matching
the project's own baseline. More iterations means more realistic results
but a longer run; a quick demo might use 5-10 instead. This is unrelated
to `FRACTION` — it's about simulation depth, not scenario size.

## Choosing which tricycle zones (TODAs) operate

By default all 20 zones run, each scaled down proportionally. To run only
specific zones, edit `MODES` in the settings, e.g.:

```python
MODES = ["todat", "westoda"]
```

To force a zone to a fixed number of vehicles instead of the automatic
percentage, use `MODE_FLEET_OVERRIDE`:

```python
MODE_FLEET_OVERRIDE = {"todat": 10, "westoda": 10}
```

Zones you exclude from `MODES` aren't just deleted — the population's
plans already have real per-zone trips baked into some people's plans (not
assigned dynamically), so the tool automatically adds a fallback for every
excluded zone so those trips still complete instead of crashing the
simulation.

## Minimums (so nothing rounds down to zero)

A literal `FRACTION × count` can round to 0 or 1 for a small zone — 1% of
a 21-vehicle fleet is 0.21 vehicles, not a usable service. Two settings
protect against that, and they are **hard lower bounds** — they apply
even if you set a fixed number in `MODE_FLEET_OVERRIDE` below them, not
just to the automatic percentage:

| Setting | Default | Protects |
|---|---|---|
| `DRT_FLEET_MINIMUM` | 2 | Every zone's DRT fleet size |
| `TRANSIT_CAPACITY_MINIMUM` | 2 | The bus's seat capacity |

## Advanced: command-line use

Every setting can be overridden with a flag of the same name instead of
editing the file, e.g.:

```bash
python downsample_pipeline.py --fraction 0.1 --label 10pct
```

A flag always wins over the file's SETTINGS block. Run
`python downsample_pipeline.py --help` for the full list, including
`--config-template` (a different baseline config to start from) and
`--output-dir` (write scenarios somewhere other than this tool's own
`output\` folder).

## A note on the population step

The population downsample is a from-scratch Python reimplementation of
MATSim's own `StreamingPopulationWriter(fraction)` decision rule (keep a
person iff a random draw < fraction, checked once per person in file
order) — verified line-for-line against `matsim-2025.0-sources.jar`. It is
**not** bit-for-bit reproducible against a Java run with the same seed
(Python and Java use different random number generators), but it applies
the identical rule, and copies each kept person's data verbatim rather
than parsing and re-serializing it — so no route, attribute, or plan
detail can be altered in the process.
