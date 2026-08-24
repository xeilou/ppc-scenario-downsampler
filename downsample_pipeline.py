#!/usr/bin/env python3
"""Build a downsampled PPC DRT scenario (population, DRT fleets, transit
capacity, network/schedule, and a generated drt-run-config.xml) in one run.

HOW TO USE THIS: edit the SETTINGS block right below these comments, save
the file, then just run it (double-click run.bat, or `python
downsample_pipeline.py` in a terminal). No command-line arguments needed.

(Advanced: every setting below can also be overridden with a command-line
flag of the same name, e.g. `python downsample_pipeline.py --fraction 0.1`,
without editing the file. Run with --help to see all of them. A flag always
wins over the SETTINGS block.)
"""

import os

# This tool's own folder -- input/ and output/ live alongside this file.
TOOL_DIR = os.path.dirname(os.path.abspath(__file__))

# ============================================================================
# SETTINGS -- edit these, then run this file. That's it.
# ============================================================================

# How much of the full-size scenario to keep, as a decimal fraction.
# 0.01 = 1%, 0.1 = 10%, 0.5 = 50%.
FRACTION = 0.2

# A short label for this scenario. It's used to name the output files and
# folders (e.g. "1pct" -> plans-1pct.xml.gz, downsampled-1pct-demo/, ...).
# Use only letters, numbers, and hyphens.
LABEL = "20pct"

# How many MATSim iterations to run. Higher = more realistic (agents get
# more chances to find better routes/modes) but slower. The project's own
# baseline config uses 40; a quick demo run might use 5-10 instead.
ITERATIONS = 40

# --- The full-size ("100%") input files to shrink down ---
# These already point at the copies bundled in this tool's own input/
# folder -- nothing to change unless you want to downsample different
# source data. Population: the full plans file (NOT an already-downsampled
# one). Transit vehicles/network/schedule: from a PPC-GTFS pipeline
# build's gtfs-output\matsim\ folder.
POPULATION_FILE = os.path.join(TOOL_DIR, "input", "plans.xml.gz")
TRANSIT_VEHICLES_FILE = os.path.join(TOOL_DIR, "input", "ppc-vehicles.xml.gz")
NETWORK_FILE = os.path.join(TOOL_DIR, "input", "ppc-network-transit-mapped.xml.gz")
SCHEDULE_FILE = os.path.join(TOOL_DIR, "input", "ppc-schedule-mapped.xml.gz")
DRT_FULL_DIR = os.path.join(TOOL_DIR, "input", "drt_full")
# drt-stops/ and service-areas/ don't get downsampled -- they're geometry
# (terminal stops, zone boundaries), not demand -- but every generated
# scenario still needs its own copy, since each output folder is fully
# self-contained (see OUTPUT_DIR below: nothing it produces reaches back
# out to this input/ folder, or to any repo, at run time).
DRT_STOPS_DIR = os.path.join(TOOL_DIR, "input", "drt-stops")
SERVICE_AREAS_DIR = os.path.join(TOOL_DIR, "input", "service-areas")

# Where each generated scenario goes: a self-contained folder named after
# LABEL, with everything (config, population, fleets, network, transit,
# stops, service areas) inside it. Nothing outside this folder is needed
# to load the scenario -- copy the whole output\<label>\ folder anywhere
# and it still works.
OUTPUT_DIR = os.path.join(TOOL_DIR, "output")

# --- Which tricycle zones (TODAs) should actually run? ---
# Leave as None to include all 20 zones. Or list specific ones to run only
# those, e.g.:  MODES = ["todat", "westoda"]
MODES = None

# Force specific zones to a FIXED number of vehicles instead of the
# automatic percentage-based count. Leave empty ({}) to let every zone use
# the automatic percentage. Example: {"todat": 10, "westoda": 10}
MODE_FLEET_OVERRIDE = {}

# --- Safety minimums ---
# However small FRACTION is, fleets and bus capacity will never drop below
# these numbers -- this stops a tiny zone from ending up with 0 vehicles.
# This applies even to a fixed number you set in MODE_FLEET_OVERRIDE above.
DRT_FLEET_MINIMUM = 2         # every zone keeps at least this many vehicles
TRANSIT_CAPACITY_MINIMUM = 2  # buses keep at least this many seats

# --- Advanced settings -- most people won't need to touch these ---
SEED = 4711  # for reproducible population sampling
# The baseline drt-run-config.xml this tool starts from and edits. This is
# a one-time snapshot bundled with this tool (input\drt-run-config-template.xml),
# not read live from any repo -- so this tool has no dependency on git, or
# on any matsim-project-ppc checkout being present, to generate a scenario.
CONFIG_TEMPLATE = os.path.join(TOOL_DIR, "input", "drt-run-config-template.xml")

# ============================================================================
# End of settings. Nothing below this line needs to be edited.
# ============================================================================

import argparse
import gzip
import random
import re
import shutil
import sys
import xml.etree.ElementTree as ET

ALL_MODES = [
    "astoda", "bmtoda", "sptoda", "bancao_bancao_toda", "istoda", "istoda2",
    "gvtv", "jtoda", "kabiptoda", "nnps", "nobtoda", "pastoda", "rotonda",
    "litoda", "sjtoda", "sl_toda", "sm_city_toda", "sm_toda", "todat", "westoda",
]


# --------------------------------------------------------------------------
# 1. Population downsampling
# --------------------------------------------------------------------------

def downsample_population(src_path, dst_path, fraction, seed):
    """Stream plans.xml.gz, keeping each <person>...</person> block with
    probability `fraction`. Equivalent to MATSim's own
    StreamingPopulationReader/StreamingPopulationWriter(fraction) decision
    rule (verified against matsim-2025.0-sources.jar), reimplemented in
    pure Python so this script doesn't need a JVM/Maven build to run.
    Kept persons are copied verbatim (never parsed/reserialized), so no
    route, attribute, or plan detail can be altered in the process."""
    rng = random.Random(seed)
    person_open_re = re.compile(r"<person\b")
    person_close_re = re.compile(r"</person>")

    kept = 0
    total = 0
    in_person = False
    buf = []

    with gzip.open(src_path, "rt", encoding="utf-8") as fin, \
            gzip.open(dst_path, "wt", encoding="utf-8", newline="\n") as fout:
        for line in fin:
            if not in_person:
                if person_open_re.search(line):
                    in_person = True
                    buf = [line]
                else:
                    fout.write(line)
                continue

            buf.append(line)
            if person_close_re.search(line):
                in_person = False
                total += 1
                if rng.random() < fraction:
                    kept += 1
                    fout.writelines(buf)
                buf = []

    return total, kept


# --------------------------------------------------------------------------
# 2. DRT fleet downsampling (even-spaced selection, with a floor)
# --------------------------------------------------------------------------

VEHICLE_LINE_RE = re.compile(r'^\s*<vehicle\b.*/>\s*$')


def downsample_fleet(src_path, dst_path, fraction, floor, fixed_count=None):
    with open(src_path, "r", encoding="utf-8-sig") as f:
        lines = f.readlines()

    vehicle_lines = [i for i, l in enumerate(lines) if VEHICLE_LINE_RE.match(l)]
    n = len(vehicle_lines)

    if fixed_count is not None:
        target = fixed_count
    else:
        target = round(n * fraction)
    # floor is an unconditional lower bound -- applies even to an explicit
    # MODE_FLEET_OVERRIDE entry, so a fleet can never be pushed to (near) zero.
    target = max(floor, target)
    target = min(target, n)

    if target >= n:
        picked = list(range(n))
    else:
        step = n / target
        picked = sorted(set(min(round(i * step), n - 1) for i in range(target)))
        i = 0
        while len(picked) < target and i < n:
            if i not in picked:
                picked.append(i)
                picked.sort()
            i += 1
        picked = picked[:target]

    keep_line_no = set(vehicle_lines[i] for i in picked)
    drop_line_no = set(vehicle_lines) - keep_line_no
    out_lines = [l for i, l in enumerate(lines) if i not in drop_line_no]

    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    with open(dst_path, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(out_lines)

    return n, len(keep_line_no)


# --------------------------------------------------------------------------
# 2b. Network: restore storage capacity on synthetic transit-stop connector
#     links (the "artificial" links pt2matsim-style stop-snapping creates)
# --------------------------------------------------------------------------

MODES_RE = re.compile(r'modes="([^"]*)"')
PERMLANES_RE = re.compile(r'permlanes="([^"]+)"')


def fix_artificial_link_storage(content, multiplier=100.0):
    """Multiply permlanes by `multiplier` on every <link> whose modes include
    "artificial" but not "car". These are the short synthetic connector links
    that stop-snapping creates radiating from each real transit stop; their
    <capacity> is already deliberately unconstrained (9999), but MATSim
    derives STORAGE capacity from length*permlanes, which storageCapacityFactor
    still scales down like any other link -- crushing these (often very
    short) links to its ~1-vehicle safety-net floor. Multiple trunk PT routes
    funneling through the same stop then queue single-file on them: real
    PT-only congestion with nothing to do with actual road capacity. Diagnosed
    and fixed the same way, by hand, on 2026-08-24 for a 1% test scenario
    (input/ppc-drt-v1/network-1pct-demo-transitfix/ppc-network-transit-mapped-storagefix.xml.gz)
    -- this generalizes that fix to any FRACTION via this pipeline. Never
    touches a link whose modes include "car" (verified: no mode combination in
    this network mixes "artificial" with "car")."""
    lines = content.splitlines(keepends=True)
    count = 0
    out = []
    for line in lines:
        if "<link " in line and 'modes="' in line:
            mm = MODES_RE.search(line)
            if mm:
                modes = mm.group(1).split(",")
                if "artificial" in modes and "car" not in modes:
                    pm = PERMLANES_RE.search(line)
                    if pm:
                        new_val = float(pm.group(1)) * multiplier
                        line = line[:pm.start(1)] + str(new_val) + line[pm.end(1):]
                        count += 1
        out.append(line)
    return "".join(out), count


LINK_ID_RE = re.compile(r'<link id="([^"]+)"')
STOP_LINKREF_RE = re.compile(r'linkRefId="([^"]+)"')


def collect_drt_terminal_links(drt_stops_dir):
    """Every linkRefId referenced by any drt-stops-<mode>.xml -- these are
    real DRT terminal/depot stop locations, not synthetic bookkeeping."""
    links = set()
    for fname in sorted(os.listdir(drt_stops_dir)):
        if not fname.endswith(".xml"):
            continue
        with open(os.path.join(drt_stops_dir, fname), "r", encoding="utf-8-sig") as f:
            text = f.read()
        links.update(STOP_LINKREF_RE.findall(text))
    return links


def fix_terminal_link_storage(content, terminal_links, multiplier=100.0):
    """Multiply permlanes by `multiplier` on every <link> that's a registered
    DRT terminal/depot stop (present in some mode's drt-stops-<mode>.xml).
    Diagnosed 2026-08-24: DRT's idle-vehicle "return to nearest depot"
    behavior sends every idle vehicle in a mode's fleet to the nearest of
    that mode's own (often very few) real terminal links -- for zones with
    thin terminal data (e.g. sm_city_toda: 858 vehicles, only 3 distinct
    terminal links), tens of vehicles converge on one link at once. Unlike
    the artificial PT-connector links, these ARE real car-mode roads
    (needed so DRT vehicles can actually drive there) -- but pt2matsim's
    survey data labels them e.g. "cluster 4, 6.2h dwell", meaning these
    specific spots were identified from real GPS data as places tricycles
    actually sit for hours. Their true real-world capacity is a proper
    waiting area, not a single travel lane, so modeling them as ~1-vehicle
    storage (length*permlanes, crushed further by storageCapacityFactor) is
    itself the inaccuracy -- this brings it closer to reality, not further
    from it. Deliberately narrower than fix_artificial_link_storage: only
    links that are actually registered as a stop get touched, not every
    car-mode link, so ordinary road-traffic modeling elsewhere is untouched."""
    lines = content.splitlines(keepends=True)
    count = 0
    out = []
    for line in lines:
        if "<link " in line and 'permlanes="' in line:
            idm = LINK_ID_RE.search(line)
            if idm and idm.group(1) in terminal_links:
                pm = PERMLANES_RE.search(line)
                if pm:
                    new_val = float(pm.group(1)) * multiplier
                    line = line[:pm.start(1)] + str(new_val) + line[pm.end(1):]
                    count += 1
        out.append(line)
    return "".join(out), count


def apply_network_storage_fix(src_path, dst_path, drt_stops_dir, multiplier=100.0):
    with gzip.open(src_path, "rt", encoding="utf-8") as f:
        content = f.read()

    content, artificial_count = fix_artificial_link_storage(content, multiplier)
    if artificial_count == 0:
        raise RuntimeError(f"Expected artificial-mode links with a permlanes attribute in "
                            f"{src_path}, found none -- network format may have changed")

    terminal_links = collect_drt_terminal_links(drt_stops_dir)
    content, terminal_count = fix_terminal_link_storage(content, terminal_links, multiplier)
    if terminal_links and terminal_count == 0:
        raise RuntimeError(f"Found {len(terminal_links)} DRT terminal stop links but none matched "
                            f"a <link> in {src_path} -- network/drt-stops may be out of sync")

    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    with gzip.open(dst_path, "wt", encoding="utf-8", newline="\n") as f:
        f.write(content)

    return artificial_count, terminal_count


# --------------------------------------------------------------------------
# 3. Transit vehicles: copy + scale seat capacity and PCE from real base values
# --------------------------------------------------------------------------

def downsample_transit_vehicles(src_path, dst_path, fraction, capacity_floor):
    with gzip.open(src_path, "rt", encoding="utf-8") as f:
        content = f.read()

    m = re.search(r'<capacity seats="(\d+)" standingRoomInPersons="(\d+)">', content)
    if not m:
        raise RuntimeError(f"Could not find a <capacity seats=...> element in {src_path}")
    base_seats = int(m.group(1))
    new_seats = max(capacity_floor, round(base_seats * fraction))

    content = content[:m.start()] + \
        f'<capacity seats="{new_seats}" standingRoomInPersons="{m.group(2)}">' + \
        content[m.end():]

    # Bus vehicles themselves are NOT downsampled (each simulated bus is one
    # real bus, not a stand-in for 1/fraction of them, unlike cars/DRT whose
    # *counts* get thinned). passengerCarEquivalents (pce) is therefore left
    # at its real, full-scale value -- unscaled -- matching network capacity
    # no longer being downscaled either (see generate_config:
    # flowCapacityFactor/storageCapacityFactor are now always 1.0). Diagnosed
    # 2026-08-24: forcing those two factors to 1.0 as a diagnostic made
    # car-mode stuckAndAbort go from 91 -> 0 by itself, with pce untouched --
    # that's what disabling network downscaling now does permanently, so the
    # pce-scaling workaround this comment used to describe is no longer
    # needed (kept here only as history; scaling pce down would now just
    # make buses artificially lighter than real).
    pm = re.search(r'<passengerCarEquivalents pce="([\d.eE+-]+)"', content)
    if not pm:
        raise RuntimeError(f"Could not find a <passengerCarEquivalents pce=...> element in {src_path}")
    base_pce = float(pm.group(1))
    new_pce = base_pce
    content = content[:pm.start(1)] + str(new_pce) + content[pm.end(1):]

    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    with gzip.open(dst_path, "wt", encoding="utf-8", newline="\n") as f:
        f.write(content)

    return base_seats, new_seats, base_pce, new_pce


# --------------------------------------------------------------------------
# 4. Config generation
# --------------------------------------------------------------------------

def load_baseline_config_text(config_template):
    with open(config_template, "r", encoding="utf-8") as f:
        return f.read()


def find_top_level_blocks(lines, start, end, open_prefix):
    blocks = []
    i = start
    while i < end:
        line = lines[i]
        if line.lstrip().startswith(open_prefix):
            depth = 1
            j = i + 1
            while j < end and depth > 0:
                s = lines[j].lstrip()
                if s.startswith("<parameterset") and s.rstrip().endswith(">") and not s.rstrip().endswith("/>"):
                    depth += 1
                elif s.startswith("</parameterset>"):
                    depth -= 1
                j += 1
            blocks.append((i, j))
            i = j
        else:
            i += 1
    return blocks


def module_bounds(lines, name):
    start = next(i for i, l in enumerate(lines) if f'<module name="{name}">' in l)
    end = next(i for i, l in enumerate(lines) if i > start and l.strip() == "</module>")
    return start, end


def generate_config(cfg, kept_modes, fleet_counts, base_seats, new_seats, base_pce, new_pce,
                     artificial_link_count, terminal_link_count):
    text = load_baseline_config_text(cfg["config_template"])

    # The baseline template's untouched paths (drt-stops, service-areas)
    # point at "../../input/ppc-drt-v1/...", the project's own repo
    # layout. This tool's output is self-contained instead -- every file a
    # generated scenario needs lives right next to its own config, e.g.
    # ./drt-stops/... -- so rebase those references once here, on the
    # still-pristine baseline text. (Every OTHER path -- network, transit,
    # plans, DRT fleets, output dir -- gets fully replaced below anyway, so
    # this rebase only actually matters for drt-stops/service-areas, but
    # it's harmless if it touches paths that get overwritten a moment later.)
    text = text.replace('value="../../input/ppc-drt-v1/drt-stops/', 'value="./drt-stops/')
    text = text.replace('value="../../input/ppc-drt-v1/service-areas/', 'value="./service-areas/')
    lines = text.splitlines(keepends=True)

    excluded_modes = [m for m in ALL_MODES if m not in kept_modes]
    label = cfg["label"]
    fraction = cfg["fraction"]

    # -- network / transit / plans / output dir / iteration count --
    text = "".join(lines)
    text = re.sub(
        r'<param name="inputNetworkFile" value="[^"]+" />',
        f'<param name="inputNetworkFile" value="./network/{os.path.basename(cfg["network"])}" />',
        text, count=1,
    )
    text = re.sub(
        r'<param name="transitScheduleFile" value="[^"]+" />',
        f'<param name="transitScheduleFile" value="./pt/{os.path.basename(cfg["schedule"])}" />',
        text, count=1,
    )
    text = re.sub(
        r'(<module name="transit">.*?)<param name="vehiclesFile" value="[^"]+" />',
        rf'\1<param name="vehiclesFile" value="./vehicles/{os.path.basename(cfg["transit_vehicles"])}" />',
        text, count=1, flags=re.DOTALL,
    )
    # Network road capacity is NOT downscaled with the population -- both
    # factors stay at full (1.0) regardless of FRACTION. (Previously these
    # matched FRACTION, shrinking road capacity in lockstep with demand;
    # disabled outright per 2026-08-25 decision so road capacity is always
    # full-scale.)
    text = re.sub(
        r'<param name="flowCapacityFactor" value="[^"]+" />',
        '<param name="flowCapacityFactor" value="1.0" />', text, count=1,
    )
    text = re.sub(
        r'<param name="storageCapacityFactor" value="[^"]+" />',
        '<param name="storageCapacityFactor" value="1.0" />', text, count=1,
    )
    text = re.sub(
        r'<param name="inputPlansFile" value="[^"]+" />',
        f'<param name="inputPlansFile" value="./plans/{os.path.basename(cfg["population"])}" />',
        text, count=1,
    )
    text = re.sub(
        r'<param name="outputDirectory" value="[^"]+" />',
        f'<param name="outputDirectory" value="./output-downsampled-{label}-demo" />',
        text, count=1,
    )
    text = re.sub(
        r'<param name="lastIteration" value="[^"]+" />',
        f'<param name="lastIteration" value="{cfg["iterations"]}" />', text, count=1,
    )
    lines = text.splitlines(keepends=True)

    # -- multiModeDrt: keep only kept_modes, rewrite their vehiclesFile --
    mm_start, mm_end = module_bounds(lines, "multiModeDrt")
    drt_blocks = find_top_level_blocks(lines, mm_start + 1, mm_end, '<parameterset type="drt"')
    new_mm_body = []
    for (i, j) in drt_blocks:
        block = lines[i:j]
        block_text = "".join(block)
        mode = re.search(r'<param name="mode" value="drt_([^"]+)"', block_text).group(1)
        if mode not in kept_modes:
            continue
        block_text = re.sub(
            r'<param name="vehiclesFile" value="[^"]+" />',
            f'<param name="vehiclesFile" value="./vehicles/drt/drt_vehicles_{mode}.xml" />',
            block_text, count=1,
        )
        new_mm_body.append(block_text)
    lines = lines[:mm_start + 1] + [b for b in new_mm_body] + lines[mm_end:]

    # -- routing: append teleported fallback for excluded modes --
    r_start, r_end = module_bounds(lines, "routing")
    fallback_teleport = "".join(
        '        <parameterset type="teleportedModeParameters">\n'
        f'            <param name="mode" value="drt_{m}" />\n'
        '            <param name="teleportedModeSpeed" value="8.0" />\n'
        '            <param name="beelineDistanceFactor" value="1.3" />\n'
        '        </parameterset>\n'
        for m in excluded_modes
    )
    if fallback_teleport:
        lines = lines[:r_end] + [fallback_teleport] + lines[r_end:]
        r_start, r_end = module_bounds(lines, "routing")  # indices shifted

    # -- scoring: drop modeParams for excluded modes UNLESS also adding a
    #    fallback there, and add fallback modeParams for excluded modes --
    sc_start, sc_end = module_bounds(lines, "scoring")
    mp_blocks = find_top_level_blocks(lines, sc_start + 1, sc_end, '<parameterset type="modeParams"')
    drop_idx = set()
    for (i, j) in mp_blocks:
        block_text = "".join(lines[i:j])
        m = re.search(r'<param name="mode" value="drt_([^"]+)"', block_text)
        if m and m.group(1) in excluded_modes:
            drop_idx.update(range(i, j))
    lines = [l for idx, l in enumerate(lines) if idx not in drop_idx]

    if excluded_modes:
        sc_start, sc_end = module_bounds(lines, "scoring")
        fallback_scoring = "".join(
            '        <parameterset type="modeParams" >\n'
            f'            <param name="mode" value="drt_{m}" />\n'
            '            <param name="constant" value="0.0" />\n'
            '            <param name="marginalUtilityOfTraveling_util_hr" value="-6.0" />\n'
            '            <param name="monetaryDistanceRate" value="0.0" />\n'
            '        </parameterset>\n'
            for m in excluded_modes
        )
        # insert right after the last kept drt_* modeParams block, before "pt"
        text = "".join(lines)
        text = text.replace(
            '        <parameterset type="modeParams" >\n            <param name="mode" value="pt" />',
            fallback_scoring + '        <parameterset type="modeParams" >\n            <param name="mode" value="pt" />',
            1,
        )
        lines = text.splitlines(keepends=True)

    # -- subtourModeChoice modes list --
    text = "".join(lines)
    modes_list = "car,pt,motorcycle," + ",".join(f"drt_{m}" for m in kept_modes) + ",walk"
    text = re.sub(
        r'(<module name="subtourModeChoice">.*?<param name="modes" value=")[^"]+(" />)',
        rf'\g<1>{modes_list}\g<2>', text, count=1, flags=re.DOTALL,
    )

    # -- header banner --
    banner = (
        f"<!-- Generated by downsample_pipeline.py, fraction={fraction}, "
        f"label={label}, iterations={cfg['iterations']}.\n"
        f"     DRT modes: {', '.join(kept_modes)}"
        + (f" (excluded: {', '.join(excluded_modes)}, kept as teleported/scoring "
           f"fallbacks — see routing/scoring modules)" if excluded_modes else "")
        + f".\n     Transit Bus capacity: {base_seats} real seats -> {new_seats} "
        f"(fraction={fraction}, floor={cfg['transit_capacity_floor']}).\n"
        f"     Transit Bus PCE: {new_pce} (real/unscaled — network capacity is not "
        f"downscaled, see below, so no pce adjustment is needed).\n"
        f"     Network flowCapacityFactor/storageCapacityFactor: fixed at 1.0 (network "
        f"downscaling disabled — road capacity does not shrink with FRACTION).\n"
        f"     Network storage-capacity fix: permlanes x100 on {artificial_link_count} artificial "
        f"(bus-only) connector links, so storageCapacityFactor doesn't crush them to a "
        f"1-vehicle floor. Also on {terminal_link_count} real DRT terminal/depot links, so "
        f"thin-terminal-data zones' idle fleets don't oversubscribe a single link's queue space.\n"
        f"     Fleet counts: " + ", ".join(f"{m}={fleet_counts[m][1]}/{fleet_counts[m][0]}"
                                            for m in kept_modes) + ". -->\n"
    )
    # A literal "--" anywhere in an XML comment body crashes MATSim's own
    # parser instantly (WstxParsingException) -- see handoff.md. label is the
    # one free-text field a caller controls that ends up in this comment.
    banner_body = banner[len("<!--"):-len("-->\n")]
    if "--" in banner_body:
        raise ValueError(
            f"generated config banner comment contains a literal '--', which "
            f"crashes MATSim's XML parser: {banner_body!r}"
        )
    text = text.replace("<config>\n", "<config>\n" + banner, 1)

    return text


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def parse_mode_fleet(spec):
    out = {}
    if not spec:
        return out
    for pair in spec.split(","):
        mode, count = pair.split("=")
        out[mode.strip()] = int(count.strip())
    return out


def build_cli():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fraction", type=float, default=None, help=f"default: {FRACTION} (set at top of file)")
    ap.add_argument("--label", default=None, help=f"default: {LABEL!r} (set at top of file)")
    ap.add_argument("--iterations", type=int, default=None, help=f"default: {ITERATIONS} (set at top of file)")
    ap.add_argument("--seed", type=int, default=None, help=f"default: {SEED} (set at top of file)")

    ap.add_argument("--population", default=None, help="default: POPULATION_FILE (set at top of file)")
    ap.add_argument("--drt-full-dir", default=None,
                     help="dir of full (100%%) per-TODA drt_vehicles_<mode>.xml files "
                          "(default: DRT_FULL_DIR at top of file, this tool's own input/drt_full/)")
    ap.add_argument("--drt-floor", type=int, default=None,
                     help=f"default: {DRT_FLEET_MINIMUM} (set at top of file)")

    ap.add_argument("--transit-vehicles", default=None, help="default: TRANSIT_VEHICLES_FILE (set at top of file)")
    ap.add_argument("--transit-capacity-floor", type=int, default=None,
                     help=f"default: {TRANSIT_CAPACITY_MINIMUM} (set at top of file)")
    ap.add_argument("--network", default=None, help="default: NETWORK_FILE (set at top of file)")
    ap.add_argument("--schedule", default=None, help="default: SCHEDULE_FILE (set at top of file)")

    ap.add_argument("--modes", default=None,
                     help="comma list of TODAs to give real DVRP fleets (default: MODES at top of file, or all 20)")
    ap.add_argument("--mode-fleet", default=None,
                     help='override fraction-based fleet count per mode, e.g. "todat=10,westoda=10" '
                          '(default: MODE_FLEET_OVERRIDE at top of file; still subject to the floor)')

    ap.add_argument("--config-template", default=None,
                     help="baseline drt-run-config.xml to start from (default: CONFIG_TEMPLATE at top of file, "
                          "the bundled input/drt-run-config-template.xml)")
    ap.add_argument("--drt-stops-dir", default=None,
                     help="default: DRT_STOPS_DIR at top of file, this tool's own input/drt-stops/")
    ap.add_argument("--service-areas-dir", default=None,
                     help="default: SERVICE_AREAS_DIR at top of file, this tool's own input/service-areas/")
    ap.add_argument("--output-dir", default=None,
                     help=f"where generated scenarios go (default: {OUTPUT_DIR!r})")
    return ap


def resolve_settings(args):
    """Merge command-line flags (if given) over the SETTINGS block at the
    top of the file. A flag always wins; otherwise the SETTINGS value."""
    cfg = {}
    cfg["fraction"] = args.fraction if args.fraction is not None else FRACTION
    cfg["label"] = args.label if args.label is not None else LABEL
    cfg["iterations"] = args.iterations if args.iterations is not None else ITERATIONS
    cfg["seed"] = args.seed if args.seed is not None else SEED
    cfg["population"] = args.population if args.population is not None else POPULATION_FILE
    cfg["transit_vehicles"] = args.transit_vehicles if args.transit_vehicles is not None else TRANSIT_VEHICLES_FILE
    cfg["network"] = args.network if args.network is not None else NETWORK_FILE
    cfg["schedule"] = args.schedule if args.schedule is not None else SCHEDULE_FILE
    cfg["drt_floor"] = args.drt_floor if args.drt_floor is not None else DRT_FLEET_MINIMUM
    cfg["transit_capacity_floor"] = (
        args.transit_capacity_floor if args.transit_capacity_floor is not None else TRANSIT_CAPACITY_MINIMUM)
    cfg["config_template"] = args.config_template if args.config_template is not None else CONFIG_TEMPLATE

    if args.modes is not None:
        cfg["modes"] = [m.strip() for m in args.modes.split(",") if m.strip()]
    elif MODES is not None:
        cfg["modes"] = list(MODES)
    else:
        cfg["modes"] = list(ALL_MODES)

    if args.mode_fleet is not None:
        cfg["mode_fleet"] = parse_mode_fleet(args.mode_fleet)
    else:
        cfg["mode_fleet"] = dict(MODE_FLEET_OVERRIDE)

    cfg["drt_full_dir"] = args.drt_full_dir or DRT_FULL_DIR
    cfg["drt_stops_dir"] = args.drt_stops_dir or DRT_STOPS_DIR
    cfg["service_areas_dir"] = args.service_areas_dir or SERVICE_AREAS_DIR
    cfg["output_dir"] = args.output_dir or OUTPUT_DIR

    return cfg


def check_settings(cfg):
    """Friendly, plain-language checks before doing any real work -- a
    non-technical user should get 'edit this setting, here's why' instead
    of a Python traceback."""
    problems = []

    file_setting_map = {
        "population": "POPULATION_FILE",
        "transit_vehicles": "TRANSIT_VEHICLES_FILE",
        "network": "NETWORK_FILE",
        "schedule": "SCHEDULE_FILE",
        "config_template": "CONFIG_TEMPLATE",
    }
    for key, setting_name in file_setting_map.items():
        value = cfg[key]
        if not os.path.isfile(value):
            problems.append(f"{setting_name} is set to:\n        {value}\n"
                             f"      ...but that file doesn't exist. Check the path, or that the "
                             f"file is still in this tool's input\\ folder.")

    if not (0 < cfg["fraction"] <= 1):
        problems.append(f"FRACTION is {cfg['fraction']}, but it needs to be a number greater than 0 "
                         f"and at most 1 (e.g. 0.01 for 1%%, 0.1 for 10%%).")

    if not re.match(r'^[A-Za-z0-9\-]+$', cfg["label"]):
        problems.append(f"LABEL is {cfg['label']!r}, but it can only contain letters, numbers, and "
                         f"hyphens (no spaces or other punctuation).")

    if cfg["iterations"] < 0:
        problems.append(f"ITERATIONS is {cfg['iterations']}, but it needs to be 0 or more "
                         f"(0 means just the initial plan, no replanning).")

    unknown = set(cfg["modes"]) - set(ALL_MODES)
    if unknown:
        problems.append(f"MODES lists unknown zone(s): {', '.join(sorted(unknown))}. "
                         f"Valid zones are: {', '.join(ALL_MODES)}.")

    unknown_override = set(cfg["mode_fleet"]) - set(cfg["modes"])
    if unknown_override:
        problems.append(f"MODE_FLEET_OVERRIDE mentions zone(s) not in MODES: "
                         f"{', '.join(sorted(unknown_override))}.")

    if not os.path.isdir(cfg["drt_full_dir"]):
        problems.append(f"Can't find the full-size DRT fleet folder at:\n        {cfg['drt_full_dir']}\n"
                         f"      Check that this tool's input\\drt_full\\ folder still has the 20 "
                         f"drt_vehicles_<zone>.xml files in it.")

    if not os.path.isdir(cfg["drt_stops_dir"]):
        problems.append(f"Can't find the DRT stops folder at:\n        {cfg['drt_stops_dir']}\n"
                         f"      Check that this tool's input\\drt-stops\\ folder is still there.")

    if not os.path.isdir(cfg["service_areas_dir"]):
        problems.append(f"Can't find the service areas folder at:\n        {cfg['service_areas_dir']}\n"
                         f"      Check that this tool's input\\service-areas\\ folder is still there.")

    return problems


def run(cfg):
    kept_modes = cfg["modes"]

    # Self-contained: everything the generated config references lives
    # under this one folder. No other folder -- not this tool's own
    # input\, not any repo -- is needed to load the scenario afterward.
    scenario_dir = os.path.join(cfg["output_dir"], cfg["label"])
    if os.path.isdir(scenario_dir):
        shutil.rmtree(scenario_dir)
    plans_dir = os.path.join(scenario_dir, "plans")
    network_dir = os.path.join(scenario_dir, "network")
    pt_dir = os.path.join(scenario_dir, "pt")
    vehicles_dir = os.path.join(scenario_dir, "vehicles")
    drt_fleet_dir = os.path.join(vehicles_dir, "drt")
    for d in (plans_dir, network_dir, pt_dir, drt_fleet_dir):
        os.makedirs(d, exist_ok=True)

    print(f"[1/7] Downsampling population @ fraction={cfg['fraction']} ...")
    plans_dst = os.path.join(plans_dir, os.path.basename(cfg["population"]))
    total, kept = downsample_population(cfg["population"], plans_dst, cfg["fraction"], cfg["seed"])
    print(f"      {kept}/{total} persons kept ({kept/total:.2%})")

    print(f"[2/7] Downsampling DRT fleets for zones: {', '.join(kept_modes)} ...")
    fleet_counts = {}
    for mode in kept_modes:
        src = os.path.join(cfg["drt_full_dir"], f"drt_vehicles_{mode}.xml")
        dst = os.path.join(drt_fleet_dir, f"drt_vehicles_{mode}.xml")
        n, k = downsample_fleet(src, dst, cfg["fraction"], cfg["drt_floor"], cfg["mode_fleet"].get(mode))
        fleet_counts[mode] = (n, k)
        print(f"      {mode}: {n} -> {k}")

    print("[3/7] Copying network (fixing artificial-link and DRT-terminal storage capacity), "
          "schedule, scaling transit capacity ...")
    network_dst = os.path.join(network_dir, os.path.basename(cfg["network"]))
    artificial_link_count, terminal_link_count = apply_network_storage_fix(
        cfg["network"], network_dst, cfg["drt_stops_dir"])
    print(f"      Storage-capacity fix: permlanes x100 on {artificial_link_count} artificial "
          f"(bus-only) links, {terminal_link_count} DRT terminal/depot links")
    shutil.copy(cfg["schedule"], os.path.join(pt_dir, os.path.basename(cfg["schedule"])))
    veh_dst = os.path.join(vehicles_dir, os.path.basename(cfg["transit_vehicles"]))
    base_seats, new_seats, base_pce, new_pce = downsample_transit_vehicles(
        cfg["transit_vehicles"], veh_dst, cfg["fraction"], cfg["transit_capacity_floor"])
    print(f"      Bus capacity: {base_seats} -> {new_seats} seats")
    print(f"      Bus PCE: {new_pce} (real/unscaled -- network capacity is not downscaled)")

    print("[4/7] Copying DRT stops and service area geometry (not downsampled, just copied) ...")
    shutil.copytree(cfg["drt_stops_dir"], os.path.join(scenario_dir, "drt-stops"))
    shutil.copytree(cfg["service_areas_dir"], os.path.join(scenario_dir, "service-areas"))

    print("[5/7] Generating drt-run-config.xml ...")
    config_text = generate_config(cfg, kept_modes, fleet_counts, base_seats, new_seats,
                                   base_pce, new_pce, artificial_link_count, terminal_link_count)
    config_path = os.path.join(scenario_dir, "drt-run-config.xml")
    with open(config_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(config_text)

    print("[6/7] Checking the generated config is well-formed XML ...")
    ET.parse(config_path)  # raises on malformed XML

    print("[7/7] Checking every file the config references actually exists ...")
    missing = []
    for m in re.finditer(r'value="(\./[^"]+\.(?:xml|xml\.gz|shp))"', config_text):
        p = os.path.normpath(os.path.join(scenario_dir, m.group(1)))
        if not os.path.isfile(p):
            missing.append(m.group(1))
    if missing:
        raise RuntimeError(f"the generated config references {len(missing)} file(s) that don't "
                            f"exist in the output folder: {', '.join(missing[:5])}"
                            + (", ..." if len(missing) > 5 else ""))

    print(f"\nDone! Self-contained scenario: {config_path}")
    print(f"  (the whole {scenario_dir} folder can be copied anywhere -- nothing outside it is needed)")


def main():
    args = build_cli().parse_args()
    cfg = resolve_settings(args)

    problems = check_settings(cfg)
    if problems:
        print("Before this can run, please fix the following:\n")
        for i, p in enumerate(problems, 1):
            print(f"  {i}. {p}\n")
        sys.exit(1)

    try:
        run(cfg)
    except Exception as e:
        print(f"\nSomething went wrong: {e}\n")
        print("If this doesn't make sense, share this message with whoever set up this script.")
        raise


if __name__ == "__main__":
    main()
