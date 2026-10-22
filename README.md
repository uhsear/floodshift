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
that fall in the missing zones are in no zone at all, and the run looks clean. A parcel list in
a different coordinate system than the zones does the same. This tool refuses the first, and
exits 4 on the second.

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
PASS  a subtype in one vintage only is left out of the key and the run says so  <-- pinned defect
...
PASS  a point exactly on the line two zones share is UNVERIFIED, neither zone may claim it  <-- pinned defect
...
PASS  exceededTransferLimit true is refused, the server cut the answer short  <-- pinned defect
...
PASS  vintages in 2236 and 3857 are refused  <-- pinned defect
...
PASS  --out without --apply writes nothing  <-- pinned defect
...
PASS  a prefix of a flag, --ap, is refused and not read as the full flag  <-- pinned defect
...
PASS  parcels nowhere near the zones are exit 4 with a hint  <-- pinned defect
...
PASS  a parcel id on two lines is refused and names the lines  <-- pinned defect
...
PASS  layers that all fit longitude and latitude are refused, as the FEMA layer is stored  <-- pinned defect
...
PASS  an id the console cannot encode prints as ? and the verdict still arrives  <-- pinned defect
...
PASS  an unexpected exception exits 70, not the 1 that means review  <-- pinned defect
...
PASS  the footer reports failures by count and by name, and exits 1  <-- pinned defect
...
--------------------------------------------------------------------
158 assertions, 0 failed
```

The full run prints all 158 assertions. Each `...` line above is where this block is cut.

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
`S_FLD_HAZ_AR`, `DFIRM compare`) returned nothing. That shows only that the search found
nothing.

## Requirements

Python 3.9 or newer and nothing else. No `arcpy`, no third-party package, no network, no
database.

The self-test prints the same 158 assertions on Windows (Python 3.13.2), on Python 3.9.25
for Windows and on Ubuntu (Python 3.12.3). The three outputs are byte for byte identical.
`coverage run --branch floodshift.py --self-test` reports 100 percent of 878 statements and 196
branches (coverage.py 7.16.1, Windows, Python 3.13). The Linux host has no coverage module, so
coverage was measured on Windows only.

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
current layer. The older vintage has to come from your own archive.

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
| `--zone-field` | `FLD_ZONE` | The zone attribute in both vintages. |
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

`--out` must name a new file. A path that is also one of the three inputs is refused with exit
64, on a dry run too.

| Exit | Meaning |
|---|---|
| 0 | Every parcel inside one zone of both vintages was decided: STABLE, RETRACED or CHANGED. |
| 1 | At least one parcel is in the REVIEW band. A person decides it. |
| 2 | A file could not be read or written. |
| 3 | The input was refused, because it cannot support an answer. |
| 4 | Nothing was compared. |
| 64 | Usage error, a wrong column name or a bad distance. |
| 70 | An unexpected crash in the tool. It is never a finding. |

## What it decides

Every parcel lands in exactly one class, and the counts add up to the parcels read.

| Class | When | What it means |
|---|---|---|
| `STABLE` | The same zone in both vintages. | Nothing to explain. |
| `RETRACED` | The zones differ, and the shift is at most `--retrace-distance`. | The line moved. Not a zone change. |
| `REVIEW` | The shift is between the two distances. | The geometry has no opinion. |
| `CHANGED` | The shift is above `--change-distance`, or a zone has no counterpart. | A real change. |
| `UNVERIFIED` | No coordinate, or not inside exactly one zone of each vintage. | No answer, and no guess. |

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
NOTE      ZONE_SUBTY is not in both vintages, so it is left out of the zone key and Zone X shaded and unshaded are not told apart
parcels classified: 6 of 6
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

zone differences that are a re-traced line, not a change:
  R-1  X to AE, 10.0 ft, line 2

parcels with no answer:
  U-1  in no zone of the old vintage; in no zone of the new vintage, line 6
  U-2  no usable coordinate, line 7

VERDICT: 1 parcel(s) in the review band. Decide them before a change count is published.
[exit 1]
```

`R-1` is the story. A plain overlay calls it a change, and the tool does not, because the old
zone `X` is 5 feet away in the new vintage and the new zone `AE` is 10 feet away in the old
one. `C-1` was in `AH`, and the nearest old `X` is 500 feet away, so it is a real change.
`V-1` is in between, at 70 feet. The geometry has no opinion about it, so the exit is 1.

The two distances are yours to set. Name a change distance of 60 and `V-1` is decided:

```
$ python floodshift.py old.json new.json parcels.csv --change-distance 60
floodshift: old.json 3 zone(s), new.json 4 zone(s), parcels.csv 6 parcel(s)
NOTE      ZONE_SUBTY is not in both vintages, so it is left out of the zone key and Zone X shaded and unshaded are not told apart
parcels classified: 6 of 6
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

zone differences that are a re-traced line, not a change:
  R-1  X to AE, 10.0 ft, line 2

parcels with no answer:
  U-1  in no zone of the old vintage; in no zone of the new vintage, line 6
  U-2  no usable coordinate, line 7

VERDICT: 2 parcel(s) changed zone. 1 of the 3 zone difference(s) were a re-traced line and are not changes.
[exit 0]
```

## Nothing is written without `--apply`

A dry run names the file and says it was not written. `--apply` writes one row per parcel.

```
$ python floodshift.py old.json new.json parcels.csv --out zones.csv
...

VERDICT: 1 parcel(s) in the review band. Decide them before a change count is published.

Check only. zones.csv was not written. Re-run with --apply.
[exit 1]

$ python floodshift.py old.json new.json parcels.csv --out zones.csv --apply
...
VERDICT: 1 parcel(s) in the review band. Decide them before a change count is published.

wrote zones.csv
[exit 1]

$ cat zones.csv
parcel_id,line,x,y,old_zone,new_zone,class,shift,reason
R-1,2,1010.0,500.0,X,AE,RETRACED,10.000,the line moved at most 30.0 ft: not a zone change
C-1,3,1500.0,1500.0,AH,X,CHANGED,500.000,"the line would have to move 500.0 ft, more than 100.0 ft: a real change"
V-1,4,1500.0,1070.0,AH,X,REVIEW,70.000,"the line would have to move 70.0 ft, between 30.0 and 100.0 ft: neither a re-trace nor a change"
S-1,5,500.0,500.0,AE,AE,STABLE,,the same zone in both vintages
U-1,6,9000.0,9000.0,,,UNVERIFIED,,in no zone of the old vintage; in no zone of the new vintage
U-2,7,,,,,UNVERIFIED,,no usable coordinate

$ python floodshift.py old.json new.json parcels.csv --ap
(usage text omitted)
floodshift.py: error: unrecognized arguments: --ap
[exit 64]
```

## Refusals

The tool says why it stopped, and the exit code is 3.

**A response the server cut short.** `cut.json` is `new.json` with `"exceededTransferLimit": true`.
A real query against the FEMA layer with `resultRecordCount=5` returned that member, and this
tool refused the file with the same message. The same query without the limit returned 19
polygons and no such member.

```
$ python floodshift.py old.json cut.json parcels.csv
REFUSED   cut.json: exceededTransferLimit is true, so the server stopped at its record limit and some zones are missing. Page the query with resultOffset until the flag is false, then try again.

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

**A run that compared nothing.** The parcels fall nowhere near the zones, as they do when they
are in a different coordinate system. This is exit 4. It is never a clean run.

```
$ python floodshift.py old.json new.json far.csv
floodshift: old.json 3 zone(s), new.json 4 zone(s), far.csv 1 parcel(s)
NOTE      ZONE_SUBTY is not in both vintages, so it is left out of the zone key and Zone X shaded and unshaded are not told apart
parcels classified: 1 of 1
  STABLE     0
  RETRACED   0
  REVIEW     0
  CHANGED    0
  UNVERIFIED 1
retrace distance 30.0 ft, change distance 100.0 ft

VERDICT: NOTHING COMPARED. No parcel was inside exactly one zone of both vintages, so the geometry checked nothing. This is not a clean run. Check that the parcels are in the same coordinate system as the zones.
[exit 4]
```

## Run on real polygons

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

The response is not in this repository. Only the counts above are.

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
- **Planar distance.** No projection, datum or unit is read or converted. The tool refuses
  values that all fit longitude and latitude. A projected system with wrong units gives a number
  in the wrong units, and `--units` only labels it.
- **Pure Python.** Edges are filed in horizontal bands, so a point meets only the edges level
  with it. The measured time for 400 parcels against 800 thousand vertices is above. Nothing
  larger was measured.
- **The whole file is read into memory.** There is no streaming.
- **No `spatialReference` is a note, not a refusal.** A datum difference between vintages that
  share a wkid is invisible to this tool. It shows as a shift, and a uniform one of a few feet
  is RETRACED.
- **A wkt on one side and a wkid on the other are refused** as different, because they share no
  identifier.
- **A key that starts with `=`, `+`, `-` or `@`** is written as it came. Open `--out` as text,
  or a spreadsheet may read it as a formula.
- **The output is written in place,** not through a temporary file. A failed write can leave a
  partial file, and the exit code is 2.
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
