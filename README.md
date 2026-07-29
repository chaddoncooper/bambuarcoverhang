# Arc Overhangs for Bambu Studio / OrcaSlicer

`bambu_arc_overhang.py` is an adapter that lets the original arc-overhang
post-processing script (nicolai-wachenschwan / stmcculloch, PrusaSlicer-only)
work on Bambu Studio and OrcaSlicer G-code. It translates the Bambu dialect
(`; FEATURE:`, `; CHANGE_LAYER`, `; CONFIG_BLOCK_START`) into the PrusaSlicer
dialect the original script expects, runs it unmodified, then translates back
losslessly. Bridge infill regions get replaced with self-supporting concentric
arcs printed into free air.

Both `.gcode` and Bambu `.gcode.3mf` files are supported. For `.gcode.3mf` it
patches `Metadata/plate_*.gcode` inside the archive and regenerates the `.md5`
files so the printer accepts it from the SD card.

## Setup (Arch)

Keep both `.py` files in the same folder.

```bash
python -m venv ~/.venvs/arcs
~/.venvs/arcs/bin/pip install -r requirements.txt
```

## Workflow A — Bambu Studio (manual post-process)

Bambu Studio has no post-processing script hook, so:

1. Slice normally. In the overhang area: **no supports**, and leave
   *Detect overhang wall* on (default). Bambu's default relative
   extrusion (M83) is required and already on for Bambu printers.
2. **File → Export → Export plate sliced file** → gives `plate_1.gcode.3mf`.
3. Run:
   ```bash
   ~/.venvs/arcs/bin/python bambu_arc_overhang.py plate_1.gcode.3mf
   # -> plate_1.arcs.gcode.3mf
   ```
4. Copy `plate_1.arcs.gcode.3mf` to the microSD card and start it from the
   printer's touchscreen (or via FTP). Don't re-open and re-slice in Studio —
   that regenerates the G-code. Dragging the file into Studio just to *preview*
   is fine.

Plain `.gcode` export works too: `bambu_arc_overhang.py file.gcode`.

## Workflow B — OrcaSlicer (automatic)

Orca has a post-processing hook: **Process → Others → Post-processing scripts**

```
/home/chad/.venvs/arcs/bin/python /path/to/bambu_arc_overhang.py
```

Orca passes the G-code path and the adapter detects slicer mode
(`SLIC3R_PP_OUTPUT_NAME`) and edits in place. Note the arcs won't show in
Orca's preview (it renders the pre-script G-code); export and drag the file
back in if you want to eyeball it.

## Where arcs get generated

The script targets **Bridge infill** regions — i.e. flat 90° overhangs that the
slicer bridges. It skips the first layer, needs regions ≥ ~50 mm² and bridges
≥ 5 mm, and replaces the bridge with arcs anchored on the previous layer's
outer wall. Steep-but-not-flat overhangs (walls) are untouched.

## Tuning

All knobs live in `makeFullSettingDict()` near the top of
`prusa_slicer_post_processing_script.py`. The ones that matter most:

- `ArcPrintSpeed` (default 90 mm/min = 1.5 mm/s) — arcs must print very slowly
  with full fan; resist the urge to crank this.
- `ArcExtrusionMultiplier` (1.35) — over-extrusion helps arcs bond in mid-air.
- `ExtendIntoPerimeter` / `MaxDistanceFromPerimeter` — coverage vs. bumpiness
  against the perimeter; raise the former if small areas aren't filling.
- `ArcCenterOffset` (2 mm) — set 0 to reach tight spots with smaller arcs.
- `RMax` (110 mm) — max arc radius.
- `ArcFanSpeed` (255) — the adapter maps this to the part fan (`M106 S`).

If a layer under-fills, the script prints a warning with suggested parameter
changes rather than failing silently.

## Caveats

- Arc undersides are functional, not pretty — best for internal geometry.
- Watch the first arc layer; printing into free air is inherently touchy.
  Dry PETG helps a lot here.
- Very slow: a big overhang area can add serious time (full fan + ~1.5 mm/s).
- The X1C's AI spaghetti detection may side-eye the arcs; consider disabling
  it for the print if it false-triggers.
- matplotlib windows: set `"plotArcsEachStep"` / debug plot flags in the
  settings dict only when debugging; the adapter forces the headless Agg
  backend so nothing pops up by default.
