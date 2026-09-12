"""Tracer generator (MVP).

Reads a mock AI output JSON describing a single circuit block and emits a
KiCad 9 schematic (.kicad_sch) using the `kicad-sch-api` library.

Three algorithms keep the output readable instead of spaghetti:

  1. Grid snapping     - every coordinate the generator emits (component
                         anchors, wire waypoints, junctions, labels) is forced
                         onto a 1.27 mm / 50 mil grid. `Component.move()` does
                         not snap for us, so `snap()` is the sole authority.
  2. Orthogonal router - wires leave a pin along the direction that pin
                         actually faces, then reach the target through
                         generated intermediate waypoints so that every emitted
                         segment is strictly horizontal or vertical.
  3. Layout math       - components are ordered strictly left to right by
                         column, rows are aligned slots shared across all
                         columns, and the component tagged `role: controller`
                         is pinned to the dead centre of the sheet.

Usage:
    python generator.py [input.json] [output.kicad_sch]

Defaults:
    input  = ./mock_ai_output.json
    output = ./tracer_output.kicad_sch
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from contextlib import contextmanager
from itertools import count
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

import kicad_sch_api as ksa
from kicad_sch_api.core.exceptions import LibraryError

HERE = Path(__file__).parent
ENGINE_VERSION = "0.1.0"

Pt = Tuple[float, float]
Vec = Tuple[float, float]
Seg = Tuple[Pt, Pt]
Box = Tuple[float, float, float, float] | None

# --- constants ---------------------------------------------------------------

GRID_MM = 1.27          # 50 mil, the KiCad schematic standard
EPS = 1e-3              # coordinate comparison tolerance, well below one grid step
STUB_MM = 2.54          # straight run every wire takes off its pin before bending
BODY_CLEARANCE_MM = 0.0  # extra air demanded between a wire and a symbol body.
                        # 0.0 means "must not overlap the drawn outline", which
                        # is what a schematic actually requires -- wires may run
                        # right up against a body. Raise it via
                        # layout.body_clearance_mm to force routes further out;
                        # it cannot go negative, because a wire crossing a body
                        # is a real drawing error rather than a tight fit.

# Power rail geometry. A power symbol carries its own visible Value text, so it
# has to stand far enough off its host pin that the two text rows cannot touch.
RAIL_DROP_MM = 5.08     # floor for the wire from a pin to its power symbol
POWER_BODY_MM = 2.54    # how far a stock power symbol's body reaches past its pin
RAIL_TEXT_GAP_MM = 2.54  # clear air demanded between two text rows
FIELD_REACH_MM = 5.08   # furthest a stock symbol parks Reference/Value from its anchor
TEXT_ROW_MM = 1.27      # height of one text row at the default 1.27 mm font
CHAR_W_MM = 0.7 * TEXT_ROW_MM   # stroke-font advance at that size

# Net names that want a ground symbol rather than a supply arrow.
GROUND_NAMES = frozenset({"GND", "GNDA", "GNDD", "AGND", "DGND", "VSS", "EARTH"})

SHEET_SIZES_MM: Dict[str, Tuple[float, float]] = {
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A": (279.4, 215.9),
    "B": (431.8, 279.4),
}

# Direction a wire should leave a pin, in schematic space (+y is DOWN), keyed by
# the pin's rotation in its own symbol. KiCad stores a pin's rotation pointing
# from the connection point *into* the body, so the wire leaves the other way.
_LOCAL_OUTWARD: Dict[int, Vec] = {
    0: (-1.0, 0.0),     # pin on the left edge   -> wire leaves left
    90: (0.0, 1.0),     # pin on the bottom edge -> wire leaves down
    180: (1.0, 0.0),    # pin on the right edge  -> wire leaves right
    270: (0.0, -1.0),   # pin on the top edge    -> wire leaves up
}


# --- 1. grid snapping --------------------------------------------------------


def snap(value: float, grid: float = GRID_MM) -> float:
    """Force a single coordinate onto the grid."""
    return round(round(value / grid) * grid, 4)


def snap_up(value: float, grid: float = GRID_MM) -> float:
    """Snap to the grid, never downwards.

    Used for clearances, where rounding to the nearest grid line could shave the
    gap back below the distance that was just calculated as the safe minimum.
    """
    return round(math.ceil(value / grid - EPS) * grid, 4)


def snap_down(value: float, grid: float = GRID_MM) -> float:
    """Snap to the grid, never upwards."""
    return round(math.floor(value / grid + EPS) * grid, 4)


def snap_away(value: float, sign: float, grid: float = GRID_MM) -> float:
    """Snap outwards along `sign`, so a clearance never rounds back inwards."""
    return snap_up(value, grid) if sign >= 0 else snap_down(value, grid)


def snap_pt(point: Sequence[float], grid: float = GRID_MM) -> Pt:
    """Force an (x, y) pair onto the grid."""
    return (snap(point[0], grid), snap(point[1], grid))


def same(a: float, b: float) -> bool:
    return abs(a - b) < EPS


def same_pt(a: Pt, b: Pt) -> bool:
    return same(a[0], b[0]) and same(a[1], b[1])


# --- pin geometry ------------------------------------------------------------


def rotate_vec(vec: Vec, degrees: float) -> Vec:
    """Rotate a schematic-space vector by a multiple of 90 degrees.

    One step maps (x, y) -> (y, -x). Screen y runs downwards, so that is the
    anticlockwise turn KiCad applies to a symbol's pins when the component
    carries a rotation. Verified against a netlist exported by kicad-cli:
    a Device:R at rotation 90 puts pin 1 to the *left* of its anchor.
    """
    x, y = vec
    for _ in range(int(round((degrees or 0.0) / 90.0)) % 4):
        x, y = y, -x
    return (x, y)


def pin_offset(comp, pin) -> Vec:
    """Pin position relative to the component anchor, in schematic space.

    Deliberately not `Schematic.get_component_pin_position()`: as of
    kicad-sch-api 0.5.5 that helper turns pins the wrong way for rotations of
    90 and 270 degrees, which silently lands wires on the opposite pin of every
    rotated part. `pin.position` is the raw symbol-space offset (y up), so flip
    y and apply the rotation ourselves.
    """
    return rotate_vec((pin.position.x, -pin.position.y), comp.rotation)


def pin_outward(comp, pin) -> Vec:
    """Unit vector pointing away from the symbol body at `pin`."""
    base = _LOCAL_OUTWARD.get(int(round(pin.rotation or 0.0)) % 360)
    if base is not None:
        return rotate_vec(base, comp.rotation)

    # Fallback for a symbol with a non-cardinal pin rotation: aim away from the
    # component anchor along whichever axis dominates.
    dx, dy = pin_offset(comp, pin)
    if abs(dx) >= abs(dy):
        return (1.0 if dx >= 0 else -1.0, 0.0)
    return (0.0, 1.0 if dy >= 0 else -1.0)


def pin_geometry(comp, pin_number: str) -> Tuple[Pt, Vec]:
    """Return the snapped absolute position of a pin and its outward direction."""
    pin = comp.get_pin(str(pin_number))
    if pin is None:
        raise KeyError(f"{comp.reference} has no pin {pin_number}")
    dx, dy = pin_offset(comp, pin)
    return snap_pt((comp.position.x + dx, comp.position.y + dy)), pin_outward(comp, pin)


# --- 3. spatial layout math --------------------------------------------------


_GRAPHIC_HEADS = frozenset({"rectangle", "polyline", "circle", "arc", "bezier"})
_BODY_CACHE: Dict[Tuple[str, float], Box] = {}


def _sexp_head(node) -> str | None:
    return str(node[0]) if node and not isinstance(node[0], list) else None


def _iter_graphics(node):
    """Yield every drawing primitive in a raw symbol definition."""
    if isinstance(node, list):
        if _sexp_head(node) in _GRAPHIC_HEADS:
            yield node
        for child in node:
            if isinstance(child, list):
                yield from _iter_graphics(child)


def _graphic_points(graphic) -> List[Pt]:
    """Every coordinate a primitive occupies, in symbol space."""
    points: List[Pt] = []
    center: Pt | None = None
    radius = 0.0

    def walk(node) -> None:
        nonlocal center, radius
        head = _sexp_head(node)
        if head in ("start", "end", "mid", "xy") and len(node) >= 3 \
                and all(isinstance(v, (int, float)) for v in node[1:3]):
            points.append((float(node[1]), float(node[2])))
        elif head == "center" and len(node) >= 3:
            center = (float(node[1]), float(node[2]))
        elif head == "radius" and len(node) >= 2:
            radius = float(node[1])
        for child in node:
            if isinstance(child, list):
                walk(child)

    walk(graphic)
    if center is not None:
        points += [(center[0] - radius, center[1] - radius),
                   (center[0] + radius, center[1] + radius)]
    return points


def symbol_body_box(comp) -> Box:
    """The drawn body of a symbol as offsets from its anchor, or None.

    `SymbolDefinition.bounding_box` only spans the pins -- it reports Device:R as
    zero width -- so the body has to come off the raw definition. This matters
    twice over: a body can reach well past the pins (the ATtiny's rectangle
    extends 12.7 mm left of an anchor whose pins start at 0), and text has to
    clear the drawn outline, not just the pin field.
    """
    key = (comp.lib_id, float(comp.rotation or 0.0))
    if key in _BODY_CACHE:
        return _BODY_CACHE[key]

    points: List[Pt] = []
    try:
        definition = comp.get_symbol_definition()
    except Exception:
        definition = None
    if definition is not None and definition.raw_kicad_data:
        for graphic in _iter_graphics(definition.raw_kicad_data):
            points += _graphic_points(graphic)

    box: Box = None
    if points:
        turned = [rotate_vec((x, -y), comp.rotation) for x, y in points]
        box = (min(p[0] for p in turned), min(p[1] for p in turned),
               max(p[0] for p in turned), max(p[1] for p in turned))
    _BODY_CACHE[key] = box
    return box


def component_box(comp) -> Tuple[float, float, float, float]:
    """Everything the symbol occupies -- body and pins -- offset from its anchor."""
    xs, ys = [0.0], [0.0]
    for pin in comp.pins:
        dx, dy = pin_offset(comp, pin)
        xs.append(dx)
        ys.append(dy)
    body = symbol_body_box(comp)
    if body is not None:
        xs += [body[0], body[2]]
        ys += [body[1], body[3]]
    return (min(xs), min(ys), max(xs), max(ys))


def component_extents(comp) -> Tuple[float, float, float, float]:
    """Reach of a symbol from its anchor as (left, right, up, down) distances."""
    x0, y0, x1, y1 = component_box(comp)
    return (-x0, x1, -y0, y1)


def minimum_row_pitch(specs: List[dict], extents: Dict[str, tuple], grid: float) -> float:
    """Smallest row pitch that still leaves room to land a rail between parts.

    A power symbol needs its wire, its own body and somewhere to put its value
    text, all in the gap between one part's lower edge and the next part's upper
    edge. Two symbols stacked closer than that leave the router with nowhere to
    put the symbol -- so the pitch is derived from the geometry instead of being
    taken on trust from the caller.
    """
    by_column: Dict[int, List[dict]] = defaultdict(list)
    for spec in specs:
        by_column[int(spec.get("column", 0))].append(spec)

    # A rail landing in the gap costs its wire, its symbol body, and a row of
    # clear air so the part opposite can land one too without touching it.
    allowance = RAIL_DROP_MM + POWER_BODY_MM + TEXT_ROW_MM
    needed = 0.0
    for group in by_column.values():
        ordered = sorted(group, key=lambda s: int(s.get("row", 0)))
        for upper, lower in zip(ordered, ordered[1:]):
            rows_apart = int(lower.get("row", 0)) - int(upper.get("row", 0))
            if rows_apart <= 0:
                continue
            below = extents[upper["ref"]][3]        # how far `upper` reaches down
            above = extents[lower["ref"]][2]        # how far `lower` reaches up
            needed = max(needed, (below + above + allowance) / rows_apart)
    return needed


def layout(sch, comps: Dict[str, object], block: dict, pitch_floor: float = 0.0) -> None:
    """Place every component: strict left-to-right columns, controller centred.

    Columns get adaptive x positions (each column is only as wide as the widest
    symbol in it, plus a clear routing channel). Rows use one uniform pitch
    shared by every column, so row N of column 3 lines up with row N of column
    4. Row 0 is the controller's row, which lands on the sheet centre.
    """
    opts = block.get("layout") or {}
    grid = float(block.get("grid_mm", GRID_MM))
    channel = float(opts.get("channel_mm", 12.7))
    margin = float(opts.get("body_margin_mm", 2.54))

    sheet_w, sheet_h = SHEET_SIZES_MM.get(str(block.get("sheet", "A4")).upper(),
                                          SHEET_SIZES_MM["A4"])
    centre = snap_pt((sheet_w / 2.0, sheet_h / 2.0), grid)

    specs = block["components"]
    extents = {s["ref"]: component_extents(comps[s["ref"]]) for s in specs}

    # --- rows: one uniform pitch, wide enough for the rails between stacked parts ---
    tallest = max(up + down for (_, _, up, down) in extents.values())
    requested = snap(float(opts.get("row_pitch_mm", tallest + channel)), grid)
    # `pitch_floor` is additive, not a competing minimum: it is how much extra a
    # retry has asked for, and must widen whatever the geometry already demands.
    row_pitch = snap_up(max(requested, minimum_row_pitch(specs, extents, grid))
                        + pitch_floor, grid)
    if row_pitch > requested + EPS:
        print(f"    row pitch {requested} mm raised to {row_pitch} mm -- the rails "
              f"between stacked parts need the room")

    controller = next((s for s in specs if s.get("role") == "controller"), specs[0])
    row_zero = int(controller.get("row", 0))

    def row_y(row: int) -> float:
        return snap(centre[1] + (int(row) - row_zero) * row_pitch, grid)

    # --- columns: adaptive widths, laid out strictly left to right ---
    by_column: Dict[int, List[dict]] = defaultdict(list)
    for spec in specs:
        by_column[int(spec.get("column", 0))].append(spec)
    columns = sorted(by_column)

    col_x: Dict[int, float] = {}
    cursor = 0.0
    prev_right = None
    for col in columns:
        left = max(extents[s["ref"]][0] for s in by_column[col]) + margin
        right = max(extents[s["ref"]][1] for s in by_column[col]) + margin
        if prev_right is not None:
            cursor = snap(cursor + prev_right + channel + left, grid)
        col_x[col] = cursor
        prev_right = right

    # Slide the whole row of columns so the controller sits dead centre.
    shift = snap(centre[0] - col_x[int(controller.get("column", 0))], grid)
    for col in columns:
        col_x[col] = snap(col_x[col] + shift, grid)

    for spec in specs:
        target = snap_pt((col_x[int(spec.get("column", 0))],
                          row_y(int(spec.get("row", 0)))), grid)
        comps[spec["ref"]].move(target[0], target[1])
        flag = "  <- controller (sheet centre)" if spec is controller else ""
        print(f"    {spec['ref']:<4} col {spec.get('column', 0):>2} "
              f"row {spec.get('row', 0):>3}  @ ({target[0]:7.2f}, {target[1]:7.2f}){flag}")


# --- 2. orthogonal pathfinding -----------------------------------------------


def simplify(points: Iterable[Pt]) -> List[Pt]:
    """Drop repeated points, then collapse runs of collinear points into one leg."""
    pts: List[Pt] = []
    for p in points:
        if not pts or not same_pt(pts[-1], p):
            pts.append(p)

    out: List[Pt] = []
    for p in pts:
        if len(out) >= 2:
            a, b = out[-2], out[-1]
            straight_x = same(a[0], b[0]) and same(b[0], p[0])
            straight_y = same(a[1], b[1]) and same(b[1], p[1])
            if straight_x or straight_y:
                out[-1] = p
                continue
        out.append(p)
    return out


def _trunk(a: float, da: float, b: float, db: float,
           grid: float, stub: float) -> float | None:
    """Pick a shared coordinate both stubs can legally reach on one axis.

    A wire may continue along the way its pin points, or turn 90 degrees off it,
    but it must never run back over its own stub and into the symbol body. That
    constrains the trunk to `(t - a) * da >= 0` and `(t - b) * db >= 0`.
    Returns None when those two half-lines do not overlap and a detour is needed.
    """
    low, high = float("-inf"), float("inf")
    for coord, direction in ((a, da), (b, db)):
        if direction > 0:
            low = max(low, coord)
        elif direction < 0:
            high = min(high, coord)

    if low > high + EPS:
        return None
    midpoint = (a + b) / 2.0
    if midpoint < low:
        return snap(low + stub, grid)      # clamped: clear the symbol we hit
    if midpoint > high:
        return snap(high - stub, grid)
    return snap(midpoint, grid)


def orthogonal_path(start: Pt, start_dir: Vec, end: Pt, end_dir: Vec,
                    grid: float = GRID_MM, stub: float = STUB_MM,
                    bias: int = 0) -> List[Pt]:
    """Build a strictly axis-aligned path between two pins.

    Each end first takes a short stub in the direction its pin faces, so a wire
    never emerges sideways out of a symbol. The two stub ends are then joined by
    generated intermediate waypoints, chosen so that no leg doubles back over a
    stub:

      * stubs on the same axis -> two corners through a shared trunk line, or a
        four-corner detour when the two pins face away from each other;
      * stubs on different axes -> one corner, taking the leg that runs with the
        pin direction when that is legal and the mirrored corner when it is not.
    """
    a = snap_pt((start[0] + start_dir[0] * stub, start[1] + start_dir[1] * stub), grid)
    b = snap_pt((end[0] + end_dir[0] * stub, end[1] + end_dir[1] * stub), grid)

    a_horizontal = abs(start_dir[0]) > abs(start_dir[1])
    b_horizontal = abs(end_dir[0]) > abs(end_dir[1])

    middle: List[Pt]
    if bias:
        # Forced detour, for when the default route runs through something.
        offset = bias * grid
        if a_horizontal == b_horizontal:
            # Two pins facing the same way on one line get a straight wire by
            # default, which happily passes through whatever sits between them
            # -- including the other pin of their own resistor. Swing the run
            # out to a parallel line so it goes around instead.
            if a_horizontal:
                lane = snap(a[1] + offset, grid)
                middle = [(a[0], lane), (b[0], lane)]
            else:
                lane = snap(a[0] + offset, grid)
                middle = [(lane, a[1]), (lane, b[1])]
        elif a_horizontal:
            # One corner each side: slide the turn along, so the long leg runs
            # down a clear channel rather than through the parts in column.
            lane = snap(a[0] + offset, grid)
            middle = [(lane, a[1]), (lane, b[1])]
        else:
            lane = snap(a[1] + offset, grid)
            middle = [(a[0], lane), (b[0], lane)]
    elif a_horizontal and b_horizontal:
        if same(a[1], b[1]):
            middle = []                                    # already collinear
        else:
            tx = _trunk(a[0], start_dir[0], b[0], end_dir[0], grid, stub)
            if tx is not None:
                middle = [(tx, a[1]), (tx, b[1])]
            else:                                          # pins face apart: detour
                ax = snap(a[0] + start_dir[0] * stub, grid)
                bx = snap(b[0] + end_dir[0] * stub, grid)
                ty = snap((a[1] + b[1]) / 2.0, grid)
                middle = [(ax, a[1]), (ax, ty), (bx, ty), (bx, b[1])]
    elif not a_horizontal and not b_horizontal:
        if same(a[0], b[0]):
            middle = []
        else:
            ty = _trunk(a[1], start_dir[1], b[1], end_dir[1], grid, stub)
            if ty is not None:
                middle = [(a[0], ty), (b[0], ty)]
            else:
                ay = snap(a[1] + start_dir[1] * stub, grid)
                by = snap(b[1] + end_dir[1] * stub, grid)
                tx = snap((a[0] + b[0]) / 2.0, grid)
                middle = [(a[0], ay), (tx, ay), (tx, by), (b[0], by)]
    else:
        # One stub is horizontal and the other vertical, so a single corner does
        # it. Running out along the horizontal pin first is the tidier corner,
        # but only when it neither backtracks nor lands on top of the other stub.
        if a_horizontal:
            run_out, mirrored = (b[0], a[1]), (a[0], b[1])
            along = (b[0] - a[0]) * start_dir[0] >= -EPS
            clear = (a[1] - b[1]) * end_dir[1] >= -EPS
        else:
            run_out, mirrored = (a[0], b[1]), (b[0], a[1])
            along = (b[1] - a[1]) * start_dir[1] >= -EPS
            clear = (a[0] - b[0]) * end_dir[0] >= -EPS
        middle = [run_out if (along and clear) else mirrored]

    path = simplify([start, a, *middle, b, end])

    for p, q in zip(path, path[1:]):
        if not (same(p[0], q[0]) or same(p[1], q[1])):
            raise AssertionError(f"non-orthogonal leg {p} -> {q}")
    return path


def merge_collinear(segments: Iterable[Seg]) -> List[Seg]:
    """Fuse collinear legs that overlap or touch into single spans.

    Chained routes share stubs, and a shared stub otherwise ships as a second
    wire lying on top of the first. Merging by axis line also keeps the file
    down to one wire object per straight run.
    """
    lines: Dict[Tuple[str, float], List[List[float]]] = defaultdict(list)
    for (x1, y1), (x2, y2) in segments:
        if same(x1, x2):
            lines[("v", round(x1, 3))].append([min(y1, y2), max(y1, y2)])
        elif same(y1, y2):
            lines[("h", round(y1, 3))].append([min(x1, x2), max(x1, x2)])

    merged: List[Seg] = []
    for (axis, fixed), spans in sorted(lines.items()):
        spans.sort()
        runs: List[List[float]] = []
        for start, end in spans:
            if runs and start <= runs[-1][1] + EPS:
                runs[-1][1] = max(runs[-1][1], end)
            else:
                runs.append([start, end])
        for start, end in runs:
            if end - start < EPS:
                continue                                   # zero-length leg
            merged.append(((fixed, start), (fixed, end)) if axis == "v"
                          else ((start, fixed), (end, fixed)))
    return merged


def _branches_at(seg: Seg, pt: Pt) -> int:
    """How many wire ends a segment contributes at `pt`: 2 mid-span, 1 at a tip."""
    (x1, y1), (x2, y2) = seg
    if same(x1, x2):
        if not same(pt[0], x1):
            return 0
        low, high = min(y1, y2), max(y1, y2)
        coord = pt[1]
    elif same(y1, y2):
        if not same(pt[1], y1):
            return 0
        low, high = min(x1, x2), max(x1, x2)
        coord = pt[0]
    else:
        return 0

    if same(coord, low) or same(coord, high):
        return 1
    return 2 if low < coord < high else 0


def junction_points(segments: Sequence[Seg]) -> List[Pt]:
    """Points where three or more wire ends meet and KiCad needs a solder dot.

    Counting ends rather than segments is what makes this survive
    `merge_collinear`: a leg that tees off the middle of a merged run sees two
    ends from the run it lands on plus its own, so the T is still caught, while
    a plain corner only ever reaches two.
    """
    dots: List[Pt] = []
    for candidate in sorted({p for seg in segments for p in seg}):
        if sum(_branches_at(seg, candidate) for seg in segments) >= 3:
            dots.append(candidate)
    return dots


# --- net routing -------------------------------------------------------------


def route_signal_net(sch, comps, net: dict, grid: float,
                     net_segments: Dict[str, List[Seg]],
                     net_labels: Dict[str, Pt],
                     clearance: float = BODY_CLEARANCE_MM) -> Tuple[int, int]:
    """Wire a signal net with orthogonal legs, left to right, plus junctions."""
    clearance = max(0.0, float(clearance))
    name = net["name"]
    pins = []
    for conn in net["connections"]:
        comp = comps.get(conn["ref"])
        if comp is None:
            print(f"    ! {name}: unknown component {conn['ref']}, skipping")
            continue
        pins.append((conn, *pin_geometry(comp, conn["pin"])))

    if len(pins) < 2:
        return (0, 0)

    # Chain the pins in left-to-right order so legs follow the signal flow
    # instead of doubling back across the sheet.
    pins.sort(key=lambda item: (item[1][0], item[1][1]))

    # What this net must not touch: any pin that is not its own, any copper
    # already laid for another net, and any symbol's drawn body.
    own = {(c["ref"], str(c["pin"])) for c in net["connections"]}
    strangers = [pin_geometry(comp, pin.number)[0]
                 for ref, comp in comps.items() for pin in comp.pins
                 if (ref, str(pin.number)) not in own]
    foreign = [seg for other, segs in net_segments.items() if other != name
               for seg in segs]
    # Bodies are carried with their owner's reference, because a wire has to be
    # allowed to leave the one symbol it is legitimately attached to.
    bodies = [(comp.reference, box)
              for comp, box in ((c, _absolute_body(c)) for c in sch.components)
              if box is not None]
    # Where each symbol's own pins sit, so `obstruction` can tell "this leg
    # starts on that symbol" from "this leg cuts across it".
    pin_points: Dict[str, List[Pt]] = {}
    for ref, comp in comps.items():
        pin_points[ref] = [pin_geometry(comp, pin.number)[0] for pin in comp.pins]
    # A label names whatever wire it sits on, so running through another net's
    # label silently merges the two nets.
    foreign_labels = [pt for other, pt in net_labels.items() if other != name]

    def _leaves_own_symbol(leg: Seg, ref: str) -> bool:
        """True if `leg` ends on a pin of `ref`, so `ref`'s body cannot block it.

        Some stock symbols draw graphics that reach past their own pins --
        Device:LED's emission arrows overhang pin 1 by 0.762 mm -- so the body
        box contains the very pin a wire has to land on. Without this, the
        first stub off such a pin always overlaps the body and the net is
        unroutable at any spacing. Only the leg touching the pin is excused,
        and only from that one symbol's body, so a leg cutting across a
        neighbour is still rejected. A leg landing on a pin that is not ours is
        already caught by the `strangers` test above, so this cannot be used to
        tunnel into someone else's part.
        """
        return any(same_pt(end, pt) for end in leg for pt in pin_points.get(ref, ()))

    def obstruction(legs: Sequence[Seg]) -> str | None:
        for leg in legs:
            for point in strangers:
                if _branches_at(leg, point):
                    return f"pin at {point}"
            for point in foreign_labels:
                if _branches_at(leg, point):
                    return f"another net's label at {point}"
            for ref, box in bodies:
                if not _boxes_overlap(_segment_box(leg, pad=clearance), box):
                    continue
                if _leaves_own_symbol(leg, ref):
                    continue
                return f"symbol body {box}"
        return copper_contact(legs, foreign)

    raw: List[Seg] = []
    for (_, p1, d1), (_, p2, d2) in zip(pins, pins[1:]):
        # Straight through first, then swing progressively wider around whatever
        # is in the way. Biases alternate sides so the detour stays short.
        chosen, fallback, blocked_by = None, None, None
        for bias in [0] + [s * k for k in range(2, 9) for s in (1, -1)]:
            path = orthogonal_path(p1, d1, p2, d2, grid=grid, bias=bias)
            legs = list(zip(path, path[1:]))
            if fallback is None:
                fallback = legs
            hit = obstruction(legs)
            if hit is None:
                chosen = legs
                break
            if blocked_by is None:
                blocked_by = hit
        if chosen is None:
            raise AssertionError(
                f"{name}: no route between {p1} and {p2} clears the obstacles "
                f"(nearest blocker: {blocked_by}). Give the parts more room, or "
                f"rotate them so their pins face the signal flow.")
        raw.extend(chosen)

    segments = merge_collinear(raw)
    net_segments.setdefault(name, []).extend(segments)
    for start, end in segments:
        sch.add_wire(start, end)

    dots = junction_points(segments)
    for dot in dots:
        sch.junctions.add(dot)

    # Name the net somewhere on its own copper that no other net passes through:
    # a shared point would hand both nets the same name.
    _, first_pt, first_dir = pins[0]
    spots = [snap_pt((first_pt[0] + first_dir[0] * STUB_MM,
                      first_pt[1] + first_dir[1] * STUB_MM), grid)]
    spots += [snap_pt(((s0[0] + e0[0]) / 2, (s0[1] + e0[1]) / 2), grid)
              for s0, e0 in segments]
    spot = next(
        (c for c in spots
         if any(_branches_at(seg, c) for seg in segments)
         and not any(_branches_at(seg, c) for seg in foreign)
         and not any(same_pt(c, other) for other in foreign_labels)),
        spots[0])
    sch.add_label(name, position=spot)
    net_labels[name] = spot

    print(f"    {name:<10} {len(pins)} pins -> {len(segments)} legs "
          f"({len(raw)} before merge), {len(dots)} junctions")
    return (len(segments), len(dots))


def _span(seg: Seg):
    """('v', x, lo, hi) or ('h', y, lo, hi) for an axis-aligned segment."""
    (x1, y1), (x2, y2) = seg
    if same(x1, x2):
        return ("v", round(x1, 3), min(y1, y2), max(y1, y2))
    if same(y1, y2):
        return ("h", round(y1, 3), min(x1, x2), max(x1, x2))
    return None


def copper_contact(mine: Sequence[Seg], theirs: Sequence[Seg]) -> str | None:
    """Describe the first electrical contact between two nets' copper, if any.

    Two things join nets in KiCad: collinear wires that overlap, and a wire
    endpoint that lands anywhere on another wire. A plain crossing does *not* --
    unless a junction sits on it -- which is why crossings are tolerated here and
    only these two cases are treated as faults.
    """
    for a in mine:
        span_a = _span(a)
        for b in theirs:
            span_b = _span(b)
            if span_a and span_b and span_a[0] == span_b[0] and same(span_a[1], span_b[1]):
                if min(span_a[3], span_b[3]) - max(span_a[2], span_b[2]) > EPS:
                    return f"collinear overlap {a} / {b}"
            for point in a:
                if _branches_at(b, point):
                    return f"endpoint {point} on {b}"
            for point in b:
                if _branches_at(a, point):
                    return f"endpoint {point} on {a}"
    return None


def _crossings(mine: Sequence[Seg], theirs: Sequence[Seg]) -> int:
    """Count places the two nets cross without touching -- legal, but untidy."""
    total = 0
    for a in mine:
        span_a = _span(a)
        for b in theirs:
            span_b = _span(b)
            if not span_a or not span_b or span_a[0] == span_b[0]:
                continue
            vert, horiz = (span_a, span_b) if span_a[0] == "v" else (span_b, span_a)
            if (horiz[2] - EPS < vert[1] < horiz[3] + EPS
                    and vert[2] - EPS < horiz[1] < vert[3] + EPS):
                total += 1
    return total


@contextmanager
def _quiet_symbol_lookup():
    """Mute the library cache while we knowingly probe for a missing symbol.

    `kicad_sch_api.library.cache` logs a warning whenever a lookup misses. A
    miss here is an expected answer, not a fault, and letting it through prints
    an alarming "Symbol VIN not found" line on every request that uses a rail
    with no stock symbol.
    """
    logger = logging.getLogger("kicad_sch_api")
    previous = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(previous)


_POWER_SYMBOL_CACHE: Dict[str, str] = {}


def resolve_power_symbol(sch, net: dict) -> str:
    """Pick the library symbol that carries this rail.

    KiCad takes a power symbol's *net name from its Value field*, not from the
    symbol name, so an off-library rail such as VIN rides a stock supply arrow
    with its value overridden and still nets up as VIN.
    """
    name = net["name"]
    key = f"{net.get('power_symbol') or ''}|{name}"
    if key in _POWER_SYMBOL_CACHE:
        return _POWER_SYMBOL_CACHE[key]

    probe = f"#PROBE{id(net) % 1000}"
    chosen = "power:GND" if name.upper() in GROUND_NAMES else "power:VCC"
    for candidate in (net.get("power_symbol"), f"power:{name}"):
        if not candidate:
            continue
        try:
            with _quiet_symbol_lookup():
                sch.components.add(lib_id=candidate, reference=probe, value=name,
                                   position=(GRID_MM, GRID_MM))
        except LibraryError:
            continue
        sch.components.remove(probe)
        chosen = candidate
        break

    _POWER_SYMBOL_CACHE[key] = chosen
    return chosen


def rail_clearance(host, pin_pos: Pt, rail_dir: Vec, floor: float, grid: float) -> float:
    """How far a power symbol must stand off its host pin to keep text apart.

    The host parks Reference and Value near its own anchor, so the distance that
    matters is measured from the *host anchor*, not from the pin: a pin sunk deep
    inside a tall symbol needs a shorter wire than a pin flush with the anchor.
    Stock symbols keep those fields within max(pin extent, FIELD_REACH_MM) of the
    anchor, so clearing that reach plus a text row and a gap clears the text.
    """
    left, right, up, down = component_extents(host)
    axis = 1 if abs(rail_dir[1]) > abs(rail_dir[0]) else 0
    if axis == 1:
        extent = down if rail_dir[1] > 0 else up
    else:
        extent = right if rail_dir[0] > 0 else left

    reach = max(extent, FIELD_REACH_MM) + TEXT_ROW_MM / 2.0 + RAIL_TEXT_GAP_MM
    anchor = (host.position.x, host.position.y)[axis]
    pin_depth = (pin_pos[axis] - anchor) * rail_dir[axis]   # pin's head start
    return snap_up(max(floor, reach - pin_depth), grid)


def _absolute_box(comp) -> Tuple[float, float, float, float]:
    """The symbol's occupied box in sheet coordinates."""
    x0, y0, x1, y1 = component_box(comp)
    return (comp.position.x + x0, comp.position.y + y0,
            comp.position.x + x1, comp.position.y + y1)


def _absolute_body(comp) -> Box:
    """Just the drawn outline in sheet coordinates, pins excluded.

    Wires legitimately start on pins, so obstacle tests use the body alone --
    testing against the pin-inclusive box would reject every route at its own
    first leg.
    """
    box = symbol_body_box(comp)
    if box is None:
        return None
    return (comp.position.x + box[0], comp.position.y + box[1],
            comp.position.x + box[2], comp.position.y + box[3])


def _rail_offsets(pin_pos: Pt, pin_dir: Vec, rail_dir: Vec, host, drop: float,
                  grid: float):
    """Yield (lateral, reach) placements for a power symbol, tidiest first.

    Three situations, and all three need a search rather than a single answer:
    a pin facing sideways can only leave along itself, so only the length of
    that run varies; a pin already facing its rail can go straight out *or* jog
    to either side; a pin facing away from its rail has to step aside far enough
    to come back down the outside of its own symbol.

    Sideways jogs are tried before longer rails, because moving into the next
    column costs less readability than a wire that runs halfway up the sheet.
    """
    perp: Vec = (-rail_dir[1], rail_dir[0])
    along = pin_dir[0] * rail_dir[0] + pin_dir[1] * rail_dir[1]

    if abs(along) <= EPS:                       # pin faces across the rail
        laterals = [(pin_dir[0] * m, pin_dir[1] * m)
                    for m in (STUB_MM + k * grid for k in range(8))]
    elif along > 0:                             # pin already faces the rail
        laterals = [(0.0, 0.0)]
        for k in range(6):
            m = STUB_MM + k * grid
            laterals.append((perp[0] * m, perp[1] * m))
            laterals.append((-perp[0] * m, -perp[1] * m))
    else:                                       # pin faces away from the rail
        # Leaving the symbol's own side first is tidier, but it can be boxed in
        # by a neighbouring pin's rail, so the far side has to be on offer too.
        away = 1.0 if pin_pos[0] >= host.position.x else -1.0
        laterals = []
        for k in range(8):
            m = 2 * STUB_MM + k * grid
            laterals.append((perp[0] * away * m, perp[1] * away * m))
            laterals.append((-perp[0] * away * m, -perp[1] * away * m))

    for extra in (k * STUB_MM for k in range(4)):
        for lateral in laterals:
            yield lateral, drop + extra


def place_power_rails(sch, comps, net: dict, grid: float, opts: dict, seq,
                      rails: List[dict],
                      net_segments: Dict[str, List[Seg]]) -> Tuple[int, int]:
    """Terminate every pin on a rail in its own power symbol.

    Rails are not routed as copper across the sheet -- that is what turns an
    eight-pin GND into spaghetti. Each pin instead gets a short orthogonal wire
    to a standard power symbol, which is what carries the connection.
    """
    name = net["name"]
    lib_id = resolve_power_symbol(sch, net)
    floor = float(opts.get("rail_drop_mm", RAIL_DROP_MM))
    placed, legs = 0, 0

    for conn in net["connections"]:
        host = comps.get(conn["ref"])
        if host is None:
            print(f"    ! {name}: unknown component {conn['ref']}, skipping")
            continue
        pin_pos, pin_dir = pin_geometry(host, conn["pin"])

        # Build the symbol first: its own pin direction says which way the rail
        # runs. A ground pin faces up, so the symbol hangs below the wire end; a
        # supply pin faces down, so it sits above. Never rotate it to suit the
        # host -- an upside-down GND is exactly the kind of thing that reads as
        # a bug in the finished sheet.
        reference = f"#PWR{next(seq):03d}"
        symbol = sch.components.add(lib_id=lib_id, reference=reference, value=name,
                                    position=pin_pos)
        # (hide yes) on the reference. This must be `hidden_properties`, NOT
        # `set_property_effects`: that call writes a preserved s-expression which
        # bypasses the hide flag and *unhides* what the library hid by default.
        symbol.hidden_properties.add("Reference")

        _, rail_out = pin_geometry(symbol, "1")
        rail_dir = (-rail_out[0], -rail_out[1])

        drop = rail_clearance(host, pin_pos, rail_dir, floor, grid)

        # Adjacent pins on one part -- a two-way connector, an IC's power pair --
        # would otherwise stub out to the same lateral column and run their rails
        # straight through each other. Search offsets until this rail makes no
        # electrical contact with copper already laid down for other nets, and
        # lands its symbol somewhere nothing else occupies.
        foreign = [seg for other, segs in net_segments.items() if other != name
                   for seg in segs]
        occupied = [_absolute_box(other) for other in sch.components
                    if other.uuid != symbol.uuid]

        best = None
        blockers: Dict[str, int] = defaultdict(int)
        for lateral, reach in _rail_offsets(pin_pos, pin_dir, rail_dir, host, drop, grid):
            trial = snap_pt((pin_pos[0] + lateral[0] + rail_dir[0] * reach,
                             pin_pos[1] + lateral[1] + rail_dir[1] * reach), grid)
            symbol.move(trial[0], trial[1])

            clash = next((other.reference for other in sch.components
                          if other.uuid != symbol.uuid
                          and _boxes_overlap(_absolute_box(symbol),
                                             _absolute_box(other))), None)
            if clash is not None:
                blockers[f"{clash} occupies the space"] += 1
                continue

            route = orthogonal_path(pin_pos, pin_dir, trial, rail_out, grid=grid)
            legs_here = merge_collinear(zip(route, route[1:]))
            touch = copper_contact(legs_here, foreign)
            if touch is not None:
                owner = next((other for other, segs in net_segments.items()
                              if other != name
                              and copper_contact(legs_here, segs) is not None), "another net")
                blockers[f"would short to {owner}"] += 1
                continue

            crossings = _crossings(legs_here, foreign)
            if best is None or crossings < best[0]:
                best = (crossings, trial, legs_here)
            if crossings == 0:
                break                                # tidiest option available
        if best is None:
            sch.components.remove(symbol.reference)
            why = "; ".join(f"{reason} ({hits}x)" for reason, hits
                            in sorted(blockers.items(), key=lambda kv: -kv[1])[:3])
            raise RailPlacementError(
                f"{name}: nowhere to put the power symbol for "
                f"{host.reference}.{conn['pin']} -- {why or 'no candidate fitted'}. "
                f"The parts around it are packed too tightly; raise "
                f"layout.row_pitch_mm or layout.channel_mm.")

        crossings, anchor, legs_here = best
        symbol.move(anchor[0], anchor[1])
        for start, end in legs_here:
            sch.add_wire(start, end)
            net_segments.setdefault(name, []).append((start, end))
            legs += 1
        placed += 1

        axis = 1 if abs(rail_dir[1]) > abs(rail_dir[0]) else 0
        rails.append({
            "net": name, "symbol": reference, "host": host.reference,
            "axis": axis, "required": drop, "anchor": anchor,
            "actual": (anchor[axis] - pin_pos[axis]) * rail_dir[axis],
        })

    print(f"    {name:<10} {placed} pins -> {placed} x {lib_id}, {legs} legs, no rail copper")
    return (placed, legs)


# --- 6. ERC furniture: power flags and no-connects ----------------------------


def add_power_flags(sch, comps, block: dict, rails: List[dict], grid: float,
                    net_segments: Dict[str, List[Seg]], seq) -> List[str]:
    """Drive rails that no power-output pin feeds.

    ERC treats every power symbol as a power *input*, so a rail fed only through
    a passive connector or an IC's supply pin reads as undriven -- the classic
    "Input Power pin not driven by any Output Power pins". A PWR_FLAG is the
    standard answer: it declares the net as sourced. Only rails that genuinely
    lack a driver get one, so a regulator output is left to speak for itself.
    """
    flagged = []
    for net in block["nets"]:
        if str(net.get("class", "signal")).lower() != "power":
            continue
        name = net["name"]
        driven = False
        for conn in net["connections"]:
            host = comps.get(conn["ref"])
            pin = host.get_pin(str(conn["pin"])) if host else None
            if pin is not None and str(pin.pin_type).endswith("POWER_OUT"):
                driven = True
                break
        if driven:
            continue

        anchor = next((r["anchor"] for r in rails if r["net"] == name), None)
        if anchor is None:
            continue

        # Sit the flag beside the rail's power symbol and wire across to it, so
        # the two pins join through copper rather than by overlapping.
        foreign = [seg for other, segs in net_segments.items() if other != name
                   for seg in segs]
        bodies = [box for box in (_absolute_body(c) for c in sch.components)
                  if box is not None]

        flag = sch.components.add(lib_id="power:PWR_FLAG", value="PWR_FLAG",
                                  reference=f"#FLG{next(seq):03d}", position=anchor)
        flag.hidden_properties.add("Reference")

        spot = None
        for direction in ((1.0, 0.0), (-1.0, 0.0), (0.0, -1.0), (0.0, 1.0)):
            for step in range(6):
                reach = 2 * STUB_MM + step * grid
                trial = snap_pt((anchor[0] + direction[0] * reach,
                                 anchor[1] + direction[1] * reach), grid)
                flag.move(trial[0], trial[1])
                if any(_boxes_overlap(_absolute_box(flag), _absolute_box(other))
                       for other in sch.components if other.uuid != flag.uuid):
                    continue
                leg = [(anchor, trial)]
                if copper_contact(leg, foreign) is not None:
                    continue
                if any(_boxes_overlap(_segment_box(leg[0], pad=0.0), box)
                       for box in bodies):
                    continue
                spot = trial
                break
            if spot is not None:
                break
        if spot is None:
            # Silently skipping would leave the rail undriven and hand the
            # caller an ERC error with no explanation of where it came from.
            sch.components.remove(flag.reference)
            raise RailPlacementError(
                f"{name}: nowhere to attach a PWR_FLAG next to its power symbol "
                f"at {anchor}; the area is too congested")

        sch.add_wire(anchor, spot)
        net_segments.setdefault(name, []).append((anchor, spot))
        flagged.append(name)
    return flagged


def add_no_connects(sch, comps, block: dict) -> List[str]:
    """Flag every pin the netlist never mentions, so ERC stops guessing.

    An unused pin is a design decision, not an oversight, but only a no-connect
    marker says so in a way KiCad believes.
    """
    used = {(c["ref"], str(c["pin"])) for net in block["nets"]
            for c in net["connections"]}
    marked = []
    for ref, comp in sorted(comps.items()):
        for pin in comp.pins:
            if (ref, str(pin.number)) in used:
                continue
            pos, _ = pin_geometry(comp, pin.number)
            sch.no_connects.add(pos)
            marked.append(f"{ref}.{pin.number}")
    return marked


# --- verification ------------------------------------------------------------


def detect_shorts(comps, block: dict, net_segments: Dict[str, List[Seg]]) -> List[str]:
    """Find pins a net's copper touches without the netlist saying it should.

    A wire that merely grazes a neighbouring pin is a short in KiCad -- there is
    no clearance rule to save you. Adjacent pins on a connector or an IC sit one
    grid step apart, so a rail or a bus that runs down their edge silently ties
    them together. Every leg is therefore checked against every pin in the
    design, not just the pins of the net that drew it.
    """
    owner = {(c["ref"], str(c["pin"])): net["name"]
             for net in block["nets"] for c in net["connections"]}

    shorts = []
    for net_name, segments in net_segments.items():
        for ref, comp in comps.items():
            for pin in comp.pins:
                if owner.get((ref, str(pin.number))) == net_name:
                    continue
                pos, _ = pin_geometry(comp, pin.number)
                if any(_branches_at(seg, pos) for seg in segments):
                    other = owner.get((ref, str(pin.number)), "no net")
                    shorts.append(f"{net_name} copper touches {ref}.{pin.number} ({other})")
    return shorts


def verify(sch, comps, block: dict, grid: float, rails: List[dict],
           net_segments: Dict[str, List[Seg]], text_boxes: List[tuple],
           net_labels: Dict[str, Pt]) -> None:
    """Fail loudly on a bad grid, a diagonal leg, or an unconnected pin."""
    off_grid = []
    for comp in sch.components:
        p = (comp.position.x, comp.position.y)
        if not same_pt(p, snap_pt(p, grid)):
            off_grid.append(f"{comp.reference} @ {p}")

    diagonal = []
    segments: List[Seg] = []
    for wire in sch.wires:
        pts = [(pt.x, pt.y) for pt in wire.points]
        for a, b in zip(pts, pts[1:]):
            if not same_pt(a, snap_pt(a, grid)) or not same_pt(b, snap_pt(b, grid)):
                off_grid.append(f"wire {a} -> {b}")
            if not (same(a[0], b[0]) or same(a[1], b[1])):
                diagonal.append(f"{a} -> {b}")
            segments.append((a, b))

    # Every pin must actually be reached by a wire segment.
    # Power stubs and signal nets are now both checked here.
    unconnected = []
    for net in block["nets"]:
        for conn in net["connections"]:
            comp = comps.get(conn["ref"])
            if comp is None:
                continue
            pos, _ = pin_geometry(comp, conn["pin"])
            
            # Check if any physical wire touches this exact pin coordinate
            reached = any(_branches_at(seg, pos) for seg in segments)
            
            if not reached:
                unconnected.append(f"{net['name']}:{conn['ref']}.{conn['pin']}")

    # Power symbols: reference suppressed, and standing far enough off the host
    # pin that the two text rows cannot meet.
    unhidden = [c.reference for c in sch.components
                if c.lib_id.startswith("power:") and "Reference" not in c.hidden_properties]
    crowded = [f"{r['symbol']}@{r['host']} {r['actual']:.2f} < {r['required']:.2f}"
               for r in rails if r["actual"] < r["required"] - EPS]

    if off_grid:
        raise AssertionError(f"{len(off_grid)} off-grid items: {off_grid[:5]}")
    if diagonal:
        raise AssertionError(f"{len(diagonal)} diagonal wires: {diagonal[:5]}")
    if unconnected:
        raise AssertionError(f"{len(unconnected)} unconnected pins: {unconnected[:5]}")
    if unhidden:
        raise AssertionError(f"{len(unhidden)} power refs still visible: {unhidden[:5]}")
    if crowded:
        raise AssertionError(f"{len(crowded)} power symbols too close: {crowded[:5]}")

    shorts = detect_shorts(comps, block, net_segments)
    if shorts:
        raise AssertionError(f"{len(shorts)} shorted pins: {shorts[:5]}")

    names = sorted(net_segments)
    contacts, crossings = [], 0
    for i, first in enumerate(names):
        for second in names[i + 1:]:
            touch = copper_contact(net_segments[first], net_segments[second])
            if touch:
                contacts.append(f"{first} / {second}: {touch}")
            crossings += _crossings(net_segments[first], net_segments[second])
    if contacts:
        raise AssertionError(f"{len(contacts)} nets in contact: {contacts[:5]}")

    # A net label names every wire beneath it, so one sitting on foreign copper
    # merges the two nets -- a short KiCad reports but pin-and-copper checks miss.
    stolen = [f"{owner}'s label at {spot} sits on {other}'s wire"
              for owner, spot in net_labels.items()
              for other, segs in net_segments.items()
              if other != owner and any(_branches_at(seg, spot) for seg in segs)]
    if stolen:
        raise AssertionError(f"{len(stolen)} labels on foreign copper: {stolen[:5]}")

    # Field text must sit clear of every drawn line and every symbol outline.
    bodies = []
    for comp in sch.components:
        bx0, by0, bx1, by1 = component_box(comp)
        bodies.append((f"body {comp.reference}",
                       (comp.position.x + bx0, comp.position.y + by0,
                        comp.position.x + bx1, comp.position.y + by1)))
    lines = [(f"wire {a}-{b}", _segment_box((a, b))) for a, b in segments]
    fouled = [f"{name} over {what}" for name, box in text_boxes
              for what, other in bodies + lines if _boxes_overlap(box, other)]
    if fouled:
        raise AssertionError(f"{len(fouled)} text fields over geometry: {fouled[:5]}")

    pin_count = sum(len(n["connections"]) for n in block["nets"])
    print(f"  verified: {len(list(sch.components))} components, {len(segments)} wire legs "
          f"-- all on the {grid} mm grid, all orthogonal, {pin_count}/{pin_count} pins connected")
    print(f"  verified: {len(rails)} power symbols, all references hidden, "
          f"all clear of host text by >= {RAIL_TEXT_GAP_MM} mm")
    pins_checked = sum(len(c.pins) for c in comps.values()) * len(net_segments)
    print(f"  verified: no shorts -- {len(net_segments)} nets' copper checked against "
          f"every pin ({pins_checked} pin/net pairs)")
    print(f"  verified: no cross-net contact across {len(names) * (len(names) - 1) // 2} "
          f"net pairs ({crossings} legal crossings, 0 overlaps, 0 tee-ins)")
    print(f"  verified: {len(text_boxes)} field texts clear of {len(bodies)} symbol bodies "
          f"and {len(lines)} wire legs")
    print(f"  verified: {len(net_labels)} net labels, none sitting on another net's copper")


# --- 5. property text placement ----------------------------------------------

TEXT_CLEAR_MM = 1.27        # air between a text row and whatever it stands off


def text_box_at(text: str, centre: Pt) -> Tuple[float, float, float, float]:
    """Box a centred text row occupies. Mirrors what audit_rendered_text checks."""
    width = max(len(text), 1) * CHAR_W_MM
    half_h = TEXT_ROW_MM / 2.0
    return (centre[0] - width / 2.0, centre[1] - half_h,
            centre[0] + width / 2.0, centre[1] + half_h)


def _boxes_overlap(a, b) -> bool:
    return a[0] < b[2] - EPS and b[0] < a[2] - EPS and a[1] < b[3] - EPS and b[1] < a[3] - EPS


def _segment_box(seg: Seg, pad: float = 0.3) -> Tuple[float, float, float, float]:
    (x1, y1), (x2, y2) = seg
    return (min(x1, x2) - pad, min(y1, y2) - pad, max(x1, x2) + pad, max(y1, y2) + pad)


def _text_candidates(box, width: float, prefer: str, grid: float):
    """Positions just outside `box`, preferred side first, then further out."""
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    gap = TEXT_CLEAR_MM + TEXT_ROW_MM / 2.0
    order = [prefer] + [d for d in ("up", "down", "right", "left") if d != prefer]

    # Snap outwards, never towards the symbol: rounding to the nearest grid line
    # would give back up to half a step of the clearance just reserved.
    for step in range(6):
        extra = step * STUB_MM
        for side in order:
            if side == "up":
                yield (snap(cx, grid), snap_away(y0 - gap - extra, -1, grid))
            elif side == "down":
                yield (snap(cx, grid), snap_away(y1 + gap + extra, +1, grid))
            elif side == "right":
                yield (snap_away(x1 + gap + extra + width / 2.0, +1, grid), snap(cy, grid))
            else:
                yield (snap_away(x0 - gap - extra - width / 2.0, -1, grid), snap(cy, grid))


def place_property_text(sch, grid: float) -> Tuple[int, int, List[tuple]]:
    """Put every visible Reference and Value upright and in clear space.

    Two things force this pass. KiCad inherits a symbol's field angle, so a part
    placed at 180 degrees gets its text written at 180 too, and a Device:R parks
    its Value at the anchor -- dead on top of the resistor body. Both are fixed
    the same way: pin every field to rotation 0 and search outwards from the
    symbol for the first slot that hits nothing.

    Safe to freeze absolute positions here, and only here: this runs after
    layout() and after routing, so nothing moves underneath it afterwards.
    """
    obstacles = [(_segment_box(((p.x, p.y), (q.x, q.y))))
                 for wire in sch.wires
                 for p, q in zip(wire.points, wire.points[1:])]
    for label in sch.labels:
        obstacles.append(text_box_at(label.text, (label.position.x, label.position.y)))

    parts = sorted(sch.components, key=lambda c: c.reference)
    for comp in parts:
        x0, y0, x1, y1 = component_box(comp)
        obstacles.append((comp.position.x + x0, comp.position.y + y0,
                          comp.position.x + x1, comp.position.y + y1))

    placed, relocated, chosen_boxes = 0, 0, []
    for comp in parts:
        power = comp.lib_id.startswith("power:")
        body = symbol_body_box(comp)
        x0, y0, x1, y1 = component_box(comp)
        box = (comp.position.x + x0, comp.position.y + y0,
               comp.position.x + x1, comp.position.y + y1)

        # A power symbol shows only its Value, and it belongs on the far side of
        # the body from the pin -- below a ground, above a supply.
        if power:
            fields = [("Value", "down" if body and body[3] > EPS else "up")]
        else:
            fields = [("Reference", "up"), ("Value", "down")]

        for field, prefer in fields:
            text = comp.reference if field == "Reference" else comp.value
            if not text:
                continue
            width = max(len(text), 1) * CHAR_W_MM
            chosen, first = None, None
            for candidate in _text_candidates(box, width, prefer, grid):
                trial = text_box_at(text, candidate)
                if first is None:
                    first = candidate
                if not any(_boxes_overlap(trial, other) for other in obstacles):
                    chosen = candidate
                    break
            if chosen is None:
                chosen = first                      # nowhere clear; keep it tidy anyway
            if chosen != first:
                relocated += 1
            comp.set_property_effects(field, {"position": chosen, "rotation": 0.0})
            obstacles.append(text_box_at(text, chosen))
            chosen_boxes.append((f"{comp.reference}.{field}={text}",
                                 text_box_at(text, chosen)))
            placed += 1

    return placed, relocated, chosen_boxes


# --- rendered-text audit -----------------------------------------------------

# Field text positions exist only once kicad-sch-api serialises a symbol -- the
# in-memory model leaves them to library defaults -- so the only honest place to
# check what will actually be drawn is the file that was just written.

_SYMBOL_RE = re.compile(r'\(lib_id "([^"]+)"\)?\s*\n\s*\(at ([-\d.]+) ([-\d.]+)')
_AT_RE = re.compile(r"\(at ([-\d.]+) ([-\d.]+)")
_LABEL_RE = re.compile(r'\(label "([^"]+)"\s*\n\s*\(at ([-\d.]+) ([-\d.]+)')
_WIRE_RE = re.compile(r'\(wire\s*\n\s*\(pts\s*\n\s*\(xy ([-\d.]+) ([-\d.]+)\)\s*'
                      r'\(xy ([-\d.]+) ([-\d.]+)\)')


def _text_box(text: str, x: float, y: float, centred: bool) -> Tuple[float, float, float, float]:
    width = max(len(text), 1) * CHAR_W_MM
    half_h = TEXT_ROW_MM / 2.0
    x0 = x - width / 2.0 if centred else x
    return (x0, y - half_h, x0 + width, y + half_h)


def audit_rendered_text(path: Path, block: dict) -> None:
    """Check the written file for visible references, clipping and collisions."""
    text = path.read_text(encoding="utf-8")
    sheet = SHEET_SIZES_MM.get(str(block.get("sheet", "A4")).upper(), SHEET_SIZES_MM["A4"])

    boxes, shown_refs, clipped, tilted = [], [], [], []
    for match in _SYMBOL_RE.finditer(text):
        lib_id, ax, ay = match.group(1), float(match.group(2)), float(match.group(3))
        end = text.find("(instances", match.start())
        for chunk in re.split(r'\(property "', text[match.start():end])[1:]:
            parts = chunk.split('"')
            field, value = parts[0], parts[2]
            if field not in ("Reference", "Value"):
                continue
            hidden = "(hide yes)" in chunk
            angle = re.search(r"\(at [-\d.]+ [-\d.]+ ([-\d.]+)\)", chunk)
            if not hidden and angle and abs(float(angle.group(1))) > EPS:
                tilted.append(f"{value} at {angle.group(1)} deg")
            if lib_id.startswith("power:") and field == "Reference" and not hidden:
                shown_refs.append(f"{value} on {lib_id}")
            if hidden or not value:
                continue
            at = _AT_RE.search(chunk)
            box = _text_box(value, float(at.group(1)), float(at.group(2)),
                            "(justify left)" not in chunk)
            if box[0] < 0 or box[1] < 0 or box[2] > sheet[0] or box[3] > sheet[1]:
                clipped.append(f"{value} at ({box[0]:.1f}, {box[1]:.1f})")
            boxes.append((f"{value}", box, False))

    # Net labels are drawn text too, and collide with fields just as happily.
    for match in _LABEL_RE.finditer(text):
        value, lx, ly = match.group(1), float(match.group(2)), float(match.group(3))
        box = _text_box(value, lx, ly, centred=False)
        if box[0] < 0 or box[1] < 0 or box[2] > sheet[0] or box[3] > sheet[1]:
            clipped.append(f"label {value} at ({box[0]:.1f}, {box[1]:.1f})")
        boxes.append((f"label:{value}", box, True))

    # Text must also stay off the copper, not just off other text.
    wires = [_segment_box(((float(m.group(1)), float(m.group(2))),
                           (float(m.group(3)), float(m.group(4)))))
             for m in _WIRE_RE.finditer(text)]
    # Net labels are exempt: a label only names a net by sitting on its wire.
    on_copper = [name for name, box, is_label in boxes
                 if not is_label and any(_boxes_overlap(box, w) for w in wires)]

    collisions = []
    for i, (name_a, box_a, _) in enumerate(boxes):
        for name_b, box_b, _ in boxes[i + 1:]:
            if _boxes_overlap(box_a, box_b):
                collisions.append(f"{name_a} / {name_b}")

    if shown_refs:
        raise AssertionError(f"{len(shown_refs)} power references visible: {shown_refs[:5]}")
    if tilted:
        raise AssertionError(f"{len(tilted)} field texts not upright: {tilted[:5]}")
    if clipped:
        raise AssertionError(f"{len(clipped)} text fields off the {sheet[0]}x{sheet[1]} mm "
                             f"sheet: {clipped[:5]}")
    if on_copper:
        raise AssertionError(f"{len(on_copper)} text fields sitting on wires: {on_copper[:5]}")
    if collisions:
        raise AssertionError(f"{len(collisions)} overlapping text fields: {collisions[:5]}")
    print(f"  verified: {len(boxes)} rendered text fields -- all upright at 0 deg, clear of "
          f"{len(wires)} wires, none clipped, none overlapping, every #PWR reference hidden")


# --- KiCad's own verdict ------------------------------------------------------


def _kicad_search_roots() -> List[Path]:
    """Where kicad-cli might live, newest install first.

    Discovery, not configuration: these are the standard install locations on
    each platform. KICAD_CLI short-circuits the whole search for an install
    that is somewhere unusual.
    """
    roots: List[Path] = []
    if sys.platform == "win32":
        for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            base = os.environ.get(var)
            if base:
                roots.append(Path(base) / "KiCad")
    elif sys.platform == "darwin":
        roots += [Path("/Applications/KiCad"), Path("/usr/local/bin")]
    else:
        roots += [Path("/usr/bin"), Path("/usr/local/bin"),
                  Path("/usr/lib/kicad/bin")]
    # ProgramFiles and ProgramW6432 are the same directory on 64-bit Windows,
    # so dedupe rather than globbing the same tree twice.
    seen: set[str] = set()
    unique: List[Path] = []
    for root in roots:
        key = os.path.normcase(str(root))
        if key not in seen and root.is_dir():
            seen.add(key)
            unique.append(root)
    return unique


def _find_kicad_cli() -> str | None:
    explicit = os.environ.get("KICAD_CLI")
    if explicit and Path(explicit).is_file():
        return explicit

    found = shutil.which("kicad-cli")
    if found:
        return found

    for base in _kicad_search_roots():
        try:
            matches = sorted(base.glob("**/kicad-cli*"), reverse=True)
        except OSError:
            continue
        for path in matches:
            if path.is_file():
                return str(path)
    return None


def erc_available() -> bool:
    """Whether KiCad's own checker can be reached from here."""
    return _find_kicad_cli() is not None


def collect_erc(path: Path) -> List[dict] | None:
    """Ask KiCad to check the file; None when the checker is unreachable.

    Returns the violations verbatim so callers can render them however they
    like -- the CLI prints them, the API serialises them.
    """
    cli = _find_kicad_cli()
    if cli is None:
        return None

    report = path.with_suffix(".erc.json")
    proc = subprocess.run([cli, "sch", "erc", "--severity-all", "--format", "json",
                           "-o", str(report), str(path)],
                          capture_output=True, text=True)
    if proc.returncode != 0 and not report.exists():
        return None

    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    finally:
        report.unlink(missing_ok=True)
    return [v for sheet in data.get("sheets", []) for v in sheet.get("violations", [])]


def run_erc(path: Path) -> bool:
    """Print KiCad's verdict. True when the file is clean or unchecked."""
    violations = collect_erc(path)
    if violations is None:
        print("  ERC: kicad-cli not found, skipped (install KiCad to enable)")
        return True
    if not violations:
        print("  verified: KiCad ERC reports 0 violations at every severity")
        return True

    print(f"  ERC FAILED: {len(violations)} violations")
    for v in violations[:8]:
        items = "; ".join(i.get("description", "") for i in v.get("items", []))
        print(f"    [{v['severity']}] {v['type']}: {v['description'][:60]}")
        print(f"        {items[:120]}")
    return False


# --- driver ------------------------------------------------------------------


def load_block(json_path: Path) -> dict:
    block = json.loads(json_path.read_text(encoding="utf-8"))["block"]
    block.setdefault("sheet", "A4")
    block.setdefault("grid_mm", GRID_MM)
    return block


class RailPlacementError(AssertionError):
    """No room for a power symbol -- the layout is too tight to route."""


def build(block: dict):
    """Build the sheet, widening the rows if the rails cannot be landed.

    `minimum_row_pitch()` predicts the room the rails need, but it reasons about
    vertical stacking only. If a rail is still boxed in -- by a wide neighbour,
    say -- widening and rebuilding is a better answer than handing the caller an
    error they can only fix by guessing at a number.
    """
    grid = float(block.get("grid_mm", GRID_MM))
    floor = 0.0
    for attempt in range(4):
        try:
            return _build_once(block, floor)
        except RailPlacementError as exc:
            if attempt == 3:
                raise
            floor = snap_up(floor + STUB_MM * 2, grid)
            print(f"  retrying with rows {floor} mm further apart ({exc})")
    raise AssertionError("unreachable")


def _build_once(block: dict, pitch_floor: float = 0.0):
    grid = float(block.get("grid_mm", GRID_MM))
    sch = ksa.create_schematic(block["name"])
    sch.set_paper_size(str(block["sheet"]).upper())

    print("  placing components:")
    comps = {}
    for i, spec in enumerate(block["components"]):
        # Scratch position; layout() moves everything onto the real grid below.
        # Nothing may touch property effects here: `set_property_effects` freezes
        # an *absolute* text position, and the later move() does not carry it
        # along, so any field written now is stranded at this scratch coordinate.
        comps[spec["ref"]] = sch.components.add(
            lib_id=spec["symbol"],
            reference=spec["ref"],
            value=str(spec.get("value", "")),
            position=(GRID_MM * (10 + 40 * i), GRID_MM * 10),
            footprint=spec.get("footprint") or None,
            rotation=float(spec.get("rotation", 0) or 0),
        )

    layout(sch, comps, block, pitch_floor)

    print("  routing nets:")
    opts = block.get("layout") or {}
    pwr_seq = count(1)
    rails: List[dict] = []
    net_segments: Dict[str, List[Seg]] = {}
    net_labels: Dict[str, Pt] = {}
    for net in block["nets"]:
        if str(net.get("class", "signal")).lower() == "power":
            place_power_rails(sch, comps, net, grid, opts, pwr_seq, rails, net_segments)
        else:
            route_signal_net(sch, comps, net, grid, net_segments, net_labels,
                             clearance=float(opts.get("body_clearance_mm",
                                                      BODY_CLEARANCE_MM)))

    flagged = add_power_flags(sch, comps, block, rails, grid, net_segments, count(1))
    if flagged:
        print(f"  power flags: {', '.join(flagged)} had no power-output driver, "
              f"PWR_FLAG added")
    unused = add_no_connects(sch, comps, block)
    if unused:
        print(f"  no-connects: {len(unused)} unused pins flagged ({', '.join(unused)})")

    # Only now is every wire and symbol final, so field text can be pinned down.
    placed, relocated, text_boxes = place_property_text(sch, grid)
    print(f"  placing text: {placed} fields upright at rotation 0, "
          f"{relocated} moved off their default slot to stay clear")

    verify(sch, comps, block, grid, rails, net_segments, text_boxes, net_labels)
    return sch


def main() -> None:
    in_path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "mock_ai_output.json"
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / "tracer_output.kicad_sch"

    block = load_block(in_path)
    print(f"Block: {block['name']} on {block['sheet']} "
          f"({len(block['components'])} components, {len(block['nets'])} nets)")

    sch = build(block)
    sch.save(str(out_path))
    audit_rendered_text(out_path, block)
    clean = run_erc(out_path)
    print(f"\nWrote {out_path}")
    if not clean:
        sys.exit(1)


if __name__ == "__main__":
    main()