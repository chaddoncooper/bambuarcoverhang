#!/usr/bin/env python3
"""
bambu_arc_overhang.py
=====================
Adapter that lets nicolai-wachenschwan's arc-overhang post-processing script
(written for PrusaSlicer G-code) work on Bambu Studio / OrcaSlicer G-code.

It works by:
  1. Translating the Bambu G-code dialect (; FEATURE:, ; CHANGE_LAYER,
     ; CONFIG_BLOCK_START ...) into the PrusaSlicer dialect the original
     script expects (;TYPE:, ;LAYER_CHANGE, ; prusaslicer_config = begin ...).
     Every translated line carries a lossless restore-marker so nothing is
     mangled on the way back.
  2. Running the unmodified original script
     (prusa_slicer_post_processing_script.py, must sit in the same folder).
  3. Translating the result back into the Bambu dialect, mapping the injected
     arc moves to "; FEATURE: Bridge" so previews colour them sensibly.

Supports plain .gcode files AND Bambu .gcode.3mf archives (it patches
Metadata/plate_*.gcode inside the zip and regenerates the .md5 files so the
printer accepts the file from SD card).

Usage:
    python3 bambu_arc_overhang.py sliced.gcode          -> sliced.arcs.gcode
    python3 bambu_arc_overhang.py plate.gcode.3mf       -> plate.arcs.gcode.3mf
    python3 bambu_arc_overhang.py sliced.gcode -o out.gcode
    python3 bambu_arc_overhang.py sliced.gcode --overwrite

When invoked by OrcaSlicer as a post-processing script (Others tab), the
SLIC3R_PP_OUTPUT_NAME env var is present and the file is modified in place
automatically.

Tuning: all arc parameters (fan speed, feedrates, ExtendIntoPerimeter, ...)
live in makeFullSettingDict() of the ORIGINAL script - edit them there.
"""

import argparse
import builtins
import hashlib
import io
import os
import re
import shutil
import sys
import tempfile
import zipfile

# ----------------------------------------------------------------------------
# Dialect maps
# ----------------------------------------------------------------------------

# Bambu/Orca "; FEATURE: X"  ->  Prusa ";TYPE:Y"
FEATURE_TO_TYPE = {
    "Inner wall":             "Perimeter",
    "Outer wall":             "External perimeter",
    "Overhang wall":          "Overhang perimeter",
    "Sparse infill":          "Internal infill",
    "Internal solid infill":  "Solid infill",
    "Top surface":            "Top solid infill",
    "Bottom surface":         "Solid infill",
    "Bridge":                 "Bridge infill",
    # NB: capital-B "Bridge infill" is what the arc script matches on.
    # "Internal bridge infill" (lowercase b) deliberately does NOT match it.
    "Internal Bridge":        "Internal bridge infill",
    "Internal bridge":        "Internal bridge infill",
    "Ironing":                "Ironing",
    "Gap infill":             "Gap fill",
    "Support":                "Support material",
    "Support interface":      "Support material interface",
    "Support transition":     "Support material",
    "Skirt":                  "Skirt/Brim",
    "Brim":                   "Skirt/Brim",
    "Prime tower":            "Wipe tower",
    "Custom":                 "Custom",
}

# Bambu config-block keys -> Prusa keys the original script reads
KEY_MAP = {
    "inner_wall_line_width":            "perimeter_extrusion_width",
    "line_width":                       "extrusion_width",
    "internal_solid_infill_line_width": "solid_infill_extrusion_width",
    "sparse_infill_line_width":         "infill_extrusion_width",
    "detect_overhang_wall":             "overhangs",
    "reduce_crossing_wall":             "avoid_crossing_perimeters",
    "overhang_fan_speed":               "bridge_fan_speed",
    "retraction_length":                "retract_length",
    "retraction_speed":                 "retract_speed",
    "nozzle_temperature":               "temperature",
    "is_infill_first":                  "infill_first",
    # same-name passthroughs
    "nozzle_diameter":                  "nozzle_diameter",
    "filament_diameter":                "filament_diameter",
    "layer_height":                     "layer_height",
    "travel_speed":                     "travel_speed",
    "use_relative_e_distances":         "use_relative_e_distances",
    "bridge_speed":                     "bridge_speed",
    "bridge_fan_speed":                 "bridge_fan_speed",
}

MARK = ";@BBL@"        # restore marker glued onto translated lines
CFG_BEGIN = "; CONFIG_BLOCK_START"
CFG_END = "; CONFIG_BLOCK_END"

_num_re = re.compile(r"^-?\d+(\.\d+)?$")


def _first_scalar(val: str) -> str:
    """Bambu stores per-extruder/per-filament lists as 'a,b,c' - take a."""
    return val.split(",")[0].strip().strip('"')


def _resolve_width(val: str, nozzle: float) -> str:
    """Handle Orca percentage line widths ('125%')."""
    v = _first_scalar(val)
    if v.endswith("%"):
        try:
            return f"{nozzle * float(v[:-1]) / 100.0:.3f}"
        except ValueError:
            return v
    return v


# ----------------------------------------------------------------------------
# Bambu -> Prusa
# ----------------------------------------------------------------------------

def read_bambu_config(lines):
    cfg, inside = {}, False
    for line in lines:
        s = line.strip()
        if s.startswith(CFG_BEGIN):
            inside = True
            continue
        if s.startswith(CFG_END):
            break
        if inside and s.startswith(";") and "=" in s:
            k, _, v = s.lstrip("; ").partition("=")
            cfg[k.strip()] = v.strip()
    return cfg


def build_prusa_config(cfg, full_text):
    nozzle = 0.4
    if "nozzle_diameter" in cfg:
        try:
            nozzle = float(_first_scalar(cfg["nozzle_diameter"]))
        except ValueError:
            pass

    out = {}
    for bkey, pkey in KEY_MAP.items():
        if bkey not in cfg:
            continue
        val = cfg[bkey]
        if pkey.endswith("extrusion_width"):
            val = _resolve_width(val, nozzle)
        else:
            val = _first_scalar(val)
        # booleans arrive as 0/1 already; leave numerics as-is
        out.setdefault(pkey, val)

    # wall order -> external_perimeters_first
    seq = cfg.get("wall_sequence", cfg.get("wall_infill_order", ""))
    out["external_perimeters_first"] = "1" if seq.lower().startswith("outer wall") else "0"
    if "infill_first" not in out:
        out["infill_first"] = "1" if "infill/inner" in seq.lower() else "0"

    # sane fallbacks so checkforNecesarrySettings() passes
    out.setdefault("nozzle_diameter", str(nozzle))
    out.setdefault("filament_diameter", "1.75")
    ew = out.get("extrusion_width", f"{nozzle * 1.125:.3f}")
    out.setdefault("extrusion_width", ew)
    out.setdefault("perimeter_extrusion_width", ew)
    out.setdefault("solid_infill_extrusion_width", ew)
    out.setdefault("infill_extrusion_width", ew)
    out.setdefault("overhangs", "1")
    out.setdefault("avoid_crossing_perimeters", "0")
    if "use_relative_e_distances" not in out:
        out["use_relative_e_distances"] = "1" if "\nM83" in full_text else "0"
    return out


def bambu_to_prusa(lines):
    """Return (translated_lines). Every rewritten line carries MARK + original."""
    text = "".join(lines)
    cfg = read_bambu_config(lines)
    prusa_cfg = build_prusa_config(cfg, text)

    out = []
    for line in lines:
        raw = line.rstrip("\n")
        s = raw.strip()

        if s.startswith("; CHANGE_LAYER"):
            out.append(f";LAYER_CHANGE{MARK}{raw}\n")
            continue

        if s.startswith("; Z_HEIGHT:"):
            # authoritative layer Z for the arc script (addZ scans G1 ... Z).
            # Restored verbatim on the way back, so the printer never sees it.
            try:
                z = float(s.split(":", 1)[1])
                out.append(f"G1 Z{z:.3f}{MARK}{raw}\n")
            except ValueError:
                out.append(line)
            continue

        if s.startswith("; LAYER_HEIGHT:"):
            try:
                h = float(s.split(":", 1)[1])
                out.append(f";HEIGHT:{h}{MARK}{raw}\n")
            except ValueError:
                out.append(line)
            continue

        if s.startswith("; FEATURE:"):
            feat = s.split(":", 1)[1].strip()
            ptype = FEATURE_TO_TYPE.get(feat, feat)
            out.append(f";TYPE:{ptype}{MARK}{raw}\n")
            continue

        out.append(line)

    # synthetic Prusa config block at EOF
    out.append("\n; prusaslicer_config = begin\n")
    for k, v in prusa_cfg.items():
        out.append(f"; {k} = {v}\n")
    out.append("; prusaslicer_config = end\n")
    return out


# ----------------------------------------------------------------------------
# Prusa -> Bambu (after arc generation)
# ----------------------------------------------------------------------------

def prusa_to_bambu(lines):
    out, in_synth_cfg = [], False
    for line in lines:
        s = line.strip()
        if s == "; prusaslicer_config = begin":
            in_synth_cfg = True
            continue
        if s == "; prusaslicer_config = end":
            in_synth_cfg = False
            continue
        if in_synth_cfg:
            continue
        if MARK in line:
            out.append(line.split(MARK, 1)[1].rstrip("\n") + "\n")
            continue
        # lines injected by the arc script
        if s.startswith(";TYPE:Arc infill"):
            out.append("; FEATURE: Bridge\n")
            continue
        if s.startswith(";TYPE:Solid infill"):   # hilbert-curve replacement infill
            out.append("; FEATURE: Internal solid infill\n")
            continue
        if s.startswith(";TYPE:"):
            # any other stray injected TYPE line: pass through as a comment
            out.append(line)
            continue
        out.append(line)
    return out


# ----------------------------------------------------------------------------
# Core runner
# ----------------------------------------------------------------------------

def run_arc_script_on(prusa_lines):
    """Feed translated gcode to the original script, return processed lines."""
    os.environ.setdefault("MPLBACKEND", "Agg")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import prusa_slicer_post_processing_script as core

    # never let it block on input() (it does so on setting warnings/errors)
    real_input = builtins.input
    builtins.input = lambda *a, **k: print(*a) or ""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".gcode",
                                         delete=False, newline="") as tf:
            tf.writelines(prusa_lines)
            tmp = tf.name
        stream = open(tmp, "r")
        core.main(stream, tmp, skipInput=True)
        with open(tmp, "r") as f:
            result = f.readlines()
    finally:
        builtins.input = real_input
        try:
            os.remove(tmp)
        except OSError:
            pass
    return result


def process_gcode_text(lines):
    translated = bambu_to_prusa(lines)
    processed = run_arc_script_on(translated)
    return prusa_to_bambu(processed)


# ----------------------------------------------------------------------------
# File / 3mf handling
# ----------------------------------------------------------------------------

def process_plain(in_path, out_path):
    with open(in_path, "r", errors="replace", newline="") as f:
        lines = f.readlines()
    result = process_gcode_text(lines)
    with open(out_path, "w", newline="") as f:
        f.writelines(result)
    print(f"Written: {out_path}")


def process_3mf(in_path, out_path):
    with zipfile.ZipFile(in_path, "r") as zin:
        names = zin.namelist()
        plates = [n for n in names
                  if re.fullmatch(r"Metadata/plate_\d+\.gcode", n)]
        if not plates:
            sys.exit("No Metadata/plate_*.gcode found - is this a sliced .gcode.3mf?")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            new_gcode = {}
            for plate in plates:
                lines = zin.read(plate).decode("utf-8",
                                               errors="replace").splitlines(keepends=True)
                print(f"--- processing {plate} ---")
                new_gcode[plate] = "".join(process_gcode_text(lines)).encode("utf-8")

            for n in names:
                if n in new_gcode:
                    zout.writestr(n, new_gcode[n])
                elif n.endswith(".gcode.md5") and n[:-4] in new_gcode:
                    md5 = hashlib.md5(new_gcode[n[:-4]]).hexdigest().upper()
                    zout.writestr(n, md5)
                else:
                    zout.writestr(n, zin.read(n))

    with open(out_path, "wb") as f:
        f.write(buf.getvalue())
    print(f"Written: {out_path}")


def default_out(path):
    for ext in (".gcode.3mf", ".gcode"):
        if path.lower().endswith(ext):
            return path[:-len(ext)] + ".arcs" + ext
    return path + ".arcs"


def main():
    ap = argparse.ArgumentParser(description="Arc overhangs for Bambu Studio / OrcaSlicer G-code")
    ap.add_argument("input", help=".gcode or .gcode.3mf file")
    ap.add_argument("-o", "--output", help="output path (default: <name>.arcs.<ext>)")
    ap.add_argument("--overwrite", action="store_true", help="modify the input file in place")
    args = ap.parse_args()

    in_path = args.input
    if not os.path.isfile(in_path):
        sys.exit(f"File not found: {in_path}")

    # OrcaSlicer post-processing mode: must edit in place
    slicer_mode = "SLIC3R_PP_OUTPUT_NAME" in os.environ
    if args.overwrite or slicer_mode:
        out_path = in_path
    else:
        out_path = args.output or default_out(in_path)

    if in_path.lower().endswith(".3mf"):
        if out_path == in_path:
            tmp = in_path + ".tmp3mf"
            process_3mf(in_path, tmp)
            shutil.move(tmp, in_path)
        else:
            process_3mf(in_path, out_path)
    else:
        process_plain(in_path, out_path)


if __name__ == "__main__":
    main()
