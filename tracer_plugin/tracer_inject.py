"""Merge a generated schematic into an existing one.

The Tracer backend already emits a complete, verified `.kicad_sch` -- symbols
placed on grid, wires orthogonal, labels clear of copper. Re-deriving that
geometry inside KiCad would throw all of it away, so injection copies the
finished artefact instead: load both sheets, shift the new content clear of
whatever is already on the target, renumber any reference designator that
collides, and write the target back.

Kept free of `wx` and `pcbnew` on purpose -- it imports under plain CPython, so
the merge can be tested without KiCad running.

`kicad_sch_api` is installed in the system Python, not in KiCad's bundled one,
so the plugin runs this module as a subprocess rather than importing it:

    python tracer_inject.py --generated NEW.kicad_sch --target SHEET.kicad_sch

which prints the same summary dict as JSON on stdout.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import kicad_sch_api as ksa

GRID_MM = 1.27

# Clear space left between what is already on the sheet and what we paste in.
DEFAULT_GAP_MM = 25.4


def _snap(value: float, grid: float = GRID_MM) -> float:
    return round(value / grid) * grid


def _xy(point: Any) -> Tuple[float, float]:
    """Read an (x, y) out of a Point, a tuple, or an object with .position."""
    if hasattr(point, "x") and hasattr(point, "y"):
        return float(point.x), float(point.y)
    if isinstance(point, (tuple, list)) and len(point) >= 2:
        return float(point[0]), float(point[1])
    raise TypeError(f"not a point: {point!r}")


def content_bounds(sch: "ksa.Schematic") -> Optional[Tuple[float, float, float, float]]:
    """Bounding box of everything drawn on a sheet, or None if it is empty.

    Symbol bodies extend past their origin, so this is an approximation -- the
    gap added on top of it is what actually keeps the two blocks apart.
    """
    xs: List[float] = []
    ys: List[float] = []

    for comp in sch.components:
        x, y = _xy(comp.position)
        xs.append(x)
        ys.append(y)
    for wire in sch.wires:
        for pt in wire.points:
            x, y = _xy(pt)
            xs.append(x)
            ys.append(y)
    for label in sch.labels:
        x, y = _xy(label.position)
        xs.append(x)
        ys.append(y)

    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def plan_offset(
    target: "ksa.Schematic",
    generated: "ksa.Schematic",
    gap_mm: float = DEFAULT_GAP_MM,
) -> Tuple[float, float]:
    """Where to put the new block so it does not land on existing content.

    Straight down the sheet: schematics grow downward far more naturally than
    sideways, and it keeps the generated block's own left-to-right flow intact.
    """
    target_box = content_bounds(target)
    generated_box = content_bounds(generated)
    if target_box is None or generated_box is None:
        return 0.0, 0.0

    _, _, _, target_bottom = target_box
    gen_left, gen_top, _, _ = generated_box

    # Keep x as-authored so the flow still starts at the same left margin.
    dx = 0.0
    dy = (target_bottom + gap_mm) - gen_top
    return _snap(dx), _snap(dy)


def net_names(sch: "ksa.Schematic") -> set:
    """Every net name visible on a sheet, from labels and power symbols.

    Power rails are global by name, so this is what decides whether an injected
    block joins onto the existing supply or floats on its own.
    """
    names = {label.text for label in sch.labels if getattr(label, "text", None)}
    for comp in sch.components:
        lib_id = comp.lib_id or ""
        if lib_id.startswith("power:") and lib_id != "power:PWR_FLAG":
            if comp.value:
                names.add(comp.value)
    return names


def _power_drivers(sch: "ksa.Schematic") -> int:
    """Count PWR_FLAG symbols -- two on one rail is an ERC error."""
    return sum(1 for c in sch.components if (c.lib_id or "") == "power:PWR_FLAG")


def _split_ref(ref: str) -> Tuple[str, Optional[int], int]:
    """'R12' -> ('R', 12, 2); '#PWR001' -> ('#PWR', 1, 3); 'LOGO' -> ('LOGO', None, 0).

    The third element is the digit width, so `#PWR001` renumbers to `#PWR017`
    rather than `#PWR17` and the sheet keeps KiCad's own padding convention.
    """
    digits = ""
    while ref and ref[-1].isdigit():
        digits = ref[-1] + digits
        ref = ref[:-1]
    return ref, (int(digits) if digits else None), len(digits)


def _free_ref(ref: str, taken: set) -> str:
    """Next unused designator sharing this one's prefix and padding."""
    if ref not in taken:
        return ref
    prefix, number, width = _split_ref(ref)
    if number is None:
        candidate, n = f"{ref}_1", 1
        while candidate in taken:
            n += 1
            candidate = f"{ref}_{n}"
        return candidate
    n = number
    while f"{prefix}{n:0{width}d}" in taken:
        n += 1
    return f"{prefix}{n:0{width}d}"


def inject(
    generated_path: str,
    target_path: str,
    gap_mm: float = DEFAULT_GAP_MM,
    backup: bool = True,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Copy every element of `generated_path` into `target_path`.

    Returns a summary of what moved, what got renamed, and where the block
    landed. With `dry_run=True` nothing is written -- the same summary comes
    back so a UI can show the plan before committing.
    """
    generated_file = Path(generated_path)
    target_file = Path(target_path)
    if not generated_file.is_file():
        raise FileNotFoundError(f"generated schematic not found: {generated_file}")
    if not target_file.is_file():
        raise FileNotFoundError(f"target schematic not found: {target_file}")

    generated = ksa.load_schematic(str(generated_file))
    target = ksa.load_schematic(str(target_file))

    dx, dy = plan_offset(target, generated, gap_mm)

    # Work out what will electrically join before anything is written. Sharing
    # a rail is usually the point; sharing it while both halves carry their own
    # regulator and PWR_FLAG is the ERC error nobody expects.
    shared_nets = sorted(net_names(target) & net_names(generated))
    warnings: List[str] = []
    if shared_nets:
        warnings.append(
            "Shares net name(s) with the existing sheet, so they become one net: "
            + ", ".join(shared_nets))
        if _power_drivers(target) and _power_drivers(generated):
            warnings.append(
                "Both sheets carry their own PWR_FLAG/regulator on those rails. "
                "KiCad will report 'Power output and Power output are connected' "
                "-- delete the duplicate driver, or rename the new block's rails.")

    taken = {c.reference for c in target.components}
    renamed: Dict[str, str] = {}
    added_components = 0

    for comp in generated.components:
        ref = _free_ref(comp.reference, taken)
        if ref != comp.reference:
            renamed[comp.reference] = ref
        taken.add(ref)

        x, y = _xy(comp.position)
        if not dry_run:
            new = target.components.add(
                lib_id=comp.lib_id,
                reference=ref,
                value=comp.value,
                position=(x + dx, y + dy),
                footprint=comp.footprint or None,
                rotation=float(getattr(comp, "rotation", 0.0) or 0.0),
            )
            # Carry across the fields `add` does not take -- datasheet,
            # description, and the hidden-reference flags on power symbols.
            try:
                for key, value in (comp.properties or {}).items():
                    if key in ("Reference", "Value", "Footprint"):
                        continue
                    new.set_property(key, value)
            except Exception:
                # A property the target's library does not know about is not
                # worth failing an otherwise good merge over.
                pass
        added_components += 1

    added_wires = 0
    for wire in generated.wires:
        points = [(x + dx, y + dy) for x, y in (_xy(p) for p in wire.points)]
        if not dry_run:
            target.wires.add(points=points, wire_type=wire.wire_type,
                             stroke_width=wire.stroke_width)
        added_wires += 1

    added_labels = 0
    for label in generated.labels:
        x, y = _xy(label.position)
        if not dry_run:
            target.labels.add(
                text=label.text,
                position=(x + dx, y + dy),
                rotation=float(getattr(label, "rotation", 0.0) or 0.0),
                size=float(getattr(label, "size", GRID_MM) or GRID_MM),
            )
        added_labels += 1

    added_junctions = 0
    for junction in generated.junctions:
        x, y = _xy(junction.position)
        if not dry_run:
            target.junctions.add(position=(x + dx, y + dy),
                                 diameter=getattr(junction, "diameter", 0) or 0)
        added_junctions += 1

    added_no_connects = 0
    for nc in generated.no_connects:
        x, y = _xy(nc.position)
        if not dry_run:
            target.no_connects.add(position=(x + dx, y + dy))
        added_no_connects += 1

    backup_path = None
    if not dry_run:
        if backup:
            backup_path = str(target_file.with_suffix(".kicad_sch.tracer-bak"))
            shutil.copy2(target_file, backup_path)
        target.save()

    return {
        "target": str(target_file),
        "source": str(generated_file),
        "dry_run": dry_run,
        "offset_mm": (dx, dy),
        "backup": backup_path,
        "added": {
            "components": added_components,
            "wires": added_wires,
            "labels": added_labels,
            "junctions": added_junctions,
            "no_connects": added_no_connects,
        },
        "renamed": renamed,
        "shared_nets": shared_nets,
        "warnings": warnings,
    }


def describe(summary: Dict[str, Any]) -> str:
    """One human-readable paragraph about an injection."""
    added = summary["added"]
    dx, dy = summary["offset_mm"]
    lines = [
        "{} {} components, {} wires, {} labels, {} junctions, {} no-connects".format(
            "Would add" if summary["dry_run"] else "Added",
            added["components"], added["wires"], added["labels"],
            added["junctions"], added["no_connects"]),
        f"Placed at offset ({dx:+.2f}, {dy:+.2f}) mm on {Path(summary['target']).name}",
    ]
    if summary["renamed"]:
        pairs = ", ".join(f"{a}->{b}" for a, b in sorted(summary["renamed"].items()))
        lines.append(f"Renumbered to avoid collisions: {pairs}")
    if summary.get("backup"):
        lines.append(f"Backup written to {Path(summary['backup']).name}")
    for warning in summary.get("warnings") or []:
        lines.append(f"[!] {warning}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """Subprocess entry point: run a merge, print the summary as JSON.

    Errors come back as `{"error": ...}` with a non-zero exit, so the caller
    never has to parse a traceback out of stderr.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--generated", required=True,
                        help="the .kicad_sch the engine just produced")
    parser.add_argument("--target", required=True,
                        help="the .kicad_sch to merge it into")
    parser.add_argument("--gap-mm", type=float, default=DEFAULT_GAP_MM,
                        help="clear space left below existing content")
    parser.add_argument("--no-backup", action="store_true",
                        help="do not write a .tracer-bak copy of the target")
    parser.add_argument("--dry-run", action="store_true",
                        help="report the plan without writing anything")
    args = parser.parse_args(argv)

    try:
        summary = inject(
            args.generated, args.target,
            gap_mm=args.gap_mm,
            backup=not args.no_backup,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001 -- the caller wants JSON, not a traceback
        json.dump({"error": f"{type(exc).__name__}: {exc}"}, sys.stdout)
        sys.stdout.flush()
        return 1

    # Carry the rendered text along, so a caller that cannot import this module
    # (KiCad's Python has no kicad_sch_api) still gets the human summary.
    summary["text"] = describe(summary)
    json.dump(summary, sys.stdout)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
