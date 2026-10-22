#!/usr/bin/env python
"""Refuse to call a re-digitized flood zone line a flood zone change.

Compares two vintages of a flood hazard polygon layer against a list of
parcel points. A plain overlay says a parcel changed zone whenever its zone
label differs between the two vintages. When a map is re-digitized the lines
move a little, and every parcel that sits within that little of a line flips
its label without anything about the flood hazard having changed. This tool
asks one more question of every flipped parcel: did the zone it used to be in
still exist within a short distance of it in the new vintage, and the zone it
is in now within a short distance of it in the old one? If both did, the line
moved and the parcel did not change zone.

    python floodshift.py --self-test
    python floodshift.py old.json new.json parcels.csv
    python floodshift.py old.json new.json parcels.csv --retrace-distance 40
    python floodshift.py old.json new.json parcels.csv --change-distance 150
    python floodshift.py old.json new.json parcels.csv --zone-field FLD_ZONE
    python floodshift.py old.json new.json parcels.csv --subtype-field ""
    python floodshift.py old.json new.json parcels.csv --out zones.csv --apply

The two vintages are Esri JSON feature sets, which is what a query with
f=json returns and what the ArcGIS Pro tool Features To JSON writes by
default. Parcels are a CSV with an id and a projected X and Y. Every class
is decided by planar distance, so both vintages and the parcels must be in
one projected coordinate system, in feet or in metres. Nothing is written
without --apply.

Exit codes: 0 every parcel that exists in both vintages was decided
(STABLE, RETRACED or CHANGED), 1 at least one parcel is in the REVIEW band
and a human has to decide it, 2 a file could not be read or written, 3 the
input was refused (it cannot support an answer), 4 nothing was compared, 64
usage error, 70 an unexpected crash, which is never a finding.
"""

from __future__ import print_function

import argparse
import contextlib
import csv
import importlib.util
import io
import json
import math
import os
import re
import shutil
import sys
import tempfile
import traceback

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# A parcel whose zone differs between the vintages is RETRACED when the zone
# it was in still exists this close to it in the new vintage, and the zone it
# is in now existed this close to it in the old one. In the linear units of
# the input CRS. 30 is a starting value, not a standard: it is the order of the
# positional error of a digitized line, and nobody measured it on your data.
# Trace a line you know did not change in both vintages and measure its offset
# before you trust it.
DEFAULT_RETRACE_DISTANCE = 30.0

# Above this the zone really changed: the old zone is gone from the parcel's
# neighbourhood. Between the two distances the geometry has no opinion and a
# person looks. Again a starting value.
DEFAULT_CHANGE_DISTANCE = 100.0

# Printed after every distance. Nothing is converted.
DEFAULT_UNITS = "ft"

# How many parcels are listed under each heading.
DEFAULT_SAMPLE = 10

# Columns read when no flag names others. The two zone fields are the names the
# FEMA National Flood Hazard Layer uses for its flood hazard polygons.
DEFAULT_ID_FIELD = "PARCEL_ID"
DEFAULT_X_FIELD = "X"
DEFAULT_Y_FIELD = "Y"
DEFAULT_ZONE_FIELD = "FLD_ZONE"
DEFAULT_SUBTYPE_FIELD = "ZONE_SUBTY"

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

STABLE = "STABLE"
RETRACED = "RETRACED"
REVIEW = "REVIEW"
CHANGED = "CHANGED"
UNVERIFIED = "UNVERIFIED"

CLASSES = (STABLE, RETRACED, REVIEW, CHANGED, UNVERIFIED)

# The columns --out writes: one row per parcel, in file order.
OUT_COLUMNS = ("parcel_id", "line", "x", "y", "old_zone", "new_zone", "class",
               "shift", "reason")

_BREAK = re.compile(r"\r\n|\r|\n")


class Refused(Exception):
    """The input cannot support an answer. Exit 3, never a finding."""


# ------------------------------------------------------------------ numbers

def _is_finite(value):
    """True for a real number. NaN and infinity are not coordinates.

    Every comparison against NaN is False, so a NaN distance would clear no
    threshold and fall out of the ladder as CHANGED: the tool would invent a
    zone change out of an empty cell.
    """
    try:
        return not (math.isnan(value) or math.isinf(value))
    except TypeError:
        return False


def to_number(text):
    """A float from a CSV cell, or None when it is blank or not a number."""
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if _is_finite(value) else None


def norm(text):
    """A zone label with case and spacing taken out.

    ' ae' and 'AE' are one zone. A stray space between two vintages is not a
    flood hazard change, and without this it would be reported as one on
    every parcel that carries the label.
    """
    if text is None:
        return ""
    return " ".join(str(text).split()).upper()


def label(key):
    """A readable name for a (zone, subtype) key."""
    return key[0] if not key[1] else "%s [%s]" % (key[0], key[1])


# ----------------------------------------------------------------- geometry

def seg_distance(px, py, ax, ay, bx, by):
    """Distance from a point to the segment a-b. A zero-length edge is a point."""
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 == 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / length2
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _on_edge(px, py, ax, ay, bx, by):
    """True when the point lies exactly on the segment a-b."""
    if (bx - ax) * (py - ay) - (by - ay) * (px - ax) != 0.0:
        return False
    return min(ax, bx) <= px <= max(ax, bx) and min(ay, by) <= py <= max(ay, by)


OUTSIDE, INSIDE, EDGE = 0, 1, 2


def edges_of(rings):
    """Every edge of every ring as (ax, ay, bx, by), each ring closed."""
    out = []
    for ring in rings:
        count = len(ring)
        for i in range(count):
            ax, ay = ring[i]
            bx, by = ring[(i + 1) % count]
            out.append((ax, ay, bx, by))
    return out


def edge_state(px, py, edges):
    """OUTSIDE, INSIDE or EDGE for a point against a polygon's edges.

    The even-odd rule, over all rings of the polygon, so a hole is a hole
    whichever way the ring is wound. Esri documents the even-odd rule as the
    one that always draws its polygons correctly. A point exactly on an edge
    is EDGE, which is kept apart so that a point on a line shared by two zones
    cannot be handed to either of them.
    """
    inside = False
    for ax, ay, bx, by in edges:
        if _on_edge(px, py, ax, ay, bx, by):
            return EDGE
        if (ay > py) != (by > py):
            cross = ax + (py - ay) * (bx - ax) / (by - ay)
            if px < cross:
                inside = not inside
    return INSIDE if inside else OUTSIDE


def ring_state(px, py, rings):
    """edge_state over the edges of these rings."""
    return edge_state(px, py, edges_of(rings))


class Zone(object):
    """One flood hazard polygon: its label, rings and bounding box.

    A county-wide flood polygon can hold hundreds of thousands of vertices.
    The edges are filed in horizontal bands, an edge in every band its height
    touches, so a point only meets the edges level with it. That is exact for
    both questions asked of a polygon: the crossing test and the on-edge test
    can only involve an edge whose height spans the point's.
    """

    def __init__(self, zone, subtype, rings):
        self.zone = zone
        self.subtype = subtype
        self.rings = rings
        self.key = (zone, "")
        xs = [p[0] for ring in rings for p in ring]
        ys = [p[1] for ring in rings for p in ring]
        self.bbox = (min(xs), min(ys), max(xs), max(ys))
        edges = edges_of(rings)
        self.bands = max(1, int(math.sqrt(len(edges))))
        self.height = (self.bbox[3] - self.bbox[1]) / self.bands or 1.0
        self.buckets = [[] for _ in range(self.bands)]
        for edge in edges:
            for b in range(self._band(min(edge[1], edge[3])),
                           self._band(max(edge[1], edge[3])) + 1):
                self.buckets[b].append(edge)

    def _band(self, y):
        """The band a height falls in, the end bands taking what is beyond."""
        return max(0, min(self.bands - 1,
                          int((y - self.bbox[1]) / self.height)))

    def state(self, px, py):
        """OUTSIDE, INSIDE or EDGE for a point."""
        return edge_state(px, py, self.buckets[self._band(py)])

    def distance(self, px, py):
        """0 when the point is in or on the polygon, else the distance to its
        nearest edge, looking band by band outward from the point's own and
        stopping once no unvisited band can hold a nearer edge."""
        if self.state(px, py) != OUTSIDE:
            return 0.0
        mid = self._band(py)
        best = math.inf
        for k in range(self.bands):
            reached = False
            for b in {mid - k, mid + k}:
                if 0 <= b < self.bands:
                    low = self.bbox[1] + b * self.height
                    gap = max(low - py, 0.0, py - (low + self.height))
                    if gap < best:
                        reached = True
                        for ax, ay, bx, by in self.buckets[b]:
                            best = min(best, seg_distance(px, py, ax, ay,
                                                          bx, by))
            if not reached:
                break
        return best


class Vintage(object):
    """One snapshot of the zone layer."""

    def __init__(self, name, zones, sr, has_subtype):
        self.name = name
        self.zones = zones
        self.sr = sr
        self.has_subtype = has_subtype
        self.by_key = {}

    def index(self, use_subtype):
        """Fix the key of every zone and group the zones by it."""
        self.by_key = {}
        for zone in self.zones:
            zone.key = (zone.zone, zone.subtype if use_subtype else "")
            self.by_key.setdefault(zone.key, []).append(zone)

    def bounds(self):
        if not self.zones:
            return None
        boxes = [z.bbox for z in self.zones]
        return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))

    def locate(self, px, py):
        """The set of zone keys whose polygons hold the point, edges included."""
        keys = set()
        for zone in self.zones:
            x0, y0, x1, y1 = zone.bbox
            if x0 <= px <= x1 and y0 <= py <= y1:
                if zone.state(px, py) != OUTSIDE:
                    keys.add(zone.key)
        return keys

    def distance_to(self, key, px, py):
        """Distance to the nearest polygon of this zone key, or inf when this
        vintage has none."""
        best = math.inf
        for zone in self.by_key.get(key, ()):
            x0, y0, x1, y1 = zone.bbox
            gap = math.hypot(max(x0 - px, 0.0, px - x1),
                             max(y0 - py, 0.0, py - y1))
            if gap >= best:
                continue
            best = min(best, zone.distance(px, py))
        return best


def _rings(raw, index):
    """Validated rings from an Esri polygon geometry, as lists of (x, y)."""
    bad = Refused("feature %d: the geometry is not polygon rings of at least "
                  "3 finite points each" % index)
    rings = raw.get("rings") if isinstance(raw, dict) else None
    if not isinstance(rings, list) or not rings:
        raise bad
    out = []
    for ring in rings:
        if not isinstance(ring, list) or len(ring) < 3:
            raise bad
        points = []
        for pt in ring:
            if not (isinstance(pt, list) and len(pt) >= 2
                    and _is_finite(pt[0]) and _is_finite(pt[1])):
                raise bad
            points.append((float(pt[0]), float(pt[1])))
        out.append(points)
    return out


def sr_ids(sr):
    """The identifiers a spatialReference object gives, as a set.

    wkid keeps the value first assigned and latestWkid the current one, so one
    projected system can be 102658 on one service and 2236 on another. Two
    spatial references agree when they share an identifier.
    """
    ids = set()
    if isinstance(sr, dict):
        for name in ("wkid", "latestWkid"):
            if sr.get(name) is not None:
                ids.add("wkid:%s" % sr[name])
        if sr.get("wkt"):
            ids.add("wkt:%s" % " ".join(str(sr["wkt"]).split()))
    return ids


def parse_vintage(data, name, zone_field=DEFAULT_ZONE_FIELD,
                  subtype_field=DEFAULT_SUBTYPE_FIELD):
    """A Vintage from a parsed Esri JSON feature set.

    Refuses a response the server cut short: exceededTransferLimit true means
    the server stopped at its record limit, so zones are missing and a parcel
    in one would be reported as outside every zone.
    """
    if not isinstance(data, dict) or not isinstance(data.get("features"),
                                                    list):
        raise Refused("%s: not an Esri JSON feature set, there is no "
                      "features list" % name)
    if data.get("exceededTransferLimit") is True:
        raise Refused(
            "%s: exceededTransferLimit is true, so the server stopped at its "
            "record limit and some zones are missing. Page the query with "
            "resultOffset until the flag is false, then try again." % name)
    sr = data.get("spatialReference")
    zones = []
    has_subtype = False
    for index, feat in enumerate(data["features"], 1):
        attrs = feat.get("attributes") if isinstance(feat, dict) else None
        if not isinstance(attrs, dict):
            raise Refused("%s: feature %d has no attributes" % (name, index))
        if zone_field not in attrs:
            raise ValueError(
                "%s: feature %d has no field %s. It has: %s"
                % (name, index, zone_field, ", ".join(sorted(attrs))))
        zone = norm(attrs[zone_field])
        if not zone:
            raise Refused("%s: feature %d has a blank %s, so it names no zone"
                          % (name, index, zone_field))
        subtype = ""
        if subtype_field and subtype_field in attrs:
            has_subtype = True
            subtype = norm(attrs[subtype_field])
        geometry = feat.get("geometry")
        if not sr and isinstance(geometry, dict):
            sr = geometry.get("spatialReference")
        zones.append(Zone(zone, subtype, _rings(geometry, index)))
    return Vintage(name, zones, sr, has_subtype)


def settle(old, new, subtype_field=DEFAULT_SUBTYPE_FIELD):
    """Fix the zone keys of both vintages and return the notes to print.

    A subtype is part of the key only when both vintages carry it. Zone X
    covers both the shaded and the unshaded hazard areas and only the subtype
    tells them apart. A subtype present in one vintage and not the other
    would make every zone differ.

    Raises Refused when the two vintages declare spatial references that
    share no identifier, or when their extents do not touch.
    """
    notes = []
    use_subtype = bool(subtype_field) and old.has_subtype and new.has_subtype
    if subtype_field and not use_subtype:
        notes.append("NOTE      %s is not in both vintages, so it is left out "
                     "of the zone key and Zone X shaded and unshaded are not "
                     "told apart" % subtype_field)
    old.index(use_subtype)
    new.index(use_subtype)
    a, b = sr_ids(old.sr), sr_ids(new.sr)
    if a and b and not (a & b):
        raise Refused(
            "the two vintages are in different coordinate systems (%s and "
            "%s). Re-export one with outSR set to the other's."
            % (", ".join(sorted(a)), ", ".join(sorted(b))))
    if not a or not b:
        notes.append("NOTE      a vintage declares no spatialReference, so "
                     "the coordinate systems were not compared")
    box_a, box_b = old.bounds(), new.bounds()
    if box_a and box_b and (box_a[0] > box_b[2] or box_b[0] > box_a[2]
                            or box_a[1] > box_b[3] or box_b[1] > box_a[3]):
        raise Refused("the two vintages do not overlap anywhere, so they "
                      "are not in one coordinate system or not one place")
    return notes


# ---------------------------------------------------------------- the core

class Parcel(object):
    def __init__(self, parcel_id, x, y, line=None):
        self.parcel_id = parcel_id
        self.x = x
        self.y = y
        self.line = line


class Result(object):
    def __init__(self, parcel, cls, reason, old_key=None, new_key=None,
                 shift=None):
        self.parcel = parcel
        self.cls = cls
        self.reason = reason
        self.old_key = old_key
        self.new_key = new_key
        self.shift = shift


class Report(object):
    def __init__(self, results, retrace, change, units, notes=()):
        self.results = results
        self.retrace = retrace
        self.change = change
        self.units = units
        self.notes = list(notes)

    def count(self, cls):
        return sum(1 for r in self.results if r.cls == cls)

    @property
    def compared(self):
        return len(self.results) - self.count(UNVERIFIED)

    @property
    def differing(self):
        return (self.count(RETRACED) + self.count(REVIEW)
                + self.count(CHANGED))


def classify_shift(shift, retrace=DEFAULT_RETRACE_DISTANCE,
                   change=DEFAULT_CHANGE_DISTANCE, units=DEFAULT_UNITS):
    """(class, reason) for a parcel whose zone differs, from its shift.

    The shift is how far the line would have had to move: the larger of the
    distance to the old vintage's version of the new zone and the distance to
    the new vintage's version of the old zone. The larger, because a parcel
    next to a line that moved 10 units and next to a zone that vanished is
    not explained by the 10.
    """
    if not (_is_finite(retrace) and _is_finite(change)
            and 0 <= retrace <= change):
        raise ValueError("need 0 <= retrace distance <= change distance, "
                         "both finite")
    if shift <= retrace:
        return RETRACED, ("the line moved at most %.1f %s: not a zone change"
                          % (retrace, units))
    if shift > change:
        if math.isinf(shift):
            return CHANGED, ("one of the two zones does not exist in the "
                             "other vintage at all")
        return CHANGED, ("the line would have to move %.1f %s, more than "
                         "%.1f %s: a real change" % (shift, units, change,
                                                    units))
    return REVIEW, ("the line would have to move %.1f %s, between %.1f and "
                    "%.1f %s: neither a re-trace nor a change"
                    % (shift, units, retrace, change, units))


def _where(keys, which):
    """The reason a point has no single zone in one vintage, or None."""
    if not keys:
        return "in no zone of the %s vintage" % which
    if len(keys) > 1:
        return ("in more than one zone of the %s vintage (%s), an overlap or "
                "a point exactly on a shared line"
                % (which, ", ".join(label(k) for k in sorted(keys))))
    return None


def judge(parcel, old, new, retrace, change, units):
    """One parcel's Result."""
    if parcel.x is None or parcel.y is None:
        return Result(parcel, UNVERIFIED, "no usable coordinate")
    keys_old = old.locate(parcel.x, parcel.y)
    keys_new = new.locate(parcel.x, parcel.y)
    problems = [p for p in (_where(keys_old, "old"), _where(keys_new, "new"))
                if p]
    if problems:
        return Result(parcel, UNVERIFIED, "; ".join(problems))
    a, b = next(iter(keys_old)), next(iter(keys_new))
    if a == b:
        return Result(parcel, STABLE, "the same zone in both vintages", a, b)
    shift = max(old.distance_to(b, parcel.x, parcel.y),
                new.distance_to(a, parcel.x, parcel.y))
    cls, reason = classify_shift(shift, retrace, change, units)
    return Result(parcel, cls, reason, a, b, shift)


def compare(parcels, old, new, retrace=DEFAULT_RETRACE_DISTANCE,
            change=DEFAULT_CHANGE_DISTANCE, units=DEFAULT_UNITS, notes=()):
    """Report for every parcel. Raises ValueError on bad distances."""
    classify_shift(0.0, retrace, change, units)
    return Report([judge(p, old, new, retrace, change, units)
                   for p in parcels], retrace, change, units, notes)


def gate(report):
    """The exit code for a report."""
    if report.compared == 0:
        return 4
    return 1 if report.count(REVIEW) else 0


def _shift_text(result, units):
    if math.isinf(result.shift):
        return "no such zone in the other vintage"
    return "%.1f %s" % (result.shift, units)


def describe(report, sample=DEFAULT_SAMPLE, overlap_hint=""):
    """The printed report, as a list of lines."""
    lines = list(report.notes)
    total = len(report.results)
    lines.append("parcels classified: %d of %d" % (total, total))
    for cls in CLASSES:
        lines.append("  %-10s %d" % (cls, report.count(cls)))
    lines.append("retrace distance %.1f %s, change distance %.1f %s"
                 % (report.retrace, report.units, report.change,
                    report.units))
    if report.compared == 0:
        lines.append("")
        lines.append("VERDICT: NOTHING COMPARED. No parcel was inside exactly "
                     "one zone of both vintages, so the geometry checked "
                     "nothing. This is not a clean run.%s" % overlap_hint)
        return lines
    lines.append("")
    lines.append("a plain overlay reports %d parcel(s) changed zone"
                 % report.differing)

    def worst(r):
        return (-r.shift, r.parcel.line or 0)

    def section(title, cls):
        rows = [r for r in report.results if r.cls == cls]
        if not rows:
            return
        rows.sort(key=worst)
        lines.append("")
        lines.append(title)
        for r in rows[:sample]:
            lines.append("  %s  %s to %s, %s, line %s"
                         % (r.parcel.parcel_id or "(no id)", label(r.old_key),
                            label(r.new_key), _shift_text(r, report.units),
                            r.parcel.line))
        if len(rows) > sample:
            lines.append("  ... and %d more" % (len(rows) - sample))

    section("zone changes the geometry confirms, worst first:", CHANGED)
    section("zone differences in the band between, for a human to look at:",
            REVIEW)
    section("zone differences that are a re-traced line, not a change:",
            RETRACED)
    unverified = [r for r in report.results if r.cls == UNVERIFIED]
    if unverified:
        lines.append("")
        lines.append("parcels with no answer:")
        for r in unverified[:sample]:
            lines.append("  %s  %s, line %s"
                         % (r.parcel.parcel_id or "(no id)", r.reason,
                            r.parcel.line))
        if len(unverified) > sample:
            lines.append("  ... and %d more" % (len(unverified) - sample))
    lines.append("")
    if report.count(REVIEW):
        lines.append("VERDICT: %d parcel(s) in the review band. Decide them "
                     "before a change count is published."
                     % report.count(REVIEW))
    else:
        lines.append("VERDICT: %d parcel(s) changed zone. %d of the %d zone "
                     "difference(s) were a re-traced line and are not "
                     "changes." % (report.count(CHANGED),
                                   report.count(RETRACED), report.differing))
    return lines


def classified_rows(report):
    """The --out rows, one per parcel."""
    rows = []
    for r in report.results:
        rows.append({
            "parcel_id": r.parcel.parcel_id,
            "line": r.parcel.line,
            "x": "" if r.parcel.x is None else repr(r.parcel.x),
            "y": "" if r.parcel.y is None else repr(r.parcel.y),
            "old_zone": label(r.old_key) if r.old_key else "",
            "new_zone": label(r.new_key) if r.new_key else "",
            "class": r.cls,
            "shift": ("" if r.shift is None or math.isinf(r.shift)
                      else "%.3f" % r.shift),
            "reason": r.reason,
        })
    return rows


# ---------------------------------------------------------------------- io

def _breaks(value):
    """How many line breaks one parsed cell carries (a quoted newline)."""
    cells = value if isinstance(value, list) else [value]
    return sum(len(_BREAK.findall(c)) for c in cells if c)


def read_parcels(path, fields):
    """Parcels from a CSV. Raises ValueError for a column that is not there
    and Refused for a parcel id that two rows claim."""
    # utf-8-sig: Excel and Esri's Table To Table write a BOM, which would
    # otherwise glue itself to the first column name.
    with open(path, "r", newline="", encoding="utf-8-sig",
              errors="replace") as handle:
        reader = csv.DictReader(handle)
        header = list(reader.fieldnames or [])
        absent = [f for f in (fields["id"], fields["x"], fields["y"])
                  if f not in header]
        if absent:
            raise ValueError(
                "%s: columns not in the CSV: %s. It has: %s"
                % (path, ", ".join(absent),
                   ", ".join(header) if header else "(no columns at all)"))
        parcels = []
        for raw in reader:
            # line_num is where the record ends. A blank line or a quoted
            # newline would move a naive row count off the file.
            line = reader.line_num - sum(_breaks(v) for v in raw.values())
            parcel_id = (raw.get(fields["id"]) or "").strip()
            parcels.append(Parcel(parcel_id, to_number(raw.get(fields["x"])),
                                  to_number(raw.get(fields["y"])), line))
    seen = {}
    for p in parcels:
        if p.parcel_id:
            seen.setdefault(p.parcel_id, []).append(p.line)
    repeats = sorted((k, v) for k, v in seen.items() if len(v) > 1)
    if repeats:
        raise Refused(
            "%s: the parcel id is not unique, so a zone change could not be "
            "tied to one parcel: %s" % (path, "; ".join(
                "'%s' is on lines %s" % (k, ", ".join(str(n) for n in v))
                for k, v in repeats[:5])))
    return parcels


def read_vintage(path, zone_field, subtype_field):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
        text = handle.read()
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise Refused("%s: not valid JSON (%s)" % (path, exc))
    return parse_vintage(data, path, zone_field, subtype_field)


def looks_like_degrees(vintages, parcels):
    """True when every usable coordinate fits longitude and latitude.

    The National Flood Hazard Layer service stores its polygons in NAD83
    geographic coordinates (wkid 4269). Distances in degrees mean nothing
    against a threshold in feet, so the layer has to be requested in a
    projected system with outSR first.
    """
    points = [(p.x, p.y) for p in parcels if p.x is not None
              and p.y is not None]
    for v in vintages:
        for zone in v.zones:
            points.extend(pt for ring in zone.rings for pt in ring)
    return bool(points) and all(abs(x) <= 180.0 and abs(y) <= 90.0
                                for x, y in points)


def _write_csv(path, columns, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="floodshift.py",
        description="Refuse to call a re-digitized flood zone line a flood "
                    "zone change.",
        epilog="All three files must be in one projected CRS. Nothing is "
               "written without --apply.",
        allow_abbrev=False,
    )
    ap.add_argument("old", nargs="?", metavar="OLD",
                    help="the older vintage, an Esri JSON feature set")
    ap.add_argument("new", nargs="?", metavar="NEW",
                    help="the newer vintage, an Esri JSON feature set")
    ap.add_argument("parcels", nargs="?", metavar="PARCELS",
                    help="the parcels as a CSV with an id, X and Y")
    ap.add_argument("--id-field", dest="id_field", default=DEFAULT_ID_FIELD,
                    help="the parcel id column (default %s)"
                         % DEFAULT_ID_FIELD)
    ap.add_argument("--x-field", dest="x_field", default=DEFAULT_X_FIELD,
                    help="the X column (default %s)" % DEFAULT_X_FIELD)
    ap.add_argument("--y-field", dest="y_field", default=DEFAULT_Y_FIELD,
                    help="the Y column (default %s)" % DEFAULT_Y_FIELD)
    ap.add_argument("--zone-field", dest="zone_field",
                    default=DEFAULT_ZONE_FIELD,
                    help="the zone attribute in both vintages (default %s)"
                         % DEFAULT_ZONE_FIELD)
    ap.add_argument("--subtype-field", dest="subtype_field",
                    default=DEFAULT_SUBTYPE_FIELD,
                    help="the zone subtype attribute, part of the zone key "
                         "when both vintages carry it. An empty string turns "
                         "it off. (default %s)" % DEFAULT_SUBTYPE_FIELD)
    ap.add_argument("--retrace-distance", dest="retrace_distance",
                    type=float, default=DEFAULT_RETRACE_DISTANCE,
                    help="at or under this shift a zone difference is "
                         "RETRACED, in the CRS's own units (default %.0f)"
                         % DEFAULT_RETRACE_DISTANCE)
    ap.add_argument("--change-distance", dest="change_distance", type=float,
                    default=DEFAULT_CHANGE_DISTANCE,
                    help="above this shift a zone difference is CHANGED "
                         "(default %.0f)" % DEFAULT_CHANGE_DISTANCE)
    ap.add_argument("--units", default=DEFAULT_UNITS,
                    help="label printed after every distance. Nothing is "
                         "converted. (default %s)" % DEFAULT_UNITS)
    ap.add_argument("--sample", type=int, default=DEFAULT_SAMPLE,
                    help="how many parcels to list under each heading "
                         "(default %d)" % DEFAULT_SAMPLE)
    ap.add_argument("--projected", action="store_true",
                    help="say the coordinates are projected, to skip the "
                         "check that refuses values that all fit longitude "
                         "and latitude")
    ap.add_argument("--out", help="path for the classification CSV")
    ap.add_argument("--apply", action="store_true",
                    help="write --out. Without this nothing is written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def _tolerate_odd_characters():
    """Make print() unable to fail on a character the console cannot encode.

    A parcel id read from a file that is not UTF-8 arrives as U+FFFD, which a
    cp1252 pipe cannot write. The UnicodeEncodeError would exit 1 and drop
    the VERDICT line, and 1 here means a parcel needs review.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="replace")


def _same_file(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(
        os.path.realpath(b))


def main(argv=None):
    """Run the CLI. An error this code did not anticipate exits 70, because 1
    is the code a scheduled job reads as 'a parcel needs review'."""
    _tolerate_odd_characters()
    try:
        return _run(argv)
    except Exception as exc:
        print("error: unexpected %s: %s. This is a fault in the tool, not a "
              "finding about the data." % (type(exc).__name__, exc),
              file=sys.stderr)
        traceback.print_exc()
        return 70


def _run(argv):
    try:
        args = _parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        # argparse exits 2 on a flag it refuses, and 2 here means a file
        # could not be read. A mistyped --ap is a usage error, so 64.
        return 64 if exc.code == 2 else exc.code

    if args.self_test:
        return self_test()

    if not (args.old and args.new and args.parcels):
        print("error: OLD, NEW and PARCELS are required. Use --self-test to "
              "verify the tool without them.", file=sys.stderr)
        return 64
    if args.sample < 0:
        print("error: --sample cannot be negative.", file=sys.stderr)
        return 64
    if args.apply and not args.out:
        print("error: --apply needs --out.", file=sys.stderr)
        return 64
    if args.out and any(_same_file(args.out, p)
                        for p in (args.old, args.new, args.parcels)):
        # Checked on a dry run too: the run that would replace an input
        # should say so before --apply is added, not after.
        print("error: --out %s is also an input. Name a new file." % args.out,
              file=sys.stderr)
        return 64

    try:
        old = read_vintage(args.old, args.zone_field, args.subtype_field)
        new = read_vintage(args.new, args.zone_field, args.subtype_field)
        fields = {"id": args.id_field, "x": args.x_field, "y": args.y_field}
        parcels = read_parcels(args.parcels, fields)
        if not args.projected and looks_like_degrees((old, new), parcels):
            raise Refused(
                "every coordinate fits longitude -180..180 and latitude "
                "-90..90, so these look like degrees. Distances in degrees "
                "mean nothing against a threshold in feet. Request the "
                "layer with outSR set to a projected system, or pass "
                "--projected if this is a local grid.")
        notes = settle(old, new, args.subtype_field)
        report = compare(parcels, old, new, args.retrace_distance,
                         args.change_distance, args.units, notes)
    except Refused as exc:
        print("REFUSED   %s" % exc)
        print("\nVERDICT: REFUSED")
        return 3
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64
    except (IOError, OSError, csv.Error) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    hint = ""
    if report.compared == 0 and any(p.x is not None for p in parcels):
        hint = (" Check that the parcels are in the same coordinate system "
                "as the zones.")
    print("floodshift: %s %d zone(s), %s %d zone(s), %s %d parcel(s)"
          % (args.old, len(old.zones), args.new, len(new.zones),
             args.parcels, len(parcels)))
    for line in describe(report, args.sample, hint):
        print(line)

    if args.out:
        if args.apply:
            try:
                _write_csv(args.out, OUT_COLUMNS, classified_rows(report))
            except (IOError, OSError, csv.Error) as exc:
                print("error: could not write %s: %s" % (args.out, exc),
                      file=sys.stderr)
                return 2
            print("\nwrote %s" % args.out)
        else:
            print("\nCheck only. %s was not written. Re-run with --apply."
                  % args.out)
    return gate(report)


# ---------------------------------------------------------------- self-test

def self_test():
    """Run the assertions in a temp directory that is removed even when one
    of them raises."""
    tmp = tempfile.mkdtemp(prefix="floodshift-selftest-")
    try:
        return _self_test(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _self_test(tmp):
    """Assertions over the decision core and the CLI. No network, no
    credentials. The only files are the ones written under tmp."""
    passed = [0]
    failed = []

    def check(cond, label_):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label_)
        else:
            failed.append(label_)
            print("FAIL  %s" % label_)

    def raises(fn, label_, exc_type=ValueError):
        try:
            fn()
        except exc_type:
            check(True, label_)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label_, exc))
        else:
            check(False, "%s (no error raised)" % label_)

    print("floodshift self-test: temporary files only, no network, no "
          "credentials")
    print("-" * 68)

    def rect(x0, y0, x1, y1):
        return [[x0, y0], [x0, y1], [x1, y1], [x1, y0], [x0, y0]]

    def feat(zone, rings, subtype=None):
        attrs = {"FLD_ZONE": zone}
        if subtype is not None:
            attrs["ZONE_SUBTY"] = subtype
        return {"attributes": attrs, "geometry": {"rings": rings}}

    def fset(features, wkid=2236, extra=None):
        data = {"spatialReference": {"wkid": wkid},
                "features": features}
        data.update(extra or {})
        return data

    def pair(old_features, new_features, subtype_field=DEFAULT_SUBTYPE_FIELD,
             old_wkid=2236, new_wkid=2236):
        old = parse_vintage(fset(old_features, old_wkid), "old",
                            DEFAULT_ZONE_FIELD, subtype_field)
        new = parse_vintage(fset(new_features, new_wkid), "new",
                            DEFAULT_ZONE_FIELD, subtype_field)
        notes = settle(old, new, subtype_field)
        return old, new, notes

    def one(old, new, x, y, retrace=30.0, change=100.0):
        return judge(Parcel("P", x, y, 2), old, new, retrace, change, "ft")

    # World: old AE on the west half, X on the east half, line at x=1000.
    old_world = [feat("AE", [rect(0, 0, 1000, 1000)]),
                 feat("X", [rect(1000, 0, 2000, 1000)])]

    # ---- the story: the line is re-drawn 15 east and nothing has changed
    new_moved = [feat("AE", [rect(0, 0, 1015, 1000)]),
                 feat("X", [rect(1015, 0, 2000, 1000)])]
    o, n, _ = pair(old_world, new_moved)
    r = one(o, n, 1010, 500)
    check(r.old_key == ("X", "") and r.new_key == ("AE", ""),
          "a parcel 10 east of the old line is X in the old vintage and AE "
          "in the new, so a plain overlay calls it a change")
    check(r.cls == RETRACED,
          "and the tool calls it RETRACED, because the old zone is 5 away in "
          "the new vintage  <-- pinned defect")
    check(abs(r.shift - 10.0) < 1e-9,
          "the shift is the larger of the two distances, 10 here")
    check(one(o, n, 500, 500).cls == STABLE,
          "a parcel deep inside AE is STABLE")
    check(one(o, n, 1500, 500).cls == STABLE,
          "a parcel deep inside X is STABLE")

    # ---- a real change: a patch of AE appears in the middle of the X area
    patch = rect(1400, 400, 1600, 600)
    new_patch = [feat("AE", [rect(0, 0, 1000, 1000)]),
                 feat("X", [rect(1000, 0, 2000, 1000), patch]),
                 feat("AE", [patch])]
    o, n, _ = pair(old_world, new_patch)
    r = one(o, n, 1500, 500)
    check(r.cls == CHANGED and abs(r.shift - 500.0) < 1e-9,
          "a parcel in a new AE patch 500 from any old AE is CHANGED  "
          "<-- pinned defect")
    check(one(o, n, 1500, 100).cls == STABLE,
          "a hole in a polygon is a hole: X above the patch is still X")
    check(n.locate(1500, 500) == {("AE", "")},
          "the point inside the hole is in AE only, not also in X")

    # ---- the band between
    new_far = [feat("AE", [rect(0, 0, 1060, 1000)]),
               feat("X", [rect(1060, 0, 2000, 1000)])]
    o, n, _ = pair(old_world, new_far)
    r = one(o, n, 1031, 500)
    check(r.cls == REVIEW and abs(r.shift - 31.0) < 1e-9,
          "a 31 shift is REVIEW, the geometry has no opinion")
    check(one(o, n, 1030, 500).cls == RETRACED,
          "and 30 exactly is still RETRACED")
    r = one(o, n, 1050, 500)
    check(r.cls == REVIEW and abs(r.shift - 50.0) < 1e-9,
          "a parcel 50 from the old line is REVIEW")

    # ---- thresholds at their exact boundary
    def cls_at(s, rt=30.0, ch=100.0):
        return classify_shift(s, rt, ch)[0]

    check(cls_at(0.0) == RETRACED, "a shift of zero is RETRACED")
    check(cls_at(30.0) == RETRACED,
          "exactly the retrace distance is RETRACED, the limit is inclusive  "
          "<-- pinned defect")
    check(cls_at(30.001) == REVIEW, "a thousandth past it is REVIEW")
    check(cls_at(100.0) == REVIEW,
          "exactly the change distance is REVIEW, the limit is inclusive")
    check(cls_at(100.001) == CHANGED, "a thousandth past it is CHANGED")
    check(cls_at(math.inf) == CHANGED,
          "a zone that does not exist in the other vintage is CHANGED")
    check("does not exist" in classify_shift(math.inf)[1],
          "and the reason says the zone is gone")
    check(cls_at(5.0, 0.0, 0.0) == CHANGED and cls_at(0.0, 0.0, 0.0)
          == RETRACED,
          "two limits of zero are legal and only an exact match retraces")
    check(cls_at(10.0, 10.0, 10.0) == RETRACED
          and cls_at(10.5, 10.0, 10.0) == CHANGED,
          "two equal limits leave the review band empty")
    raises(lambda: classify_shift(1.0, -1.0, 5.0),
           "a negative retrace distance is refused")
    raises(lambda: classify_shift(1.0, 10.0, 5.0),
           "a change distance under the retrace distance is refused")
    raises(lambda: classify_shift(1.0, float("nan"), 5.0),
           "a NaN retrace distance is refused, it would clear no threshold"
           "  <-- pinned defect")
    raises(lambda: classify_shift(1.0, 0.0, float("inf")),
           "an infinite change distance is refused")

    # ---- the larger of the two distances decides, never the smaller
    new_gone = [feat("X", [rect(0, 0, 2000, 1000)])]
    o, n, _ = pair(old_world, new_gone)
    r = one(o, n, 990, 500)
    check(r.cls == CHANGED and math.isinf(r.shift),
          "an old zone that is gone from the new vintage is CHANGED, though "
          "the new zone is 10 away in the old one  <-- pinned defect")
    new_all_ae = [feat("AE", [rect(0, 0, 2000, 1000)])]
    o, n, _ = pair(old_world, new_all_ae)
    check(one(o, n, 1010, 500).cls == CHANGED,
          "and the same the other way round: the new zone has no old "
          "neighbour")

    # ---- a label that differs only in case or spacing is not a change
    spaced = [feat(" ae ", [rect(0, 0, 1000, 1000)]),
              feat("x", [rect(1000, 0, 2000, 1000)])]
    o, n, _ = pair(old_world, spaced)
    check(one(o, n, 500, 500).cls == STABLE and one(o, n, 1500, 500).cls
          == STABLE,
          "' ae ' and 'AE' are one zone, a stray space is no flood change  "
          "<-- pinned defect")
    check(norm(None) == "" and norm("area  of\tminimal") == "AREA OF MINIMAL",
          "norm folds case and runs of whitespace and reads None as blank")
    check(label(("AE", "")) == "AE"
          and label(("X", "FLOODWAY")) == "X [FLOODWAY]",
          "a label shows the subtype only when there is one")

    # ---- the subtype is part of the zone, because X is two hazards
    shaded = "0.2 PCT ANNUAL CHANCE FLOOD HAZARD"
    minimal = "AREA OF MINIMAL FLOOD HAZARD"
    same_shape_old = [feat("X", [rect(0, 0, 2000, 1000)], minimal)]
    same_shape_new = [feat("X", [rect(0, 0, 2000, 1000)], shaded)]
    o, n, notes = pair(same_shape_old, same_shape_new)
    r = one(o, n, 500, 500)
    check(r.cls == CHANGED and notes == [],
          "unshaded X becoming shaded X is CHANGED, the zone letter alone "
          "would call it STABLE  <-- pinned defect")
    check(r.old_key == ("X", minimal) and label(r.new_key).startswith("X ["),
          "and the result names both subtypes")
    o, n, notes = pair(same_shape_old, same_shape_new, subtype_field="")
    check(one(o, n, 500, 500).cls == STABLE and notes == [],
          "with the subtype turned off the same parcel is STABLE, said "
          "nothing")
    o, n, notes = pair(old_world, same_shape_new)
    check(len(notes) == 1 and "left out of the zone key" in notes[0],
          "a subtype in one vintage only is left out of the key and the run "
          "says so  <-- pinned defect")
    check(one(o, n, 990, 500).cls == CHANGED,
          "and then the zone letters are compared as they stand")

    # ---- a point with no single zone gets no answer
    o, n, _ = pair(old_world, new_moved)
    r = one(o, n, 1000, 500)
    check(r.cls == UNVERIFIED and "more than one zone of the old" in r.reason,
          "a point exactly on the line two zones share is UNVERIFIED, "
          "neither zone may claim it  <-- pinned defect")
    check(one(o, n, 0, 500).cls == STABLE,
          "a point on the outer edge of the only zone around it is inside "
          "it")
    r = one(o, n, 3000, 500)
    check(r.cls == UNVERIFIED and "in no zone of the old" in r.reason
          and "in no zone of the new" in r.reason and "; " in r.reason,
          "a point in no zone of either vintage says so for both")
    r = one(o, n, None, 500)
    check(r.cls == UNVERIFIED and r.reason == "no usable coordinate",
          "a parcel with no coordinate is UNVERIFIED")
    r = one(o, n, 500, None)
    check(r.cls == UNVERIFIED, "and so is one with no Y")
    twice = [feat("AE", [rect(0, 0, 1000, 1000)]),
             feat("AE", [rect(500, 0, 1500, 1000)])]
    o2, n2, _ = pair(twice, twice)
    check(one(o2, n2, 700, 500).cls == STABLE,
          "two overlapping polygons of one zone are one zone, not an "
          "overlap")
    clash = [feat("AE", [rect(0, 0, 1000, 1000)]),
             feat("X", [rect(500, 0, 1500, 1000)])]
    o2, n2, _ = pair(clash, clash)
    check(one(o2, n2, 700, 500).cls == UNVERIFIED,
          "two polygons of different zones over one point are an overlap")

    # ---- the geometry
    notch = [(0, 0), (0, 10), (4, 10), (4, 4), (6, 4), (6, 10), (10, 10),
             (10, 0), (0, 0)]
    check(ring_state(5, 7, [notch]) == OUTSIDE,
          "a point in the notch of a concave ring is outside")
    check(ring_state(2, 7, [notch]) == INSIDE, "and one in an arm is inside")
    check(ring_state(0, 0, [notch]) == EDGE and ring_state(10, 5, [notch])
          == EDGE, "a vertex and a point on an edge are EDGE")
    check(ring_state(5, 5, [[(0, 0), (0, 10), (10, 10), (10, 0)]]) == INSIDE,
          "a ring that is not closed is closed by the reader")
    check(ring_state(5, 5, [[(0, 0), (0, 0), (0, 10), (10, 10), (10, 0)]])
          == INSIDE, "a repeated vertex is a zero-length edge, not a fault")
    check(ring_state(5, 5, [[(0, 0), (0, 10), (10, 10), (10, 0)],
                            [(3, 3), (7, 3), (7, 7), (3, 7)]]) == OUTSIDE
          and ring_state(5, 5, [[(0, 0), (10, 0), (10, 10), (0, 10)],
                                [(3, 3), (3, 7), (7, 7), (7, 3)]]) == OUTSIDE,
          "a hole is a hole whichever way it is wound")
    check(seg_distance(5, 5, 0, 0, 10, 0) == 5.0
          and seg_distance(-3, 4, 0, 0, 10, 0) == 5.0
          and seg_distance(13, 4, 0, 0, 10, 0) == 5.0
          and seg_distance(3, 4, 0, 0, 0, 0) == 5.0,
          "distance to a segment: across it, past either end, and to a "
          "zero-length one")
    box = Zone("AE", "", [[(0, 0), (0, 10), (10, 10), (10, 0)]])
    check(box.distance(5, 5) == 0.0 and box.distance(10, 5) == 0.0
          and box.distance(13, 5) == 3.0 and box.distance(13, 14) == 5.0,
          "distance to a polygon is 0 inside or on it, else to its edge")
    check(box.bbox == (0, 0, 10, 10), "the bounding box is the extent")
    ring = [(500 + 400 * math.cos(i * math.pi / 100) * (1 + (i % 7) / 20.0),
             500 + 400 * math.sin(i * math.pi / 100) * (1 + (i % 5) / 20.0))
            for i in range(200)]
    blob = Zone("AE", "", [ring, [(480, 480), (520, 480), (520, 520),
                                  (480, 520)]])
    all_edges = edges_of(blob.rings)
    points = [(gx, gy) for gx in range(-300, 1400, 97)
              for gy in range(-300, 1400, 89)]
    outside = [pt for pt in points
               if edge_state(pt[0], pt[1], all_edges) == OUTSIDE]
    bad_state = [pt for pt in points if blob.state(*pt)
                 != edge_state(pt[0], pt[1], all_edges)]
    bad_distance = [pt for pt in outside if abs(
        blob.distance(*pt) - min(seg_distance(pt[0], pt[1], *e)
                                 for e in all_edges)) > 1e-9]
    check(blob.bands > 1 and 0 < len(outside) < len(points)
          and not bad_state and not bad_distance,
          "the banded index gives the same state and distance as a scan of "
          "every edge, at %d points around a %d-edge polygon with a hole  "
          "<-- pinned defect" % (len(points), len(all_edges)))
    flat = Zone("AE", "", [[(0, 0), (5, 0), (10, 0)]])
    check(flat.state(5, 0) == EDGE and flat.state(5, 1) == OUTSIDE
          and flat.distance(5, 3) == 3.0,
          "a polygon with no height is read without a division by zero")
    two = Vintage("v", [Zone("AE", "", [[(0, 0), (0, 10), (10, 10),
                                         (10, 0)]]),
                        Zone("AE", "", [[(100, 0), (100, 10), (110, 10),
                                         (110, 0)]])], None, False)
    two.index(False)
    check(two.distance_to(("AE", ""), 12, 5) == 2.0,
          "the nearest polygon of a key wins, and a far one is skipped")
    check(math.isinf(two.distance_to(("X", ""), 12, 5)),
          "a key the vintage does not have is infinitely far")
    check(Vintage("v", [], None, False).bounds() is None,
          "an empty vintage has no extent")
    check("at most 30.0 ft" in classify_shift(1.0)[1]
          and "a real change" in classify_shift(500.0)[1]
          and "neither a re-trace" in classify_shift(50.0)[1],
          "each class gives its reason in words")

    # ---- the feature sets
    check(sr_ids({"wkid": 102658, "latestWkid": 2236})
          & sr_ids({"wkid": 2236}) == {"wkid:2236"},
          "102658 with latestWkid 2236 and a plain 2236 share an identifier")
    check(sr_ids(None) == set() and sr_ids({"wkt": "PROJCS[ x ]"})
          == {"wkt:PROJCS[ x ]"}
          and sr_ids({"wkt": "A  B"}) == sr_ids({"wkt": "A B"}),
          "a missing spatialReference gives no id and a wkt is compared with "
          "its spaces folded")

    def refused(fn, label_):
        raises(fn, label_, Refused)

    refused(lambda: parse_vintage([], "v"), "a list is not a feature set")
    refused(lambda: parse_vintage({"features": 3}, "v"),
            "a feature set without a features list is refused")
    refused(lambda: parse_vintage(fset([], extra={"exceededTransferLimit":
                                                  True}), "v"),
            "exceededTransferLimit true is refused, the server cut the "
            "answer short  <-- pinned defect")
    check(len(parse_vintage(fset([], extra={"exceededTransferLimit": False}),
                            "v").zones) == 0,
          "exceededTransferLimit false is a whole answer")
    refused(lambda: parse_vintage(fset([5]), "v"),
            "a feature that is not an object is refused")
    raises(lambda: parse_vintage(fset([{"attributes": {"OTHER": "AE"},
                                        "geometry": {"rings": [rect(0, 0, 1,
                                                                    1)]}}]),
                                 "v"),
           "a feature without the zone field is a usage error naming the "
           "fields it has")
    refused(lambda: parse_vintage(fset([feat("  ", [rect(0, 0, 1, 1)])]),
                                  "v"),
            "a blank zone is refused, it names no zone")
    refused(lambda: parse_vintage(fset([feat(None, [rect(0, 0, 1, 1)])]),
                                  "v"),
            "a null zone is refused too")
    bad_geometry = [None, {}, {"rings": []}, {"rings": [5]},
                    {"rings": [[[0, 0], [1, 1]]]},
                    {"rings": [[[0, 0], [1, 1], [2]]]},
                    {"rings": [[[0, 0], [1, 1], [float("nan"), 2]]]},
                    {"rings": [[[0, 0], [1, 1], ["a", 2]]]},
                    {"rings": [[[0, 0], [1, 1], 7]]}, [rect(0, 0, 1, 1)]]
    for geometry in bad_geometry:
        refused(lambda g=geometry: parse_vintage(
            fset([{"attributes": {"FLD_ZONE": "AE"}, "geometry": g}]), "v"),
            "a geometry of %s is refused" % json.dumps(geometry)[:40])
    v = parse_vintage({"features": [{"attributes": {"FLD_ZONE": "AE",
                                                    "ZONE_SUBTY": None},
                                     "geometry": {"rings": [rect(0, 0, 1, 1)],
                                                  "spatialReference":
                                                  {"wkid": 2236}}}]}, "v")
    check(v.sr == {"wkid": 2236} and v.has_subtype
          and v.zones[0].subtype == "",
          "the spatialReference is read from a geometry when the set has "
          "none, and a null subtype is blank")

    # ---- two vintages that cannot be compared
    refused(lambda: pair(old_world, new_moved, new_wkid=3857),
            "vintages in 2236 and 3857 are refused  <-- pinned defect")
    fema_style = {"wkid": 102658, "latestWkid": 2236}
    o = parse_vintage(fset(old_world, extra={"spatialReference": fema_style}),
                      "old")
    n = parse_vintage(fset(new_moved), "new")
    check(not any("spatialReference" in x for x in settle(o, n)),
          "102658 with latestWkid 2236 against 2236 is the same system, "
          "said nothing about it")
    o = parse_vintage({"features": old_world}, "old")
    n = parse_vintage(fset(new_moved), "new")
    check(any("declares no spatialReference" in x for x in settle(o, n)),
          "a vintage with no spatialReference is noted, not refused")
    far = [feat("AE", [rect(10 ** 6, 0, 10 ** 6 + 5, 5)])]
    refused(lambda: pair(old_world, far),
            "vintages whose extents do not touch are refused")
    refused(lambda: pair(old_world, [feat("AE", [rect(0, 10 ** 6, 5,
                                                      10 ** 6 + 5)])]),
            "and so are vintages that overlap in x but not in y")
    o, n, _ = pair([], [])
    check(compare([Parcel("P", 1, 1, 2)], o, n).compared == 0,
          "two empty vintages are legal and compare nothing")
    o, n, _ = pair([], new_moved)
    check(gate(compare([Parcel("P", 1, 1, 2)], o, n)) == 4,
          "one empty vintage compares nothing, which is exit 4")

    # ---- the report
    o, n, _ = pair(old_world + [feat("AH", [rect(0, 1000, 2000, 2000)])],
                   [feat("AE", [rect(0, 0, 1015, 1000)]),
                    feat("X", [rect(1015, 0, 2000, 1000)]),
                    feat("AH", [rect(0, 1000, 2000, 1060)]),
                    feat("X", [rect(0, 1060, 2000, 2000)])])
    parcels = [Parcel("R-1", 1010, 500, 2), Parcel("C-1", 1500, 1500, 3),
               Parcel("V-1", 1500, 1070, 4), Parcel("S-1", 500, 500, 5),
               Parcel("U-1", 9000, 9000, 6), Parcel("U-2", None, None, 7),
               Parcel("", 1011, 500, 8)]
    rep = compare(parcels, o, n)
    counts = dict((c, rep.count(c)) for c in CLASSES)
    check(counts == {STABLE: 1, RETRACED: 2, REVIEW: 1, CHANGED: 1,
                     UNVERIFIED: 2}, "each parcel lands in exactly one class")
    check(sum(counts.values()) == len(parcels) and rep.differing == 4
          and rep.compared == 5,
          "the classes add up to the parcels read, and a plain overlay would "
          "report 4 changes")
    check(gate(rep) == 1, "a parcel in the review band is exit 1")
    check(gate(compare([p for p in parcels if p.parcel_id != "V-1"], o, n))
          == 0, "without it the run is exit 0")
    check(gate(compare(parcels[4:6], o, n)) == 4,
          "parcels that were all UNVERIFIED are exit 4, not a clean run")
    check(gate(compare([], o, n)) == 4, "no parcels at all is exit 4 as well")
    text = "\n".join(describe(rep))
    check("a plain overlay reports 4 parcel(s) changed zone" in text
          and "RETRACED" in text and "R-1" in text and "(no id)" in text,
          "the report names the plain count and each class")
    check("zone changes the geometry confirms" in text
          and "C-1  AH to X, 500.0 ft" in text,
          "the report lists the real change with its shift")
    check("in the review band" in text.splitlines()[-1],
          "the verdict names the review band")
    text = "\n".join(describe(compare([p for p in parcels
                                       if p.parcel_id != "V-1"], o, n)))
    check(text.splitlines()[-1] == "VERDICT: 1 parcel(s) changed zone. 2 of "
          "the 3 zone difference(s) were a re-traced line and are not "
          "changes.", "the clean verdict counts the real changes")
    quiet = "\n".join(describe(compare([parcels[3]], o, n)))
    check("0 parcel(s) changed zone. 0 of the 0" in quiet
          and "zone changes the geometry" not in quiet,
          "a run with no difference lists no section")
    check("... and 1 more" in "\n".join(describe(rep, sample=1))
          and "... and 2 more" in "\n".join(describe(rep, sample=0)),
          "a short sample cuts the longer lists and says so")
    many = compare([Parcel("U%d" % i, None, None, i) for i in range(4)]
                   + [parcels[3]], o, n)
    check("... and 3 more" in "\n".join(describe(many, 1)),
          "the parcels with no answer are cut by the sample too")
    nothing = "\n".join(describe(compare(parcels[4:6], o, n), 10, " HINT"))
    check(nothing.splitlines()[-1].startswith("VERDICT: NOTHING COMPARED")
          and nothing.endswith("HINT"),
          "a run that compared nothing says so on the verdict line")
    rows = classified_rows(rep)
    check([r["parcel_id"] for r in rows] == [p.parcel_id for p in parcels]
          and rows[1]["shift"] == "500.000" and rows[0]["shift"] == "10.000"
          and rows[3]["shift"] == "" and rows[5]["x"] == ""
          and rows[0]["old_zone"] == "X" and rows[4]["old_zone"] == "",
          "the output has one row per parcel, in file order")
    gone = compare([Parcel("P", 990, 500, 2)], *pair(
        old_world, [feat("X", [rect(0, 0, 2000, 1000)])])[:2])
    check(classified_rows(gone)[0]["shift"] == ""
          and "does not exist" in classified_rows(gone)[0]["reason"]
          and "no such zone" in "\n".join(describe(gone)),
          "an infinite shift is written blank and printed in words")
    raises(lambda: compare([], o, n, 50.0, 10.0),
           "compare refuses a change distance under the retrace distance")

    # ---- the files
    def tmpfile(name, text, mode="w"):
        path = os.path.join(tmp, name)
        if mode == "wb":
            with open(path, "wb") as fh:
                fh.write(text)
        else:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                fh.write(text)
        return path

    def run_cli(argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    world_old = old_world + [feat("AH", [rect(0, 1000, 2000, 2000)])]
    world_new = [feat("AE", [rect(0, 0, 1015, 1000)]),
                 feat("X", [rect(1015, 0, 2000, 1000)]),
                 feat("AH", [rect(0, 1000, 2000, 1060)]),
                 feat("X", [rect(0, 1060, 2000, 2000)])]
    old_json = tmpfile("old.json", json.dumps(fset(world_old)))
    new_json = tmpfile("new.json", json.dumps(fset(world_new, extra={
        "spatialReference": fema_style})))
    csv_text = ("PARCEL_ID,X,Y\nR-1,1010,500\nC-1,1500,1500\n"
                "V-1,1500,1070\nS-1,500,500\nU-1,9000,9000\nU-2,,\n")
    parcels_csv = tmpfile("parcels.csv", csv_text)
    base = [old_json, new_json, parcels_csv]
    code, out, err = run_cli(base)
    check(code == 1 and err == "" and "VERDICT: 1 parcel(s) in the review "
          "band" in out and "floodshift: " in out,
          "the whole CLI on the synthetic layers ends at exit 1 for the "
          "review parcel")
    check("R-1  X to AE, 10.0 ft, line 2" in out
          and "C-1  AH to X, 500.0 ft, line 3" in out
          and "V-1  AH to X, 70.0 ft, line 4" in out,
          "and prints each parcel with its zones, shift and file line")
    check("Check only" not in out, "no --out, no note about writing")
    check("is not in both vintages" in out, "the layers carry no subtype, "
          "and the run says the key is the zone letter alone")
    code, out, err = run_cli(base + ["--subtype-field", ""])
    check(code == 1 and "is not in both vintages" not in out,
          "--subtype-field with an empty value turns the subtype off, and "
          "then there is nothing to say")
    code, out, err = run_cli(base + ["--change-distance", "60",
                                     "--units", "m"])
    check(code == 0 and "VERDICT: 2 parcel(s) changed zone" in out
          and "70.0 m" in out,
          "a change distance of 60 makes the 70 unit parcel CHANGED, and "
          "--units relabels without converting")
    code, out, err = run_cli(base + ["--retrace-distance", "5",
                                     "--change-distance", "5"])
    check(code == 0 and "R-1  X to AE, 10.0 ft" in out
          and "VERDICT: 3 parcel(s) changed zone. 0 of the 3" in out,
          "a retrace distance of 5 turns the 10 unit parcel into a change")
    renamed = tmpfile("renamed.csv", csv_text.replace("PARCEL_ID,X,Y",
                                                      "APN,POINT_X,POINT_Y"))
    code, out, err = run_cli([old_json, new_json, renamed, "--id-field", "APN",
                              "--x-field", "POINT_X", "--y-field", "POINT_Y"])
    check(code == 1 and "R-1  X to AE, 10.0 ft, line 2" in out,
          "--id-field, --x-field and --y-field read the columns an Add "
          "Geometry Attributes export names")
    zones_out = os.path.join(tmp, "zones.csv")
    code, out, err = run_cli(base + ["--out", zones_out])
    check(code == 1 and "Check only. " in out and not os.path.exists(
        zones_out), "--out without --apply writes nothing  <-- pinned defect")
    code, out, err = run_cli(base + ["--out", zones_out, "--apply"])
    with open(zones_out, newline="", encoding="utf-8") as fh:
        written = list(csv.DictReader(fh))
    check(code == 1 and ("wrote " + zones_out) in out and len(written) == 6
          and written[0]["class"] == RETRACED
          and tuple(written[0].keys()) == OUT_COLUMNS,
          "--apply writes one row per parcel with the class")
    for flag in ("--ap", "--appl", "--ou", "--out-"):
        err_buf, refusal = io.StringIO(), False
        with contextlib.redirect_stderr(err_buf):
            try:
                _parse(base + [flag] + ([zones_out] if "ou" in flag else []))
            except SystemExit:
                refusal = True
        check(refusal and "unrecognized arguments" in err_buf.getvalue(),
              "a prefix of a flag, %s, is refused and not read as the full "
              "flag%s" % (flag, "  <-- pinned defect" if flag == "--ap"
                          else ""))
    skip = os.path.join(tmp, "skip.csv")
    code, out, err = run_cli(base + ["--ap", "--out", skip])
    check(code == 64 and not os.path.exists(skip),
          "main turns a refused prefix into exit 64 and writes nothing")
    check(_parse(base).apply is False and _parse(base).projected is False,
          "--apply and --projected default to off")
    check(run_cli(["--help"])[0] == 0, "--help exits 0")
    check(run_cli([])[0] == 64 and "required" in run_cli([])[2],
          "no files is exit 64")
    check(run_cli(base + ["--sample", "-1"])[0] == 64,
          "a negative --sample is exit 64")
    check(run_cli(base + ["--apply"])[0] == 64,
          "--apply without --out is exit 64")
    for victim in base:
        code, out, err = run_cli(base + ["--out", victim, "--apply"])
        check(code == 64 and "also an input" in err,
              "--out naming %s is refused" % os.path.basename(victim))
    with open(parcels_csv, encoding="utf-8") as fh:
        check(fh.read() == csv_text,
              "and the input is untouched  <-- pinned defect")
    code, out, err = run_cli(base + ["--retrace-distance", "50",
                                     "--change-distance", "10"])
    check(code == 64 and "retrace distance" in err,
          "a change distance under the retrace distance is exit 64")
    code, out, err = run_cli(base + ["--zone-field", "ZONE"])
    check(code == 64 and "has no field ZONE" in err,
          "a wrong zone field is exit 64 naming the fields there")
    code, out, err = run_cli(base + ["--id-field", "NOPE"])
    check(code == 64 and "columns not in the CSV: NOPE" in err,
          "a wrong parcel column is exit 64 naming the header")
    empty_csv = tmpfile("empty.csv", "")
    code, out, err = run_cli([old_json, new_json, empty_csv])
    check(code == 64 and "no columns at all" in err,
          "a CSV with no header is exit 64")
    code, out, err = run_cli([old_json, new_json, os.path.join(tmp, "no.csv")])
    check(code == 2 and "no.csv" in err, "a missing file is exit 2")
    huge = tmpfile("huge.csv", "PARCEL_ID,X,Y\n" + "A" * 200000 + ",1,1\n")
    code, out, err = run_cli([old_json, new_json, huge])
    check(code == 2, "a cell over the csv limit is exit 2, not a crash")
    code, out, err = run_cli([old_json, new_json,
                              tmpfile("none.csv", "PARCEL_ID,X,Y\n")])
    check(code == 4 and "NOTHING COMPARED" in out and "Check that the "
          "parcels" not in out,
          "a parcel file with a header and no rows is exit 4")
    code, out, err = run_cli([old_json, new_json, tmpfile(
        "far.csv", "PARCEL_ID,X,Y\nA,90000,90000\nB,80000,70000\n")])
    check(code == 4 and "same coordinate system as the zones" in out,
          "parcels nowhere near the zones are exit 4 with a hint  "
          "<-- pinned defect")
    code, out, err = run_cli([old_json, new_json, tmpfile(
        "blank.csv", "PARCEL_ID,X,Y\nA,,\n")])
    check(code == 4 and "same coordinate system" not in out,
          "parcels with no coordinates at all get no hint")

    # ---- what a refusal looks like
    def refuse(argv, fragment, label_):
        code, out, err = run_cli(argv)
        check(code == 3 and "REFUSED" in out and fragment in out
              and out.rstrip().endswith("VERDICT: REFUSED"), label_)

    dup = tmpfile("dup.csv",
                  "PARCEL_ID,X,Y\nA,1,1\nB,2,2\nA,3,3\n,1,1\n,2,2\n")
    refuse([old_json, new_json, dup], "'A' is on lines 2, 4",
           "a parcel id on two lines is refused and names the lines  "
           "<-- pinned defect")
    cut = tmpfile("cut.json", json.dumps(fset(world_new, extra={
        "exceededTransferLimit": True})))
    refuse([old_json, cut, parcels_csv], "exceededTransferLimit",
           "a vintage the server cut short is refused through the CLI")
    other_sr = tmpfile("sr.json", json.dumps(fset(world_new, 3857)))
    refuse([old_json, other_sr, parcels_csv], "different coordinate systems",
           "vintages in different systems are refused through the CLI")
    notjson = tmpfile("notjson.json", "{not json")
    refuse([old_json, notjson, parcels_csv], "not valid JSON",
           "a file that is not JSON is refused")
    bom_json = tmpfile("bom.json", chr(0xFEFF) + json.dumps(fset(world_new)))
    check(run_cli([old_json, bom_json, parcels_csv])[0] == 1,
          "a JSON file with a BOM is read  <-- pinned defect")
    deg_old = tmpfile("deg_old.json", json.dumps(fset([
        feat("AE", [rect(-81.3, 29.0, -81.2, 29.2)])], 4269)))
    deg_new = tmpfile("deg_new.json", json.dumps(fset([
        feat("X", [rect(-81.3, 29.0, -81.2, 29.2)])], 4269)))
    deg_csv = tmpfile("deg.csv", "PARCEL_ID,X,Y\nA,-81.25,29.1\n")
    refuse([deg_old, deg_new, deg_csv], "look like degrees",
           "layers that all fit longitude and latitude are refused, as the "
           "FEMA layer is stored  <-- pinned defect")
    code, out, err = run_cli([deg_old, deg_new, deg_csv, "--projected"])
    check(code == 0 and "VERDICT: 1 parcel(s) changed zone" in out,
          "--projected says the small numbers are a local grid")
    check(not looks_like_degrees([], []),
          "no coordinates at all do not look like degrees")

    # ---- ragged and odd csv files
    odd = tmpfile("odd.csv", chr(0xFEFF) + "PARCEL_ID,X,Y\n\nP-1,1010,500\n"
                  "\"P\n-2\",500,500\nshort\nP-4,1,2,3,4\nP-5,nan,5\n"
                  "P-6,inf,5\nP-7,abc,5\n")
    ps = read_parcels(odd, {"id": "PARCEL_ID", "x": "X", "y": "Y"})
    check([p.line for p in ps] == [3, 4, 6, 7, 8, 9, 10],
          "the line of a parcel is where it starts, past a blank line and a "
          "quoted newline  <-- pinned defect")
    check(ps[0].parcel_id == "P-1" and ps[2].x is None
          and ps[3].x == 1.0 and ps[4].x is None and ps[5].x is None
          and ps[6].x is None,
          "a BOM, a short row, a long row, NaN, infinity and text are read "
          "without a crash")
    check(to_number(None) is None and to_number("  ") is None
          and to_number(" 3.5 ") == 3.5 and to_number("1e400") is None,
          "to_number reads blank, spaced and overflowing cells")
    check(not _is_finite("x") and not _is_finite(None) and _is_finite(1),
          "_is_finite reads text and None as not a number")
    two_blank = tmpfile("blankid.csv", "PARCEL_ID,X,Y\n,1,1\n,2,2\n")
    check(len(read_parcels(two_blank, {"id": "PARCEL_ID", "x": "X",
                                       "y": "Y"})) == 2,
          "two parcels with no id are not a duplicate id")
    latin = tmpfile("latin.csv", b"PARCEL_ID,X,Y\nP\xff1,1010,500\n", "wb")
    wrapper = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    real_out = sys.stdout
    sys.stdout = wrapper
    try:
        code = main([old_json, new_json, latin])
    finally:
        wrapper.flush()
        sys.stdout = real_out
    shown = wrapper.buffer.getvalue().decode("cp1252")
    check(code == 0 and "VERDICT" in shown and "P?1" in shown,
          "an id the console cannot encode prints as ? and the verdict still "
          "arrives  <-- pinned defect")
    calls = []

    class Stream(object):
        def reconfigure(self, **kw):
            calls.append(kw)

    probe_out, probe_err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = Stream()
    try:
        _tolerate_odd_characters()
    finally:
        sys.stdout, sys.stderr = probe_out, probe_err
    check(calls == [{"errors": "replace"}] * 2,
          "both streams are told to replace what they cannot encode")

    # ---- a write that fails, and a crash
    code, out, err = run_cli(base + ["--out", tmp, "--apply"])
    check(code == 2 and "could not write" in err,
          "a write that fails is exit 2, never the 1 that means review")
    real_compare = globals()["compare"]

    def boom(*a, **k):
        raise RuntimeError("boom")

    globals()["compare"] = boom
    try:
        code, out, err = run_cli(base)
    finally:
        globals()["compare"] = real_compare
    check(code == 70 and "unexpected RuntimeError" in err,
          "an unexpected exception exits 70, not the 1 that means review  "
          "<-- pinned defect")
    real_self_test = globals()["self_test"]
    globals()["self_test"] = lambda: 7
    try:
        status = main(["--self-test"])
    finally:
        globals()["self_test"] = real_self_test
    check(status == 7,
          "main returns the exit status of --self-test, so a red self-test "
          "fails CI  <-- pinned defect")

    # ---- claims about the tool itself
    import socket
    real_socket = socket.socket

    socket.socket = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("the tool opened a socket"))
    try:
        code, out, err = run_cli(base)
    finally:
        socket.socket = real_socket
    check(code == 1 and err == "",
          "a full run with socket.socket disabled still works, the tool "
          "opens no connection")
    with open(os.path.abspath(__file__), "rb") as fh:
        source = fh.read()
    check(all(byte < 128 for byte in bytearray(source)),
          "the source is pure ASCII")

    # ---- the harness itself
    quiet_out = sys.stdout
    sys.stdout = io.StringIO()
    mark = len(failed)
    try:
        check(False, "probe: a false condition must be recorded as a failure")
        raises(lambda: None, "probe: a call that raises nothing must fail")
        raises(lambda: [][0], "probe: the wrong exception must fail")
    finally:
        sys.stdout = quiet_out
    probe = failed[mark:]
    del failed[mark:]
    check(len(probe) == 3,
          "check() and raises() really do record a failure  <-- pinned "
          "defect")
    raises(lambda: (_ for _ in ()).throw(ValueError("x")),
           "and raises() accepts the exception it is looking for")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        red = summary(1, ["probe one", "probe two"])
    check(red == 1 and "3 assertions, 2 failed" in buf.getvalue()
          and "  FAILED: probe two" in buf.getvalue(),
          "the footer reports failures by count and by name, and exits 1  "
          "<-- pinned defect")

    spec = importlib.util.spec_from_file_location(
        "floodshift_imported", os.path.abspath(__file__))
    imported = importlib.util.module_from_spec(spec)
    buf = io.StringIO()
    cache_before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        with contextlib.redirect_stdout(buf):
            spec.loader.exec_module(imported)
    finally:
        sys.dont_write_bytecode = cache_before
    check(buf.getvalue() == "" and imported.gate(imported.compare(
        [], *pair(old_world, new_moved)[:2])) == 4,
          "importing the module prints nothing and exposes the core")

    return summary(passed[0], failed)


def summary(passed_count, failures):
    print("-" * 68)
    total = passed_count + len(failures)
    if failures:
        print("%d assertions, %d failed" % (total, len(failures)))
        for f in failures:
            print("  FAILED: %s" % f)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
