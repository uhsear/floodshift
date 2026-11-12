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

Exit codes: 0 every parcel was decided (STABLE, RETRACED or CHANGED), 1 at
least one parcel is in the REVIEW band and a human has to decide it, 2 a file
could not be read or written, 3 the input was refused (it cannot support an
answer), 4 nothing was compared, 5 at least one parcel has no answer
(UNVERIFIED) and none is in the REVIEW band, 64 usage error, 70 an unexpected
crash, which is never a finding. --out names a file that does not exist yet.
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
from fractions import Fraction

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
    except (TypeError, OverflowError):
        # An integer too large for a float (over about 1e308) is no coordinate.
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
    """True when the point lies exactly on the segment a-b.

    The float cross product is the quick test. Rounding can hide a zero in
    it or invent one, so a value inside its error bound is decided again
    with exact rational arithmetic on the float inputs. A decimal coordinate
    such as 49.2333... is rarely a dyadic fraction, so the float product of
    a point that is exactly on a slanted line is often not zero.
    """
    if not (min(ax, bx) <= px <= max(ax, bx)
            and min(ay, by) <= py <= max(ay, by)):
        return False
    left, right = (bx - ax) * (py - ay), (by - ay) * (px - ax)
    if abs(left - right) > _CROSS_ERROR * (abs(left) + abs(right)):
        return False
    return ((Fraction(bx) - Fraction(ax)) * (Fraction(py) - Fraction(ay))
            == (Fraction(by) - Fraction(ay)) * (Fraction(px) - Fraction(ax)))


OUTSIDE, INSIDE, EDGE = 0, 1, 2

# Shewchuk's bound for a float 2x2 determinant is about 3.3e-16 times the sum
# of the two products; this is a little wider, which only costs a few exact
# checks.
_CROSS_ERROR = 1e-15


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
    whichever way the ring is wound. Esri documents that the even-odd
    fill rule guarantees a polygon draws correctly even if the ring
    orientation is not as described. A point exactly on an edge
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
        self.lone = ""
        self.bare = False
        self.by_key = {}
        self.gaps = set()
        self.subtyped = set()

    def index(self, use_subtype):
        """Fix the key of every zone and group the zones by it."""
        self.by_key = {}
        for zone in self.zones:
            zone.key = (zone.zone, zone.subtype if use_subtype else "")
            self.by_key.setdefault(zone.key, []).append(zone)
        # a letter with a blank polygon and a filled one in one vintage has
        # a missing value, not a hazard that has no subtype
        self.gaps = set()
        self.subtyped = set(z.zone for z in self.zones if z.subtype)
        if use_subtype:
            self.gaps = (set(z for (z, s) in self.by_key if not s)
                         & set(z for (z, s) in self.by_key if s))

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
            "resultOffset until the flag is false, join the features of "
            "every page into one list under one feature set, and read that "
            "file. A page read alone leaves zones out." % name)
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
            subtype = norm(attrs[subtype_field])
            has_subtype = has_subtype or bool(subtype)
        geometry = feat.get("geometry")
        if not sr and isinstance(geometry, dict):
            sr = geometry.get("spatialReference")
        zones.append(Zone(zone, subtype, _rings(geometry, index)))
    return Vintage(name, zones, sr, has_subtype)


def settle(old, new, subtype_field=DEFAULT_SUBTYPE_FIELD):
    """Fix the zone keys of both vintages and return the notes to print.

    A subtype is part of the key only when both vintages carry it, that is,
    when some feature of each has a non-blank value. Zone X covers both the
    shaded and the unshaded hazard areas and only the subtype tells them
    apart, and the subtype is what separates a floodway from the rest of
    zone AE. A subtype in neither vintage leaves every zone with no answer
    for a parcel that is in it in both, because the letter alone cannot say
    that no parcel moved between subtypes. A subtype present in one vintage
    and not the other would make every zone differ, so it is left out of the
    key. The vintage that has it is marked, and judge() gives no answer for a
    parcel whose zone letter matches in a zone that holds a subtype there:
    the letter alone cannot say that the hazard stayed the same.

    Raises Refused when the two vintages declare spatial references that
    share no identifier, or when their extents do not touch.
    """
    notes = []
    use_subtype = bool(subtype_field) and old.has_subtype and new.has_subtype
    if subtype_field and not use_subtype:
        note = ("NOTE      %s is missing or blank in at least one vintage, so "
                "it is left out of the zone key and a zone's subtypes, "
                "such as Zone X shaded and unshaded or an AE floodway, are "
                "not told apart" % subtype_field)
        if old.has_subtype != new.has_subtype:
            (old if old.has_subtype else new).lone = subtype_field
            note += (". A parcel in the same zone of both vintages is "
                     "UNVERIFIED where the vintage that has the subtype "
                     "gives the zone one")
        else:
            old.bare = new.bare = True
            note += (". A parcel in the same zone in both vintages is "
                     "UNVERIFIED, because only the subtype tells shaded X "
                     "from unshaded X or an AE floodway from the rest of "
                     "AE. Add %s to outFields, or pass --subtype-field "
                     "\"\" to accept the letter alone" % subtype_field)
        notes.append(note)
    old.index(use_subtype)
    new.index(use_subtype)
    if use_subtype:
        # a letter blank all through one vintage and filled in the other is
        # the same missing value, so the pair counts as a gap on the blank side
        for blank, filled in ((old, new), (new, old)):
            blank.gaps |= (set(z for (z, s) in blank.by_key if not s)
                           & set(z for (z, s) in filled.by_key if s))
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
        return RETRACED, ("the line would have to move at most %.1f %s: "
                          "counted as a moved line, not a zone change"
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
    gap = [(which, k[0]) for which, v, k in (("old", old, a), ("new", new, b))
           if not k[1] and k[0] in v.gaps]
    if gap:
        return Result(parcel, UNVERIFIED,
                      "the subtype is blank on this polygon of the %s "
                      "vintage, where zone %s has one elsewhere, so "
                      "the zone cannot be matched" % gap[0])
    if a == b:
        blind = [which for which, v in (("old", old), ("new", new))
                 if v.lone and a[0] in v.subtyped]
        if blind:
            return Result(parcel, UNVERIFIED,
                          "the same zone letter in both vintages, but only "
                          "the %s vintage has a subtype, so a move between "
                          "subtypes of this zone cannot be seen" % blind[0])
        if old.bare:
            return Result(parcel, UNVERIFIED,
                          "zone %s in both vintages, but neither vintage "
                          "has a subtype, so a move between its subtypes "
                          "(shaded and unshaded X, an AE floodway) cannot "
                          "be seen" % a[0])
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
    """The exit code for a report.

    4 nothing was compared, 1 a parcel is in the REVIEW band, 5 a parcel has
    no answer, 0 every parcel was decided. A run that could not tell for a
    parcel is never a clean run.
    """
    if report.compared == 0:
        return 4
    if report.count(REVIEW):
        return 1
    return 5 if report.count(UNVERIFIED) else 0


def _shift_text(result, units):
    if math.isinf(result.shift):
        return "no such zone in the other vintage"
    return "%.1f %s" % (result.shift, units)


def describe(report, sample=DEFAULT_SAMPLE, overlap_hint=""):
    """The printed report, as a list of lines."""
    lines = list(report.notes)
    total = len(report.results)
    lines.append("parcels read: %d, compared in both vintages: %d"
                 % (total, report.compared))
    for cls in CLASSES:
        lines.append("  %-10s %d" % (cls, report.count(cls)))
    lines.append("retrace distance %.1f %s, change distance %.1f %s"
                 % (report.retrace, report.units, report.change,
                    report.units))
    unverified = [r for r in report.results if r.cls == UNVERIFIED]

    def no_answer_section():
        if not unverified:
            return
        lines.append("")
        lines.append("parcels with no answer:")
        for r in unverified[:sample]:
            lines.append("  %s  %s, line %s"
                         % (r.parcel.parcel_id or "(no id)", r.reason,
                            r.parcel.line))
        if len(unverified) > sample:
            lines.append("  ... and %d more" % (len(unverified) - sample))

    if report.compared == 0:
        no_answer_section()
        lines.append("")
        lines.append("VERDICT: NOTHING COMPARED. No parcel could be compared "
                     "in both vintages, so the geometry checked nothing. "
                     "This is not a clean run.%s" % overlap_hint)
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
    section("zone differences within the retrace distance, counted as a moved "
            "line:", RETRACED)
    no_answer_section()
    lines.append("")
    if report.count(REVIEW):
        verdict = ("VERDICT: %d parcel(s) in the review band. Decide them "
                   "before a change count is published."
                   % report.count(REVIEW))
    elif report.differing == 0:
        verdict = ("VERDICT: 0 parcel(s) changed zone. No compared parcel "
                   "has a different zone in the two vintages.")
    else:
        verdict = ("VERDICT: %d parcel(s) changed zone. %d of the %d zone "
                   "difference(s) are within the retrace distance and are "
                   "counted as a moved line, not as a zone change."
                   % (report.count(CHANGED), report.count(RETRACED),
                      report.differing))
    if unverified:
        verdict += (" %d of %d parcel(s) have no answer and are in none of "
                    "these counts." % (len(unverified), total))
    lines.append(verdict)
    return lines


# The first characters a spreadsheet reads as a formula (OWASP, CSV Injection).
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _cell(text):
    """Text for a spreadsheet cell. A leading = + - @ tab or CR is read as a
    formula, so it gets a leading apostrophe. Numbers are not passed here."""
    return "'" + text if text[:1] in _FORMULA_LEAD else text


def _uncell(text):
    """Undo _cell: drop one apostrophe that stands before a formula lead.
    An apostrophe before anything else is part of the text and stays."""
    if text[:1] == "'" and text[1:2] in _FORMULA_LEAD:
        return text[1:]
    return text


def classified_rows(report):
    """The --out rows, one per parcel."""
    rows = []
    for r in report.results:
        rows.append({
            "parcel_id": _cell(r.parcel.parcel_id),
            "line": r.parcel.line,
            "x": "" if r.parcel.x is None else repr(r.parcel.x),
            "y": "" if r.parcel.y is None else repr(r.parcel.y),
            "old_zone": _cell(label(r.old_key)) if r.old_key else "",
            "new_zone": _cell(label(r.new_key)) if r.new_key else "",
            "class": r.cls,
            "shift": ("" if r.shift is None or math.isinf(r.shift)
                      else "%.3f" % r.shift),
            "reason": _cell(r.reason),
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
            # --out writes the id through _cell, so an id read back from it
            # loses the same apostrophe.
            parcel_id = _uncell(raw.get(fields["id"]) or "").strip()
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
    # "x" fails if the file exists, so a file made after the check in _run is
    # not overwritten either.
    with open(path, "x", newline="", encoding="utf-8") as handle:
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
    if args.out and os.path.lexists(args.out):
        # Checked on a dry run too: the run that would replace a file should
        # say so before --apply is added, not after. Any existing path is
        # refused: an input, a link to an input, a directory, a stranger's file.
        print("error: --out %s already exists. Name a file that does not "
              "exist yet." % args.out, file=sys.stderr)
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
    if report.compared == 0 and any("in no zone" in r.reason
                                    for r in report.results):
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

    ABSENT = object()

    def feat(zone, rings, subtype=ABSENT):
        attrs = {"FLD_ZONE": zone}
        if subtype is not ABSENT:
            attrs["ZONE_SUBTY"] = subtype
        return {"attributes": attrs, "geometry": {"rings": rings}}

    def fset(features, wkid=2236, extra=None):
        data = {"spatialReference": {"wkid": wkid},
                "features": features}
        data.update(extra or {})
        return data

    def pair(old_features, new_features, subtype_field=None,
             old_wkid=2236, new_wkid=2236):
        # None: the default field, or off when no feature carries it, so the
        # many geometry cases below need not say it
        if subtype_field is None:
            subtype_field = (DEFAULT_SUBTYPE_FIELD if any(
                DEFAULT_SUBTYPE_FIELD in f["attributes"]
                for f in old_features + new_features) else "")
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
    # two zones share the slanted edge (20.8, 24.5)-(74.2, 98.7); the point
    # is exactly on it, but 38.6 and 49.2333... are not dyadic fractions, so
    # the float cross product is not zero
    slant = [[20.8, 24.5], [74.2, 98.7]]
    shared = [feat("AE", [slant + [[10.0, 60.0]]]),
              feat("VE", [slant + [[70.8, 25.5]]])]
    o, n, _ = pair(shared, shared)
    px, py = 38.6, 49.233333333333334
    check((74.2 - 20.8) * (py - 24.5) - (98.7 - 24.5) * (px - 20.8) != 0.0
          and (Fraction(74.2) - Fraction(20.8)) * (Fraction(py) - Fraction(24.5))
          == (Fraction(98.7) - Fraction(24.5)) * (Fraction(px) - Fraction(20.8)),
          "the fixture point is exactly on the slanted line while its float "
          "cross product is not zero")
    r = one(o, n, px, py)
    check(r.cls == UNVERIFIED and "more than one zone of the old" in r.reason
          and "AE, VE" in r.reason,
          "a point exactly on a slanted line two zones share is UNVERIFIED "
          "although the float cross product is not zero  "
          "<-- pinned defect")
    check(one(o, n, px, math.nextafter(py, math.inf)).cls == STABLE,
          "and a point one float step off that line is not on it")
    check(not _on_edge(38.6, 49.3, 20.8, 24.5, 74.2, 98.7)
          and not _on_edge(80.0, 105.0, 20.8, 24.5, 74.2, 98.7),
          "a point well off the line, and one on its extension beyond the "
          "end, is not on the segment")
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
    check("not a zone change" in classify_shift(1.0)[1]
          and "no change" not in classify_shift(1.0)[1].replace("not a zone "
                                                                "change", ""),
          "a RETRACED reason says the line would have to move, and counts "
          "it as a moved line, it does not say nothing changed  "
          "<-- pinned defect")

    # ---- pins for what the first suite let a mutation sweep break
    hyp = [(0, 0), (1000, 0), (0, 1000), (0, 0)]
    check(ring_state(900, 900, [hyp]) == OUTSIDE
          and ring_state(500, 500, [hyp]) == EDGE
          and ring_state(100, 100, [hyp]) == INSIDE,
          "a point beside a diagonal edge is not on it, only a point exactly "
          "on the line is  <-- pinned defect")
    stub = [(5, 0), (5, 3), (10, 3), (10, 0)]
    check(ring_state(5, 4, [stub]) == OUTSIDE
          and ring_state(10, -1, [stub]) == OUTSIDE
          and ring_state(15, 0, [stub]) == OUTSIDE
          and ring_state(0, 3, [stub]) == OUTSIDE
          and ring_state(5, 3, [stub]) == EDGE,
          "a point on the extension of an edge is not on the edge, in x or "
          "in y  <-- pinned defect")
    parts = [[(1000, 1000), (1000, 1400), (1400, 1400), (1400, 1000)],
             [(1000, 1600), (1000, 2000), (1400, 2000), (1400, 1600)]]
    check(ring_state(1000, 1500, parts) == OUTSIDE
          and ring_state(1000, 1700, parts) == EDGE,
          "a point in the gap between two parts, level with both of their "
          "edges, is outside  <-- pinned defect")
    corner = [(1000, 1000), (2000, 1000), (2000, 2000)]
    check(ring_state(1050, 1100, [corner]) == OUTSIDE
          and ring_state(1900, 1500, [corner]) == INSIDE
          and ring_state(1500, 1500, [corner]) == EDGE,
          "the closing edge of a ring that is not closed is an edge of the "
          "ring  <-- pinned defect")
    diamond = [(1000, 1000), (1100, 1050), (1000, 1100), (900, 1050)]
    check(ring_state(1030, 1050, [diamond]) == INSIDE
          and ring_state(920, 1050, [diamond]) == INSIDE
          and ring_state(1150, 1050, [diamond]) == OUTSIDE
          and ring_state(880, 1050, [diamond]) == OUTSIDE,
          "a point level with a vertex is judged the same from both sides of "
          "it  <-- pinned defect")
    elbow = [[1000, 0], [2000, 0], [2000, 1000], [1900, 1000], [1900, 100],
             [1000, 100]]
    nick = [rect(1505, 480, 1520, 520)]
    moved = [feat("X", [rect(0, 0, 1495, 1000)]),
             feat("AE", [rect(1495, 0, 2000, 1000)])]
    for order in ((nick, [elbow]), ([elbow], nick)):
        o, n, _ = pair([feat("X", [rect(0, 0, 2000, 1000)]),
                        feat("AE", order[0]), feat("AE", order[1])], moved)
        r = one(o, n, 1500, 500)
        check(r.cls == RETRACED and r.shift == 5.0,
              "of two polygons of one zone the nearer counts, whichever is "
              "read first: the elbow is 400 away, the nick 5  "
              "<-- pinned defect")
    o, n, notes = pair([feat("X", [rect(0, 0, 2000, 1000)])],
                       [feat("X", [rect(0, 0, 2000, 1000)], minimal)])
    r = one(o, n, 500, 500)
    check(r.cls == UNVERIFIED and len(notes) == 1
          and "only the new vintage has a subtype" in r.reason,
          "a subtype in one vintage only does not make the same zone two "
          "zones, and the zone letter alone is no answer  <-- pinned defect")
    o, n, notes = pair([feat("X", [rect(0, 0, 2000, 1000)], minimal)],
                       [feat("X", [rect(0, 0, 2000, 1000)])])
    check(one(o, n, 500, 500).cls == UNVERIFIED
          and "only the old vintage has a subtype" in one(o, n, 500, 500
                                                          ).reason,
          "and the same when the old vintage is the one that has it")
    o, n, notes = pair([feat("AE", [rect(0, 0, 1000, 1000)], ""),
                        feat("X", [rect(1000, 0, 2000, 1000)], "")],
                       [feat("AE", [rect(0, 0, 1000, 1000)], ""),
                        feat("X", [rect(1000, 0, 2000, 1000)], minimal)])
    check(one(o, n, 500, 500).cls == STABLE
          and one(o, n, 1500, 500).cls == UNVERIFIED and len(notes) == 1
          and "UNVERIFIED" in notes[0],
          "a zone with no subtype in the vintage that has the field is still "
          "STABLE, and the note says what is UNVERIFIED  <-- pinned defect")
    o, n, notes = pair([feat("X", [rect(0, 0, 2000, 1000)], None)],
                       [feat("X", [rect(0, 0, 2000, 1000)], shaded)])
    check(one(o, n, 500, 500).cls == UNVERIFIED and len(notes) == 1,
          "a subtype field that is blank on every feature of the older "
          "vintage is no subtype, so a filled newer one is no false CHANGED  "
          "<-- pinned defect")
    o, n, notes = pair([feat("AE", [rect(0, 0, 1000, 1000)], ""),
                        feat("X", [rect(1000, 0, 2000, 1000)], minimal)],
                       [feat("AE", [rect(0, 0, 1000, 1000)], ""),
                        feat("X", [rect(1000, 0, 2000, 1000)], ""),
                        feat("X", [rect(2000, 0, 3000, 1000)], shaded)])
    r = one(o, n, 1500, 500)
    check(r.cls == UNVERIFIED and not notes and "blank on this polygon of "
          "the new vintage" in r.reason and one(o, n, 500, 500).cls == STABLE,
          "one polygon with a blank subtype in a vintage that carries it "
          "elsewhere for that zone is no answer, not a false CHANGED, while "
          "a zone that has no subtype anywhere stays STABLE  "
          "<-- pinned defect")
    r = one(n, o, 1500, 500)
    check(r.cls == UNVERIFIED and "of the old vintage" in r.reason,
          "and the same when the blank polygon is in the old vintage")
    lone_rows = [feat("X", [rect(0, 0, 1000, 1000)], minimal),
                 feat("X", [rect(1000, 0, 2000, 1000)], ""),
                 feat("AE", [rect(2000, 0, 3000, 1000)], "")]
    bare_rows = [feat("X", [rect(0, 0, 2000, 1000)]),
                 feat("AE", [rect(2000, 0, 3000, 1000)])]
    for lone_first in (True, False):
        o, n, notes = (pair(lone_rows, bare_rows) if lone_first
                       else pair(bare_rows, lone_rows))
        side = lambda x: one(o, n, x, 500)
        check(len(notes) == 1 and side(500).cls == UNVERIFIED
              and side(1500).cls == UNVERIFIED and side(2500).cls == STABLE,
              "a blank polygon of a zone that has a subtype elsewhere in the "
              "only vintage that carries one is no answer, as the filled "
              "polygon is, and a zone with no subtype there stays STABLE  "
              "<-- pinned defect")
    o, n, notes = pair([feat("AE", [rect(0, 0, 1000, 1000)], "FLOODWAY"),
                        feat("X", [rect(1000, 0, 2000, 1000)], minimal)],
                       [feat("AE", [rect(0, 0, 1000, 1000)], "FLOODWAY"),
                        feat("X", [rect(1000, 0, 2000, 1000)], "")])
    r = one(o, n, 1500, 500)
    check(r.cls == UNVERIFIED and not notes and "blank on this polygon of "
          "the new vintage" in r.reason and one(o, n, 500, 500).cls == STABLE,
          "a zone letter blank on every polygon of the new vintage and "
          "filled in the old is no answer, not a false CHANGED, while "
          "another zone stays STABLE  <-- pinned defect")
    r = one(n, o, 1500, 500)
    check(r.cls == UNVERIFIED and "of the old vintage" in r.reason,
          "and the same when the vintage that is blank all through is the "
          "old one")
    o, n, notes = pair(old_world, old_world, DEFAULT_SUBTYPE_FIELD)
    r = one(o, n, 1500, 500)
    check(r.cls == UNVERIFIED and "neither vintage has a subtype" in r.reason
          and len(notes) == 1 and "the same zone in both vintages is "
          "UNVERIFIED" in notes[0] and "--subtype-field" in notes[0],
          "a subtype in neither vintage leaves a parcel that is X in both "
          "with no answer, as shaded and unshaded X look alike, and the "
          "note says how to fix it  <-- pinned defect")
    r = one(o, n, 500, 500)
    check(r.cls == UNVERIFIED and "zone AE in both vintages" in r.reason
          and "AE floodway" in r.reason and "AE floodway" in notes[0],
          "and a parcel that is AE in both is no answer either, as a floodway "
          "inside AE looks like the rest of AE without a subtype and the "
          "note names it  <-- pinned defect")
    o, n, notes = pair(old_world, old_world, "")
    check(one(o, n, 1500, 500).cls == STABLE and notes == [],
          "with the subtype turned off the same X parcel is STABLE, said "
          "nothing")
    o, n, notes = pair([feat("AE", [rect(0, 0, 1000, 1000)], None),
                        feat("X", [rect(1000, 0, 2000, 1000)], None)],
                       [feat("AE", [rect(0, 0, 1015, 1000)], ""),
                        feat("X", [rect(1015, 0, 2000, 1000)], "  ")])
    check(one(o, n, 1500, 500).cls == UNVERIFIED and len(notes) == 1
          and one(o, n, 1010, 500).cls == RETRACED,
          "a subtype field blank on every feature of both vintages is no "
          "subtype, and a zone that differs by letter is still judged by "
          "its shift  <-- pinned defect")
    sweep_old = [feat("AE", [rect(0, 0, 2000, 1000)]),
                 feat("X", [rect(0, 1100, 2000, 1200)])]
    sweep_new = [feat("AE", [rect(0, 0, 1000, 1000)]),
                 feat("X", [rect(1000, 0, 2000, 1000)])]
    o, n, _ = pair(sweep_old, sweep_new)
    ranked = compare([Parcel("SMALL", 1150, 500, 2),
                      Parcel("B-late", 1900, 500, 5),
                      Parcel("B-early", 1900, 501, 3)], o, n)
    check([r_.parcel.parcel_id for r_ in ranked.results
           if r_.cls == CHANGED] == ["SMALL", "B-late", "B-early"],
          "all three are CHANGED, the 600 away one first in the file")
    listed = [x.split()[0] for x in describe(ranked) if " AE to X" in x]
    check(listed == ["B-early", "B-late", "SMALL"],
          "the changes are listed worst first, a tie by file line  "
          "<-- pinned defect")
    def shown(rep_, sample):
        text_ = describe(rep_, sample)
        return (len([x for x in text_ if " AE to X" in x]),
                [x for x in text_ if x.startswith("  ... and")])

    check(shown(ranked, 2) == (2, ["  ... and 1 more"])
          and shown(ranked, 3) == (3, [])
          and shown(ranked, 0) == (0, ["  ... and 3 more"])
          and shown(ranked, 9) == (3, []),
          "--sample lists exactly that many and says how many more, and "
          "says nothing at an exact fit  <-- pinned defect")
    none_ = compare([Parcel("U%d" % i, 9000 + i, 9000, i) for i in range(4)]
                    + [Parcel("S", 500, 500, 9)], o, n)

    def unanswered(sample):
        text_ = describe(none_, sample)
        return (len([x for x in text_ if x.startswith("  U") and "line" in x]),
                [x for x in text_ if x.startswith("  ... and")])

    check(unanswered(2) == (2, ["  ... and 2 more"])
          and unanswered(4) == (4, []) and unanswered(0) == (0,
                                                             ["  ... and 4 "
                                                              "more"]),
          "--sample cuts the parcels with no answer to exactly that many  "
          "<-- pinned defect")
    for dx, dy in ((10, 0), (-10, 0), (0, 10), (0, -10)):
        o, n, _ = pair([feat("AE", [rect(0, 0, 10, 10)])],
                       [feat("AE", [rect(dx, dy, dx + 10, dy + 10)])])
        check(o.bounds() is not None and n.bounds() is not None,
              "vintages that only touch, at (%d, %d), are one place and are "
              "not refused  <-- pinned defect" % (dx, dy))
    for dx, dy in ((11, 0), (-11, 0), (0, 11), (0, -11)):
        raises(lambda dx=dx, dy=dy: pair(
            [feat("AE", [rect(0, 0, 10, 10)])],
            [feat("AE", [rect(dx, dy, dx + 10, dy + 10)])]),
            "vintages 1 apart at (%d, %d) are refused" % (dx, dy), Refused)
    wide = parse_vintage(fset([feat("AE", [rect(-180, -1, 180, 1)])]), "v")
    tall = parse_vintage(fset([feat("AE", [rect(-1, -90, 1, 90)])]), "v")
    over = parse_vintage(fset([feat("AE", [rect(-1, -1, 1, 90.5)])]), "v")
    check(looks_like_degrees([wide], []) and looks_like_degrees([tall], [])
          and not looks_like_degrees([over], []),
          "a coordinate of exactly 180 or 90 still fits degrees and one past "
          "it does not  <-- pinned defect")
    check(not _is_finite(10 ** 400) and not _is_finite(-10 ** 400),
          "an integer too large for a float is not a coordinate  "
          "<-- pinned defect")

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
                    {"rings": [[[0, 0], [1, 1], 7]]}, [rect(0, 0, 1, 1)],
                    {"rings": [[[0, 0], [1, 1], [10 ** 400, 2]]]},
                    {"rings": [[[0, 0], [1, 1], [2, float("nan")]]]},
                    {"rings": [[[0, 0], [1, 1], [2, float("inf")]]]}]
    for geometry in bad_geometry:
        refused(lambda g=geometry: parse_vintage(
            fset([{"attributes": {"FLD_ZONE": "AE"}, "geometry": g}]), "v"),
            "a geometry of %s is refused" % json.dumps(geometry)[:40])
    curve = [[0, 0], [1, 1], [2, 0]]
    refused(lambda: parse_vintage(fset([{"attributes": {"FLD_ZONE": "AE"},
                                         "geometry": {"curveRings": [curve]}}]),
                                  "v"),
            "a geometry with curveRings and no rings is refused")
    both = parse_vintage(fset([{"attributes": {"FLD_ZONE": "AE"},
                                "geometry": {"rings": [rect(0, 0, 1, 1)],
                                             "curveRings": [curve]}}]), "v")
    check(len(both.zones) == 1 and both.zones[0].rings == [
          [(float(x), float(y)) for x, y in rect(0, 0, 1, 1)]],
          "a geometry with both rings and curveRings is read from rings and "
          "curveRings is ignored")
    v = parse_vintage({"features": [{"attributes": {"FLD_ZONE": "AE",
                                                    "ZONE_SUBTY": None},
                                     "geometry": {"rings": [rect(0, 0, 1, 1)],
                                                  "spatialReference":
                                                  {"wkid": 2236}}}]}, "v")
    check(v.sr == {"wkid": 2236} and not v.has_subtype
          and v.zones[0].subtype == "",
          "the spatialReference is read from a geometry when the set has "
          "none, and a null subtype is blank and carries no subtype")
    check(parse_vintage(fset([feat("AE", [rect(0, 0, 1, 1)], None),
                              feat("X", [rect(0, 0, 1, 1)], minimal)]),
                        "v").has_subtype,
          "one feature with a value is enough to carry the subtype")

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
          == 5, "without it the two parcels with no answer are exit 5, not a "
          "clean 0  <-- pinned defect")
    check(gate(compare([p for p in parcels if p.parcel_id
                        not in ("V-1", "U-1", "U-2")], o, n)) == 0,
          "and with every parcel decided the run is exit 0")
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
          "the 3 zone difference(s) are within the retrace distance and are "
          "counted as a moved line, not as a zone change. 2 of 6 parcel(s) "
          "have no answer and are in none of these counts.",
          "the verdict counts the real changes, calls the retraced ones a "
          "moved line without saying nothing changed, and names the parcels "
          "with no answer  <-- pinned defect")
    check("parcels read: 7, compared in both vintages: 5" in "\n".join(
        describe(rep)) and "classified" not in "\n".join(describe(rep)),
          "the header counts the parcels compared, not total over total  "
          "<-- pinned defect")
    decided = [p for p in parcels if p.parcel_id not in ("V-1", "U-1", "U-2")]
    text = "\n".join(describe(compare(decided, o, n)))
    check("no answer" not in text and text.splitlines()[-1].endswith(
        "not as a zone change."),
          "and a run with every parcel decided names none")
    lopsided = compare([parcels[3]] + [Parcel("U%d" % i, 9000 + i, 9000, i)
                                       for i in range(99)], o, n)
    check(gate(lopsided) == 5 and describe(lopsided)[-1].endswith(
        "99 of 100 parcel(s) have no answer and are in none of these "
        "counts."),
          "one parcel decided among 99 with no answer is exit 5 and the "
          "last line says 99 of 100  <-- pinned defect")
    quiet = "\n".join(describe(compare([parcels[3]], o, n)))
    check(quiet.splitlines()[-1] == "VERDICT: 0 parcel(s) changed zone. No "
          "compared parcel has a different zone in the two vintages."
          and "zone changes the geometry" not in quiet,
          "a run with no difference lists no section and no '0 of the 0'  "
          "<-- pinned defect")
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
    check(rows[0]["x"] == "1010" and rows[0]["y"] == "500"
          and rows[1]["x"] == "1500" and rows[1]["y"] == "1500",
          "x holds the X coordinate and y the Y  <-- pinned defect")
    gone = compare([Parcel("P", 990, 500, 2)], *pair(
        old_world, [feat("X", [rect(0, 0, 2000, 1000)])])[:2])
    check(classified_rows(gone)[0]["shift"] == ""
          and "does not exist" in classified_rows(gone)[0]["reason"]
          and "no such zone" in "\n".join(describe(gone)),
          "an infinite shift is written blank and printed in words")
    raises(lambda: compare([], o, n, 50.0, 10.0),
           "compare refuses a change distance under the retrace distance")

    # ---- pins from the mutation sweep of the decision core
    o, n, _ = pair(old_world, new_moved)
    check(gate(compare([Parcel("S", 500, 500, 2), Parcel("U", 9000, 9000, 3)],
                       o, n)) == 5,
          "exactly one parcel with no answer is exit 5, not a clean 0  "
          "<-- pinned defect")
    stacked = [feat("AE", [rect(0, 0, 1000, 1000)]),
               feat("X", [rect(0, 1000, 1000, 2000)])]
    o2, n2, _ = pair(stacked, stacked)
    check(one(o2, n2, 500, 1000).cls == UNVERIFIED
          and one(o2, n2, 500, 2000).cls == STABLE
          and one(o2, n2, 500, 0).cls == STABLE,
          "a point on a horizontal line two zones share is UNVERIFIED, and "
          "one on the top or bottom edge of the only zone is inside it  "
          "<-- pinned defect")
    o2 = parse_vintage(fset(old_world), "old")
    n2 = parse_vintage({"features": new_moved}, "new")
    check(any("declares no spatialReference" in x for x in settle(o2, n2)),
          "a newer vintage with no spatialReference is noted too, not "
          "refused  <-- pinned defect")
    o, n, _ = pair(old_world, new_moved)
    only_retraced = describe(compare([Parcel("R-1", 1010, 500, 2),
                                      Parcel("S-1", 500, 500, 3)], o, n))
    check(only_retraced[-1] == "VERDICT: 0 parcel(s) changed zone. 1 of the "
          "1 zone difference(s) are within the retrace distance and are "
          "counted as a moved line, not as a zone change.",
          "a run whose only differences are RETRACED says 0 changed zone and "
          "that the 1 is a moved line  <-- pinned defect")
    formula = classified_rows(Report([Result(
        Parcel("=cmd", 1.5, -2.5, 2), STABLE, "r", ("-AE", ""), ("@X", ""),
        1.0), Result(Parcel("P-1", 1.0, 2.0, 3), STABLE, "r", ("AE", ""),
                     ("AE", ""))], 30.0, 100.0, "ft"))
    check(formula[0]["parcel_id"] == "'=cmd" and formula[0]["old_zone"]
          == "'-AE" and formula[0]["new_zone"] == "'@X"
          and formula[0]["x"] == "1.5" and formula[0]["y"] == "-2.5"
          and formula[1]["parcel_id"] == "P-1",
          "a parcel id or zone that starts with = + - or @ is written with a "
          "leading apostrophe, a negative coordinate is not  "
          "<-- pinned defect")
    leads = (("=", "an equals sign"), ("+", "a plus sign"),
             ("-", "a minus sign"), ("@", "an at sign"), ("\t", "a tab"),
             ("\r", "a carriage return"))
    for lead, name in leads:
        guarded = classified_rows(Report([Result(
            Parcel(lead + "id", -1.5, -2.5, 7), UNVERIFIED, lead + "why",
            (lead + "Z", "sub"), (lead + "Y", ""), 3.0)], 30.0, 100.0,
            "ft"))[0]
        check(guarded["parcel_id"] == "'" + lead + "id"
              and guarded["old_zone"] == "'" + lead + "Z [sub]"
              and guarded["new_zone"] == "'" + lead + "Y"
              and guarded["reason"] == "'" + lead + "why"
              and guarded["x"] == "-1.5" and guarded["y"] == "-2.5"
              and guarded["line"] == 7 and guarded["shift"] == "3.000"
              and guarded["class"] == UNVERIFIED,
              "%s at the start of the id, both zones and the reason gets "
              "an apostrophe and the number columns do not  "
              "<-- pinned defect" % name)
        check(_uncell(_cell(lead + "text")) == lead + "text",
              "%s at the start of a cell survives a write and a read back"
              % name)
    plain = classified_rows(Report([Result(
        Parcel("it's=1", 1.0, 2.0, 2), STABLE, "a=b+c", ("A-E", "@x"),
        ("'AE", ""))], 30.0, 100.0, "ft"))[0]
    check(plain["parcel_id"] == "it's=1" and plain["old_zone"] == "A-E [@x]"
          and plain["new_zone"] == "'AE" and plain["reason"] == "a=b+c"
          and _cell("") == "" and _uncell("") == "",
          "a formula character past the first place, an apostrophe lead and "
          "an empty cell are written as they are")
    check(_cell("\n=1") == "\n=1" and _cell(" =1") == " =1"
          and _cell(chr(0xFF1D) + "1") == chr(0xFF1D) + "1",
          "a line feed, a space or a full-width equals sign at the start is "
          "not guarded, as the README Limits say")
    check(_uncell("'abc") == "'abc" and _uncell("'") == "'"
          and _uncell("''=x") == "''=x" and _uncell("'=x") == "=x"
          and _uncell("'\t") == "\t" and _uncell("'\r") == "\r",
          "reading back drops one apostrophe before a formula lead and "
          "nothing else")

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
    check("is missing or blank in at least one vintage" in out,
          "the layers carry no subtype, and the run says the key is the zone "
          "letter alone")
    x_csv = tmpfile("xparcel.csv", "PARCEL_ID,X,Y\nXS-1,1500,500\nS-1,500,500\n")
    x_csv3 = tmpfile("xparcel3.csv", "PARCEL_ID,X,Y\nXS-1,1500,500\n"
                     "S-1,500,500\nR-1,1010,500\n")
    code, out, err = run_cli([old_json, new_json, x_csv3])
    check(code == 5 and "XS-1  zone X in both vintages, but neither vintage "
          "has a subtype" in out and "S-1  zone AE in both vintages, but "
          "neither vintage has a subtype" in out
          and "2 of 3 parcel(s) have no answer" in out,
          "the whole CLI on layers with no subtype exits 5 for a parcel that "
          "is X in both and for one that is AE in both, not a clean 0  "
          "<-- pinned defect")
    code, out, err = run_cli([old_json, new_json, x_csv])
    check(code == 4 and "NOTHING COMPARED" in out
          and "UNVERIFIED 2" in out and "parcels with no answer:" in out
          and "XS-1  zone X in both vintages" in out
          and "S-1  zone AE in both vintages" in out,
          "and when every parcel is the same zone in both, the run is "
          "NOTHING COMPARED, exit 4, not a clean 0, and it lists the parcels "
          "with no answer and why  <-- pinned defect")
    code, out, err = run_cli([old_json, new_json, x_csv, "--subtype-field",
                              ""])
    check(code == 0 and "VERDICT: 0 parcel(s) changed zone" in out
          and "no answer" not in out,
          "and --subtype-field with an empty value accepts the letter alone")
    code, out, err = run_cli(base + ["--subtype-field", ""])
    check(code == 1 and "is missing or blank in at least one vintage" not in out,
          "--subtype-field with an empty value turns the subtype off, and "
          "then there is nothing to say")
    code, out, err = run_cli(base + ["--change-distance", "60",
                                     "--units", "m"])
    check(code == 5 and "VERDICT: 2 parcel(s) changed zone" in out
          and "70.0 m" in out and out.rstrip().endswith("3 of 6 parcel(s) "
          "have no answer and are in none of these counts."),
          "a change distance of 60 makes the 70 unit parcel CHANGED, and "
          "--units relabels without converting; the three parcels with no "
          "answer make it exit 5")
    code, out, err = run_cli(base + ["--retrace-distance", "5",
                                     "--change-distance", "5"])
    check(code == 5 and "R-1  X to AE, 10.0 ft" in out
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
          and written[0]["x"] == "1010.0" and written[0]["y"] == "500.0"
          and tuple(written[0].keys()) == OUT_COLUMNS,
          "--apply writes one row per parcel with the class")
    lead_ids = ("=A", "+B", "-C", "@D", "\tE", "\rF", "'Q", "P-1")
    lead_rows = classified_rows(Report([
        Result(Parcel(pid, -1.5, 2.5, 99), STABLE, "r", ("AE", ""),
               ("AE", "")) for pid in lead_ids], 30.0, 100.0, "ft"))
    lead_csv = os.path.join(tmp, "lead.csv")
    _write_csv(lead_csv, OUT_COLUMNS, lead_rows)
    with open(lead_csv, newline="", encoding="utf-8") as fh:
        lead_text = fh.read()
    back = read_parcels(lead_csv, {"id": "parcel_id", "x": "x", "y": "y"})
    check(lead_text.startswith("parcel_id,") and "'=A,99," in lead_text
          and [p.parcel_id for p in back] == ["=A", "+B", "-C", "@D", "E",
                                              "F", "'Q", "P-1"]
          and all(p.x == -1.5 and p.y == 2.5 for p in back)
          and [p.line for p in back] == [2, 3, 4, 5, 6, 7, 9, 10],
          "the --out file read back as parcels gives the ids that went in, "
          "minus the leading and trailing space the reader always drops, "
          "with the apostrophe lead gone, a real apostrophe kept, and a "
          "carriage return in a cell counted as a line break  "
          "<-- pinned defect")
    lead_old = tmpfile("lead_old.json", json.dumps(fset([
        feat("AE", [rect(0, 0, 1000, 1000)]),
        feat("X", [rect(1000, 0, 2000, 1000)])])))
    lead_new = tmpfile("lead_new.json", json.dumps(fset([
        feat("=AE", [rect(0, 0, 1000, 1000)]),
        feat("+X", [rect(1000, 0, 2000, 1000)])])))
    lead_in = tmpfile("lead_in.csv", "PARCEL_ID,X,Y\n=cmd,500,500\n"
                      "@cmd,1500,500\n-ok,1600,600\n")
    lead_out = os.path.join(tmp, "lead_out.csv")
    code, out, err = run_cli([lead_old, lead_new, lead_in, "--out",
                              lead_out, "--apply"])
    with open(lead_out, newline="", encoding="utf-8") as fh:
        lead_back = list(csv.DictReader(fh))
    check(code == 0 and [r["parcel_id"] for r in lead_back]
          == ["'=cmd", "'@cmd", "'-ok"]
          and [r["old_zone"] for r in lead_back] == ["AE", "X", "X"]
          and [r["new_zone"] for r in lead_back] == ["'=AE", "'+X", "'+X"]
          and [r["x"] for r in lead_back] == ["500.0", "1500.0", "1600.0"],
          "the CLI writes the apostrophe before an id that starts with = @ "
          "or -, and before a zone that starts with = or +, and not before "
          "a zone that does not  <-- pinned defect")
    stripped = read_parcels(tmpfile("ws.csv", "PARCEL_ID,X,Y\n\t=A,1,1\n"
                                    "\"\rB\",2,2\n"),
                            {"id": "PARCEL_ID", "x": "X", "y": "Y"})
    check([p.parcel_id for p in stripped] == ["=A", "B"]
          and parse_vintage(fset([feat("\tae", [rect(0, 0, 9, 9)])]), "v",
                            DEFAULT_ZONE_FIELD, "").zones[0].zone == "AE",
          "a tab or a carriage return before a parcel id or a zone label "
          "is dropped by the reader, so only the library API can reach the "
          "writer with one")
    again = os.path.join(tmp, "again.csv")
    code2, out2, err2 = run_cli([lead_old, lead_new, lead_out, "--id-field",
                                 "parcel_id", "--x-field", "x", "--y-field",
                                 "y", "--out", again, "--apply"])
    with open(again, newline="", encoding="utf-8") as fh:
        again_rows = list(csv.DictReader(fh))
    cols = ("parcel_id", "class", "old_zone", "new_zone", "x", "y")
    check(code2 == code and len(again_rows) == len(lead_back)
          and all([r[c] for r in again_rows] == [r[c] for r in lead_back]
                  for c in cols)
          and "=cmd  " in out2 and "'=cmd" not in out2,
          "the --out file fed back to the tool gives the same ids, "
          "classes, zones and coordinates, and the report names the id "
          "without the apostrophe  <-- pinned defect")
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
        check(code == 64 and "already exists" in err,
              "--out naming %s is refused" % os.path.basename(victim))
    with open(parcels_csv, encoding="utf-8") as fh:
        check(fh.read() == csv_text,
              "and the input is untouched  <-- pinned defect")
    precious = tmpfile("precious.csv", "PRECIOUS\n")
    for flags in (["--apply"], []):
        code, out, err = run_cli(base + ["--out", precious] + flags)
        with open(precious, encoding="utf-8") as fh:
            check(code == 64 and "already exists" in err
                  and fh.read() == "PRECIOUS\n" and "wrote" not in out,
                  "--out naming any other existing file is refused and the "
                  "file is untouched, %s  <-- pinned defect"
                  % ("with --apply" if flags else "on a dry run"))
    code, out, err = run_cli(base + ["--out", os.path.join(tmp, ".", "old.json"),
                                     "--apply"])
    check(code == 64, "an input spelled with a dot segment is refused too")
    code, out, err = run_cli(base + ["--out", tmp, "--apply"])
    check(code == 64 and "already exists" in err,
          "a directory is not a new file either")
    real_lexists = os.path.lexists
    os.path.lexists = lambda path: False
    try:
        code, out, err = run_cli(base + ["--out", precious, "--apply"])
    finally:
        os.path.lexists = real_lexists
    with open(precious, encoding="utf-8") as fh:
        check(code == 2 and "could not write" in err
              and fh.read() == "PRECIOUS\n",
              "a file that appears between the check and the write is not "
              "overwritten either, exit 2")
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

    sub_old = tmpfile("sub_old.json", json.dumps(fset([
        feat("AE", [rect(0, 0, 1000, 1000)], None),
        feat("X", [rect(1000, 0, 2000, 1000)], None)])))
    sub_new = tmpfile("sub_new.json", json.dumps(fset([
        feat("AE", [rect(0, 0, 1000, 1000)], ""),
        feat("X", [rect(1000, 0, 2000, 1000)], minimal)])))
    code, out, err = run_cli([sub_old, sub_new, tmpfile(
        "sub.csv", "PARCEL_ID,X,Y" + chr(10) + "A-1,500,500" + chr(10)
        + "X-1,1500,500" + chr(10))])
    check(code == 5 and "X-1  the same zone letter in both vintages" in out
          and "CHANGED    0" in out and "STABLE     1" in out
          and "same coordinate system" not in out,
          "a subtype blank in the older file and filled in the newer is "
          "exit 5 with the X parcel UNVERIFIED, not a CHANGED one  "
          "<-- pinned defect")
    code, out, err = run_cli([sub_old, sub_new, tmpfile(
        "sub1.csv", "PARCEL_ID,X,Y" + chr(10)
        + "X-1,1500,500" + chr(10))])
    check(code == 4 and "same coordinate system" not in out
          and "No parcel could be compared" in out,
          "and when every parcel is of that kind the exit is 4 without a "
          "coordinate system hint")

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
    spaced_id = tmpfile("spacedid.csv", "PARCEL_ID,X,Y"
                      + chr(10) + "  P-9  ,1,1" + chr(10))
    check(read_parcels(spaced_id, {"id": "PARCEL_ID", "x": "X",
                                   "y": "Y"})[0].parcel_id == "P-9",
          "spaces around a parcel id are not part of it  <-- pinned defect")
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
    code, out, err = run_cli(base + ["--out", os.path.join(tmp, "no", "z.csv"),
                                     "--apply"])
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
