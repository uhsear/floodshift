# floodshift

Refuse to call a re-digitized flood zone line a flood zone change. Compares two vintages of a
flood hazard layer against a list of parcels, and says which zone differences a moved line
explains and which ones the geometry confirms as real.

A flood map layer is traced again against a newer base map. The hazard did not change. The
lines moved by a few feet. A parcel report joins every parcel to its zone in the old layer and
its zone in the new layer, and lists each parcel whose label differs as a parcel that changed
flood zone.

A flood line is long, and parcels sit along both sides of it. Every parcel within a few feet of
the line is on the other side of it now. So the report fills up with the parcels that happen to
stand next to a line, and nothing in it separates them from the parcels where the hazard
really changed. The join reports no error. The counts look plausible. Somebody now has to
either act on every row or check every row by hand.

A common defence is a buffer: ignore any parcel within some distance of a line. It throws away
the evidence along with the noise. A new patch of hazard that starts
beside an old line is exactly the case it hides.

The question that separates the two cases is local. For a parcel whose label differs, did the
zone it used to be in still exist a short distance away in the new vintage, and did the zone it
is in now exist a short distance away in the old one? If both did, the line moved and the
parcel did not change zone. If either zone has no such neighbour, the zone really changed. In between, a person looks.

The quietest failure is a check that ran and compared nothing. A server that stops at its
record limit returns part of a layer and says so in one member of the response. The parcels
that fall in the missing zones are in no zone at all, and the run looks clean. This tool
refuses that file. A parcel list in a different coordinate system than the zones can do the
same. This tool exits 4 when no parcel lands in a zone, and it refuses values that all fit
longitude and latitude. It cannot see a parcel list in another projected unit, such as metres
against feet. See Limits.

```
$ python floodshift.py --self-test
floodshift self-test: temporary files only, no network, no credentials
--------------------------------------------------------------------
PASS  a parcel 10 east of the old line is X in the old vintage and AE in the new, so a plain overlay calls it a change
PASS  and the tool calls it RETRACED, because the old zone is 5 away in the new vintage  <-- pinned defect
...
PASS  a parcel in a new AE patch 500 from any old AE is CHANGED  <-- pinned defect
...
PASS  exactly the retrace distance is RETRACED, the limit is inclusive  <-- pinned defect
...
PASS  an old zone that is gone from the new vintage is CHANGED, though the new zone is 10 away in the old one  <-- pinned defect
...
PASS  ' ae ' and 'AE' are one zone, a stray space is no flood change  <-- pinned defect
...
PASS  unshaded X becoming shaded X is CHANGED, the zone letter alone would call it STABLE  <-- pinned defect
...
PASS  a subtype in one vintage only does not make the same zone two zones, and the zone letter alone is no answer  <-- pinned defect
...
PASS  a subtype field that is blank on every feature of the older vintage is no subtype, so a filled newer one is no false CHANGED  <-- pinned defect
...
PASS  a point exactly on the line two zones share is UNVERIFIED, neither zone may claim it  <-- pinned defect
...
PASS  exactly one parcel with no answer is exit 5, not a clean 0  <-- pinned defect
PASS  a point on a horizontal line two zones share is UNVERIFIED, and one on the top or bottom edge of the only zone is inside it  <-- pinned defect
...
PASS  exceededTransferLimit true is refused, the server cut the answer short  <-- pinned defect
...
PASS  vintages in 2236 and 3857 are refused  <-- pinned defect
...
PASS  --out without --apply writes nothing  <-- pinned defect
...
PASS  a tab at the start of the id, both zones and the reason gets an apostrophe and the number columns do not  <-- pinned defect
...
PASS  the --out file fed back to the tool gives the same file, and the report names the id without the apostrophe  <-- pinned defect
...
PASS  a prefix of a flag, --ap, is refused and not read as the full flag  <-- pinned defect
...
PASS  a subtype blank in the older file and filled in the newer is exit 5 with the X parcel UNVERIFIED, not a CHANGED one  <-- pinned defect
...
PASS  parcels nowhere near the zones are exit 4 with a hint  <-- pinned defect
...
PASS  a parcel id on two lines is refused and names the lines  <-- pinned defect
...
PASS  an unexpected exception exits 70, not the 1 that means review  <-- pinned defect
...
PASS  the footer reports failures by count and by name, and exits 1  <-- pinned defect
...
--------------------------------------------------------------------
225 assertions, 0 failed
```

The full run prints all 225 assertions. Each `...` line above is where this block is cut.

## What already exists

Getting the data is a solved problem. `seabre/fema-nfhl-postgis-load` loads the FEMA National
Flood Hazard Layer into PostGIS, and `mikejohnson51/NFHL` is an R interface to it. I read their
descriptions on GitHub and not their code. Neither describes a comparison of two vintages.

ArcGIS Pro has the closest tool I found,
[Detect Feature Changes](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/detect-feature-changes.html).
It needs the Advanced licence. Its page describes update and base line features, a search
distance that "must be greater than zero", and a `CHANGE_TYPE` field with values such as `S` for
a spatial change and `A` for an attribute change. That is a good fit for comparing two versions
of a line layer. It does not take parcels, and it does not say which parcels changed zone. This
tool does not replace it. It answers the narrower question about parcels.

A spatial overlay of the parcels with each vintage, then a comparison of the two labels, is what
the story describes. It uses the whole shape of the parcel, which this tool does not.

Three unrelated GitHub repositories are also named floodshift: a disaster-response table, a
flood segmentation model for satellite images, and one with no description. They share the name
and nothing else.

A search of GitHub on 2026-10-22 for a tool that compares the flood zones of two map vintages
(`flood zone change FIRM`, `flood zone parcel change`, `FEMA flood map revision parcels`,
`S_FLD_HAZ_AR`, `DFIRM compare`) returned nothing. A repeat on 2026-10-29 with `gh search repos`
found only the same three floodshift repositories, and the descriptive queries returned nothing
again. That shows only that the search found nothing.

## Requirements

Python 3.9 or newer and nothing else. No `arcpy`, no third-party package, no network, no
database.

The self-test prints the same 225 assertions on Windows with Python 3.13.2, Python 3.12 and
Python 3.9.25, and on Ubuntu with Python 3.12.3. The outputs are identical apart from line
endings: Windows ends each line with CRLF when the output is redirected to a file, and Ubuntu
ends it with LF. `coverage run --branch floodshift.py --self-test` reports 100 percent of 1037
statements and 222 branches (coverage.py 7.16.1, Windows, Python 3.13). The Linux host has no
coverage module, so coverage was measured on Windows only.

```
git clone https://github.com/uhsear/floodshift.git
```

## Getting the inputs

All three files must be in one projected coordinate system, in feet or in metres. The tool
measures a straight line between two points and converts nothing.

**The two vintages** are Esri JSON feature sets. A query against a feature layer with `f=json`
returns that shape, and `outSR` sets the coordinate system of the returned geometry
([Query](https://developers.arcgis.com/rest/services-reference/enterprise/query-feature-service-layer/)).
The ArcGIS Pro tool
[Features To JSON](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/conversion/features-to-json.html)
writes Esri JSON by default. I did not run `arcpy`, and that page does not say whether the file
carries a `spatialReference`. The tool notes a vintage that has none and carries on.

The FEMA layer of flood hazard zones stores its polygons in NAD83 geographic coordinates
(wkid 4269) and caps a response at 2000 records
([layer 28](https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28)). So ask for a
projected system, and page the query:

```
.../NFHL/MapServer/28/query?where=1%3D1&geometry=<xmin,ymin,xmax,ymax>&geometryType=esriGeometryEnvelope&inSR=4269&outSR=<projected wkid>&outFields=FLD_ZONE,ZONE_SUBTY&f=json
```

A response that the server cut short carries `"exceededTransferLimit": true`. A complete one
did not carry the member at all, in the one query I ran. The service I queried returns the
current layer. The older vintage has to come from your own archive. Join the features of every
page into one list under one feature set before you give the file to this tool.

**The parcels** are a CSV with an id, X and Y. In ArcGIS Pro,
[Feature To Point](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/feature-to-point.html)
with the `INSIDE` option puts a point inside each polygon, where `CENTROID` may fall outside it.
[Add Geometry Attributes](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/add-geometry-attributes.html)
with `POINT_X_Y_Z_M` adds `POINT_X` and `POINT_Y`. Export the table to CSV and name the columns:

```
python floodshift.py old.json new.json parcels.csv --id-field APN --x-field POINT_X --y-field POINT_Y
```

## Usage

```
python floodshift.py --self-test
python floodshift.py old.json new.json parcels.csv
python floodshift.py old.json new.json parcels.csv --retrace-distance 40 --change-distance 150
python floodshift.py old.json new.json parcels.csv --units m
python floodshift.py old.json new.json parcels.csv --subtype-field ""
python floodshift.py old.json new.json parcels.csv --out zones.csv --apply
```

| Flag | Default | What it does |
|---|---|---|
| `OLD` | none | The older vintage. Required unless `--self-test`. |
| `NEW` | none | The newer vintage. Required unless `--self-test`. |
| `PARCELS` | none | The parcels as a CSV. Required unless `--self-test`. |
| `--id-field` | `PARCEL_ID` | The parcel id column. |
| `--x-field` | `X` | The X column. |
| `--y-field` | `Y` | The Y column. |
| `--zone-field` | `FLD_ZONE` | The zone attribute in both vintages. The name is case sensitive. |
| `--subtype-field` | `ZONE_SUBTY` | The subtype attribute. An empty string turns it off. |
| `--retrace-distance` | `30` | At or under this shift a zone difference is RETRACED. |
| `--change-distance` | `100` | Above this shift a zone difference is CHANGED. |
| `--units` | `ft` | Label printed after every distance. Nothing is converted. |
| `--sample` | `10` | How many parcels to list under each heading. `0` lists none. |
| `--projected` | off | Say the coordinates are projected, to skip the degrees check. |
| `--out` | off | Write the classification of every parcel to this CSV. Needs `--apply`. |
| `--apply` | off | Actually write. Without it nothing is written. |
| `--self-test` | off | Run the assertions and exit. Other flags are ignored. |

Every flag must be spelled in full. The parser sets `allow_abbrev=False`, which Python's
[argparse](https://docs.python.org/3.9/library/argparse.html) documents as the switch that
turns off abbreviations of long options. `--ap` is refused with exit 64 and is not read as
`--apply`. `--ou` is refused too. The self-test pins the case.

`--out` must name a new file. A path that already exists is refused with exit 64, on a dry run
too. That includes the three inputs.

| Exit | Meaning |
|---|---|
| 0 | Every parcel was decided: STABLE, RETRACED or CHANGED. It does not mean that no zone changed. Read the CHANGED count, or `--out`, for that. |
| 1 | At least one parcel is in the REVIEW band. A person decides it. |
| 2 | A file could not be read or written. |
| 3 | The input was refused, because it cannot support an answer. |
| 4 | Nothing was compared. |
| 5 | At least one parcel is UNVERIFIED and none is in the REVIEW band. The tool could not tell for those parcels. |
| 64 | Usage error, a wrong column name or a bad distance. |
| 70 | An unexpected crash in the tool. It is never a finding. |

Exit 4 comes first, then 1, then 5. A run with a REVIEW parcel and an UNVERIFIED parcel exits 1.

## What it decides

Every parcel lands in exactly one class, and the counts add up to the parcels read.

| Class | When | What it means |
|---|---|---|
| `STABLE` | The same zone in both vintages. | Nothing to explain. |
| `RETRACED` | The zones differ, and the shift is at most `--retrace-distance`. | The line moved. Not a zone change. |
| `REVIEW` | The shift is between the two distances. | The geometry has no opinion. |
| `CHANGED` | The shift is above `--change-distance`, or a zone has no counterpart. | A real change. |
| `UNVERIFIED` | No coordinate, or not inside exactly one zone of each vintage, or the subtype is in one vintage only (below). | No answer, and no guess. |

The shift of a parcel whose zone differs is the larger of two distances: from the parcel to the
nearest polygon of its new zone in the old vintage, and from the parcel to the nearest polygon
of its old zone in the new vintage. It is the larger one, because a parcel next to a line that
moved 10 feet and also next to a zone that vanished is not explained by the 10. A zone that
does not exist at all in the other vintage gives an infinite shift, which is CHANGED.

The zone is its label with case and spacing folded, plus the subtype when both vintages carry
one. A zone code of `ae` with a stray space is the same zone as `AE`. Zone X is two hazards:
FEMA labels the moderate hazard areas "Zone B or Zone X (shaded)" and the minimal hazard areas
"Zone C or Zone X (unshaded)"
([flood zones](https://www.fema.gov/glossary/flood-zones)). Only the subtype tells the two X
areas apart, so a parcel that goes from one to the other is a change. In the one query I ran,
the X polygons carried the subtype `AREA OF MINIMAL FLOOD HAZARD` and the A polygons had no
subtype. The tool compares whatever text the subtype holds.

A vintage carries the subtype when at least one of its features has a value there. A field that
is present and blank on every feature carries nothing. When only one vintage carries the
subtype, the subtype is left out of the key, and the run prints a note. A parcel whose zone
letter matches in both vintages is then UNVERIFIED if it sits in a zone that has a subtype in
the vintage that carries it. The letter alone cannot show that a shaded X did not become an
unshaded one. A parcel in a zone with a blank subtype there is STABLE. When neither vintage
carries a subtype, the parcels are compared by letter and the note says so.

A point exactly on a line that two zones share, or inside two overlapping polygons of different
zones, is in more than one zone. It is UNVERIFIED. Neither zone may claim it.

Polygon rings use the even-odd rule, so a hole is a hole whichever way the ring is wound. Esri
documents that rule as the one that draws its polygons correctly, and says a ring's first
point is also its last
([geometry objects](https://developers.arcgis.com/rest/services-reference/enterprise/geometry-objects/)).
The reader closes a ring that is not closed.

## The demo

The layers are synthetic. Save this as `make_demo.py` and run it.

```python
import json


def rect(x0, y0, x1, y1):
    return [[x0, y0], [x0, y1], [x1, y1], [x1, y0], [x0, y0]]


def layer(*zones):
    return {"spatialReference": {"wkid": 2236}, "features": [
        {"attributes": {"FLD_ZONE": z}, "geometry": {"rings": [rect(*box)]}}
        for z, box in zones]}


json.dump(layer(("AE", (0, 0, 1000, 1000)), ("X", (1000, 0, 2000, 1000)),
                ("AH", (0, 1000, 2000, 2000))), open("old.json", "w"))
json.dump(layer(("AE", (0, 0, 1015, 1000)), ("X", (1015, 0, 2000, 1000)),
                ("AH", (0, 1000, 2000, 1060)), ("X", (0, 1060, 2000, 2000))),
          open("new.json", "w"))
open("parcels.csv", "w").write(
    "PARCEL_ID,X,Y\nR-1,1010,500\nC-1,1500,1500\nV-1,1500,1070\n"
    "S-1,500,500\nU-1,9000,9000\nU-2,,\n")
```

The old layer has its AE and X boundary at x = 1000. In the new layer it is at x = 1015.
The AH area north of y = 1000 now stops at y = 1060, and X covers the rest. `R-1` is 10 feet
east of the old line. `C-1` and `V-1` are in the AH area that became X.

```
$ python floodshift.py old.json new.json parcels.csv
floodshift: old.json 3 zone(s), new.json 4 zone(s), parcels.csv 6 parcel(s)
NOTE      ZONE_SUBTY is missing or blank in at least one vintage, so it is left out of the zone key and Zone X shaded and unshaded are not told apart
parcels read: 6, compared in both vintages: 4
  STABLE     1
  RETRACED   1
  REVIEW     1
  CHANGED    1
  UNVERIFIED 2
retrace distance 30.0 ft, change distance 100.0 ft

a plain overlay reports 3 parcel(s) changed zone

zone changes the geometry confirms, worst first:
  C-1  AH to X, 500.0 ft, line 3

zone differences in the band between, for a human to look at:
  V-1  AH to X, 70.0 ft, line 4

zone differences within the retrace distance, counted as a moved line:
  R-1  X to AE, 10.0 ft, line 2

parcels with no answer:
  U-1  in no zone of the old vintage; in no zone of the new vintage, line 6
  U-2  no usable coordinate, line 7

VERDICT: 1 parcel(s) in the review band. Decide them before a change count is published. 2 of 6 parcel(s) have no answer and are in none of these counts.
[exit 1]
```

`R-1` is the story. A plain overlay calls it a change, and the tool does not, because the old
zone `X` is 5 feet away in the new vintage and the new zone `AE` is 10 feet away in the old
one. `C-1` was in `AH`, and the nearest old `X` is 500 feet away, so it is a real change.
`V-1` is in between, at 70 feet. The geometry has no opinion about it, so the exit is 1.

The two distances are yours to set. Name a change distance of 60 and `V-1` is decided. The two
parcels with no answer still keep the run from a clean exit, so the code is 5, not 0:

```
$ python floodshift.py old.json new.json parcels.csv --change-distance 60
floodshift: old.json 3 zone(s), new.json 4 zone(s), parcels.csv 6 parcel(s)
NOTE      ZONE_SUBTY is missing or blank in at least one vintage, so it is left out of the zone key and Zone X shaded and unshaded are not told apart
parcels read: 6, compared in both vintages: 4
  STABLE     1
  RETRACED   1
  REVIEW     0
  CHANGED    2
  UNVERIFIED 2
retrace distance 30.0 ft, change distance 60.0 ft

a plain overlay reports 3 parcel(s) changed zone

zone changes the geometry confirms, worst first:
  C-1  AH to X, 500.0 ft, line 3
  V-1  AH to X, 70.0 ft, line 4

zone differences within the retrace distance, counted as a moved line:
  R-1  X to AE, 10.0 ft, line 2

parcels with no answer:
  U-1  in no zone of the old vintage; in no zone of the new vintage, line 6
  U-2  no usable coordinate, line 7

VERDICT: 2 parcel(s) changed zone. 1 of the 3 zone difference(s) are within the retrace distance and are counted as a moved line, not as a zone change. 2 of 6 parcel(s) have no answer and are in none of these counts.
[exit 5]
```

## Nothing is written without `--apply`

A dry run names the file and says it was not written. `--apply` writes one row per parcel.

```
$ python floodshift.py old.json new.json parcels.csv --out zones.csv
...

VERDICT: 1 parcel(s) in the review band. Decide them before a change count is published. 2 of 6 parcel(s) have no answer and are in none of these counts.

Check only. zones.csv was not written. Re-run with --apply.
[exit 1]

$ python floodshift.py old.json new.json parcels.csv --out zones.csv --apply
...
VERDICT: 1 parcel(s) in the review band. Decide them before a change count is published. 2 of 6 parcel(s) have no answer and are in none of these counts.

wrote zones.csv
[exit 1]

$ cat zones.csv
parcel_id,line,x,y,old_zone,new_zone,class,shift,reason
R-1,2,1010.0,500.0,X,AE,RETRACED,10.000,"the line would have to move at most 30.0 ft: counted as a moved line, not a zone change"
C-1,3,1500.0,1500.0,AH,X,CHANGED,500.000,"the line would have to move 500.0 ft, more than 100.0 ft: a real change"
V-1,4,1500.0,1070.0,AH,X,REVIEW,70.000,"the line would have to move 70.0 ft, between 30.0 and 100.0 ft: neither a re-trace nor a change"
S-1,5,500.0,500.0,AE,AE,STABLE,,the same zone in both vintages
U-1,6,9000.0,9000.0,,,UNVERIFIED,,in no zone of the old vintage; in no zone of the new vintage
U-2,7,,,,,UNVERIFIED,,no usable coordinate

$ python floodshift.py old.json new.json parcels.csv --ap
usage: floodshift.py [-h] [--id-field ID_FIELD] [--x-field X_FIELD]
                     [--y-field Y_FIELD] [--zone-field ZONE_FIELD]
                     [--subtype-field SUBTYPE_FIELD]
                     [--retrace-distance RETRACE_DISTANCE]
                     [--change-distance CHANGE_DISTANCE] [--units UNITS]
                     [--sample SAMPLE] [--projected] [--out OUT] [--apply]
                     [--self-test]
                     [OLD] [NEW] [PARCELS]
floodshift.py: error: unrecognized arguments: --ap
[exit 64]
```

A spreadsheet reads a cell as a formula when its text starts with `=`, `+`, `-`, `@`, a tab or
a carriage return
([OWASP, CSV Injection](https://owasp.org/www-community/attacks/CSV_Injection) lists these
characters and the single-quote prefix as the defence). Every text column of `--out` is written
with one leading apostrophe when it starts with one of the six: `parcel_id`, `old_zone`,
`new_zone` and `reason`. The numeric columns (`line`, `x`, `y`, `shift`) are not touched, so a
negative coordinate keeps its sign, and `class` is one of five fixed words. The self-test
checks each of the four text columns for each of the six characters, and that the numeric
columns come out unchanged, with a negative coordinate.

The tool reads one CSV, the parcel list, and `--out` is a valid parcel list: run with
`--id-field parcel_id --x-field x --y-field y` and the file comes back. The reader drops one
apostrophe from the id when it stands before one of the six characters, and nothing else. The
self-test writes a file with ids `=A`, `+B`, `-C`, `@D`, a tab id, a carriage return id,
`'Q` and `P-1`, reads it back, and then runs the CLI on its own `--out` file and checks that
the second file equals the first.

None of the six reaches the output from a file today through a tab or a carriage return. The
reader strips the white space around a parcel id, and the zone reader collapses white space in a
label, so a leading tab or carriage return is gone before the writer sees it. The tab and
carriage return guard is there for the next code path, and the self-test reaches it through
`classified_rows` directly. The `=`, `+`, `-` and `@` cases are reached end to end through the
CLI. The `reason` text always begins with a fixed word, and the guard on it is the same belt
and braces.

## A subtype in one vintage only

Zone X is two hazards, and the subtype tells them apart. Here the older file has the
`ZONE_SUBTY` field on every feature but blank, and the newer file fills it in. The shapes are
identical. A tool that compared the subtype as a key would call every X parcel a change. This
tool does not, and it does not call the X parcel stable either:

```
$ python floodshift.py sub_old.json sub_new.json sub.csv
floodshift: sub_old.json 2 zone(s), sub_new.json 2 zone(s), sub.csv 2 parcel(s)
NOTE      ZONE_SUBTY is missing or blank in at least one vintage, so it is left out of the zone key and Zone X shaded and unshaded are not told apart. A parcel in the same zone of both vintages is UNVERIFIED where the vintage that has the subtype gives the zone one
parcels read: 2, compared in both vintages: 1
  STABLE     1
  RETRACED   0
  REVIEW     0
  CHANGED    0
  UNVERIFIED 1

a plain overlay reports 0 parcel(s) changed zone

parcels with no answer:
  X-1  the same zone letter in both vintages, but only the new vintage has a subtype, so a move between subtypes of this zone cannot be seen, line 3

VERDICT: 0 parcel(s) changed zone. No compared parcel has a different zone in the two vintages. 1 of 2 parcel(s) have no answer and are in none of these counts.
[exit 5]
```

The files are `sub_old.json` and `sub_new.json` with an AE polygon (blank subtype in both) and
an X polygon (blank in the old file, `AREA OF MINIMAL FLOOD HAZARD` in the new one). The
parcels are `A-1` inside AE and `X-1` inside X. To get a decision, re-export the older vintage
with the subtype, or pass `--subtype-field ""` to say the letter is enough.

## Refusals

The tool says why it stopped, and the exit code is 3.

**A response the server cut short.** `cut.json` is `new.json` with `"exceededTransferLimit": true`.
A real query against the FEMA layer with `resultRecordCount=5` returned that member, and this
tool refused the file with the same message. The same query without the limit returned 19
polygons and no such member.

```
$ python floodshift.py old.json cut.json parcels.csv
REFUSED   cut.json: exceededTransferLimit is true, so the server stopped at its record limit and some zones are missing. Page the query with resultOffset until the flag is false, join the features of every page into one list under one feature set, and read that file. A page read alone leaves zones out.

VERDICT: REFUSED
[exit 3]
```

**Degrees.** Every coordinate fits longitude and latitude, which is how the FEMA layer stores
its polygons (wkid 4269). Distances in degrees mean nothing against a threshold in feet.

```
$ python floodshift.py deg_old.json deg_new.json deg.csv
REFUSED   every coordinate fits longitude -180..180 and latitude -90..90, so these look like degrees. Distances in degrees mean nothing against a threshold in feet. Request the layer with outSR set to a projected system, or pass --projected if this is a local grid.

VERDICT: REFUSED
[exit 3]
```

**A parcel id that two rows claim.** A zone change cannot be tied to one parcel.

```
$ python floodshift.py old.json new.json dup.csv
REFUSED   dup.csv: the parcel id is not unique, so a zone change could not be tied to one parcel: 'R-1' is on lines 2, 3

VERDICT: REFUSED
[exit 3]
```

**A feature that names no zone or has no polygon.** One feature with a blank or null zone
label, or with null geometry, refuses the whole vintage. The tool does not skip it. Clean the
layer first. The self-test pins both cases. The other refusals are two vintages in different
coordinate systems, two vintages that do not overlap anywhere, a file that is not JSON, and
a file with no `features` list.

**A run that compared nothing.** The parcels fall nowhere near the zones, as they do when they
are in a different coordinate system. This is exit 4. It is never a clean run.

```
$ python floodshift.py old.json new.json far.csv
floodshift: old.json 3 zone(s), new.json 4 zone(s), far.csv 1 parcel(s)
NOTE      ZONE_SUBTY is missing or blank in at least one vintage, so it is left out of the zone key and Zone X shaded and unshaded are not told apart
parcels read: 1, compared in both vintages: 0
  STABLE     0
  RETRACED   0
  REVIEW     0
  CHANGED    0
  UNVERIFIED 1
retrace distance 30.0 ft, change distance 100.0 ft

VERDICT: NOTHING COMPARED. No parcel could be compared in both vintages, so the geometry checked nothing. This is not a clean run. Check that the parcels are in the same coordinate system as the zones.
[exit 4]
```

## Run on real polygons

This section was measured once, on an earlier build of the tool, before the exit 5 rule and the
subtype rule above. I did not run it again for this version. The response and the outputs were
not kept, so you cannot check the figures from this repository.

One query for a small box against the FEMA layer returned 19 polygons. Two of them hold about
420 thousand and 380 thousand vertices. I used that response, projected with `outSR`, as the
new vintage. I made the old vintage by moving every vertex 12 feet east and 9 feet south, a
15 foot shift. That is a simulated re-digitization, not a real one. The parcels are 400 points
6 feet either side of vertices of the hazard A polygons.

- A plain overlay reported 40 parcels as changed zone. The tool classified all 40 RETRACED, with
  a shift of 3.5 to 13.1 feet. 151 were STABLE. 209 had no answer, because they lie in no zone of
  the new vintage.
- I then relabelled the largest A polygon in the old vintage as X. That is a real change
  in the label and none in the shape. The tool reported 136 CHANGED and 3 RETRACED among 139 zone
  differences.
- Each run took about 5 to 6 seconds for these 800 thousand vertices on Windows.

On 2026-10-29 I checked the service again. Layer 28 reports a maximum record count of 2000 and
wkid 4269, and a query with `resultRecordCount=5` returns `exceededTransferLimit: true`. That
matches the sections above.

## Limits

- **One point per parcel.** A parcel the line crosses is judged by its one point. Use Feature To
  Point with `INSIDE`, and know that a parcel half in a zone is not described by it.
- **A re-trace and a small real revision look the same.** A real revision that moves a line by
  less than `--retrace-distance` is RETRACED. The class means the geometry cannot show a
  change. It does not mean nobody changed the map.
- **Only the zone label and the subtype are compared.** A zone `AE` with a new base flood
  elevation is STABLE. A floodway that moved inside an `AE` area is a change only if the
  subtype says so.
- **A relabel is a change.** FEMA's page names the moderate hazard areas "Zone B or Zone X
  (shaded)" and the minimal hazard areas "Zone C or Zone X (unshaded)". A layer that uses B in
  one vintage and X in the other shows every such parcel as CHANGED. Recode one vintage first.
- **The distances are starting values.** 30 and 100 are not standards. Trace a line you know did
  not change, in both vintages, and measure its offset.
- **The thresholds and the printed unit are never read from the spatialReference.** Set both
  yourself in a metric CRS. The defaults are 30 and 100 in the CRS's own units, and the default
  label is `ft`. A metre-based CRS prints `ft` unless you pass `--units m`, and 30 m is about
  98 ft.
- **Planar distance.** No projection, datum or unit is read or converted. The tool refuses
  values that all fit longitude and latitude. A projected system with wrong units gives a number
  in the wrong units, and `--units` only labels it.
- **All three inputs must share one projected CRS, and the tool cannot check the parcels.** A
  parcel file has no spatial reference. A parcel list in metres against zones in feet is not
  refused. It lands in the wrong place and is STABLE, UNVERIFIED or exit 4, and a true change
  can read as none. Check the parcels' CRS yourself.
- **Exit 0 means every parcel was decided, not that no zone changed.** A CI job cannot gate on
  confirmed changes with the exit code. Read the CHANGED count or `--out`.
- **Rows with no id and no coordinates count as parcels with no answer.** Excel often writes a
  trailing row of only commas. It becomes a parcel with no id and no coordinates, which is
  UNVERIFIED and turns an otherwise clean run into exit 5. Delete blank spreadsheet rows first.
- **Coordinates are parsed with Python's `float()`,** which accepts forms such as `1_000` and
  non-ASCII digits. A full-width number is read as a coordinate.
- **Save the parcel CSV as UTF-8.** CSV and JSON are decoded with replacement characters.
  Text that is not UTF-8 is read with replacement characters, so two different ids, or two
  labels, that differ only in those bytes become the same text. Two ids can then be refused as
  a duplicate id, and labels can compare as different zones.
- **A CSV with two columns of the same name keeps the last one** without a warning. This is how
  Python's `csv.DictReader` behaves.
- **A NUL byte in a parcel file** is exit 2 on Python 3.9, where the csv module rejects it.
  Python 3.13 reads it. The self-test count is the same on both.
- **Field names must match in case.** A service that returns `fld_zone` gets exit 64 naming the
  field, not a silent mismatch.
- **Only plain Esri JSON polygon rings are accepted.** A geometry with `curveRings`, and GeoJSON,
  are refused with exit 3 and not read.
- **A vintage with any feature that has null geometry or a blank or null zone label is refused
  whole.** Clean it first.
- **Only a JSON `true` is read as a truncated response.** A hand-edited or concatenated file
  that carries `exceededTransferLimit` as the string `"true"` is not refused. Real servers send
  a JSON boolean.
- **Two names for one projection are refused unless one carries `latestWkid`.** A vintage that
  declares only wkid 102658 and one that declares only 2236 share no identifier and are
  refused as different systems, although they are the same projection. The refusal is safe,
  and a false alarm. Re-export one vintage with `outSR` set to the other's. A wkt on one side
  and a wkid on the other are refused for the same reason.
- **Pure Python.** Edges are filed in horizontal bands, so a point meets only the edges level
  with it. Finding the zones that hold a parcel scans the bounding box of every zone, so the
  work grows with parcels times zones. Run time was measured only for 400 parcels against 19
  polygons, on an earlier build. A county-wide layer against a large parcel file will be slow.
- **Coordinates beyond about 1e300 are not supported.** A polygon with a near-zero height, or a
  coordinate near 1e308, can stop the run with exit 64 or 70, which is never a finding. A
  coordinate of 1e308 on both sides of a polygon gave exit 64 in my test.
- **The whole file is read into memory.** There is no streaming.
- **No `spatialReference` is a note, not a refusal.** A datum difference between vintages that
  share a wkid is invisible to this tool. It shows as a shift, and a uniform one of a few feet
  is RETRACED.
- **Parcel ids and `--units` are printed as they are.** An id that holds terminal escape
  sequences can change how a terminal shows the report. Open the report in a viewer you
  trust if the parcel file is not yours.
- **The output is written in place,** not through a temporary file. A failed write, such as a
  full disk, can leave a partial file, and the exit code is 2. A second run is then refused
  because the file exists. Delete the partial file by hand.
- **Only six leading characters are guarded.** A line feed, the full-width forms of `=`, `+`,
  `-` and `@`, and a formula that starts after white space are not. OWASP lists the line feed
  and the full-width forms as well, and warns that Excel can drop the apostrophe when a CSV is
  saved and re-opened. This tool writes the file and does not see that later save.
- **An id that really starts with an apostrophe and then `=`, `+`, `-`, `@`, a tab or a
  carriage return loses that apostrophe when it is read back.** The reader cannot tell it from
  the apostrophe the writer added. An id of `'Q` and an id of `A'=1` are not affected.
- **Interpreters re-run.** The 225 assertions were re-run on Windows with Python 3.13.2, 3.12
  and 3.9.25, and on Ubuntu with Python 3.12.3. Linux has no coverage module.
- **No line is unreachable offline.** Coverage is complete. The tool has no network code and
  holds no credential, so there is no stub server. The self-test disables `socket.socket` while
  a full run goes through.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [datumcheck](https://github.com/uhsear/datumcheck) - exit non-zero when PROJ moves your data
  with a null-shift operation. Run it first when two vintages might sit on different datums.
- [idreuse](https://github.com/uhsear/idreuse) - the same refusal for a join: a match the
  geometry disproves is not a match.
- [sliverkeep](https://github.com/uhsear/sliverkeep) - drop the sliver parts of a polygon layer,
  and keep every part that holds an address point.
