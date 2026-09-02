# partcad-cam-gscrib

A PartCAD package that turns a part into the **G-code that makes it** — sliced
into layers, walked in as perimeters, filled, and written as a program an FDM
printer will execute.

The G-code is written by [gscrib](https://github.com/joansalasoler/gscrib). This
package is the PartCAD side of it: it declares the `cam:` file type, the machine
description a part can point at, and the script PartCAD runs to write the file.
No gscrib code is vendored here — PartCAD installs gscrib from PyPI into the
sandbox it runs the implementation in.

Published in the PartCAD index as `//pub/feature/cam/gscrib`.

## Why `cam:` and not `export:` or `render:`

PartCAD's three output sections are not three names for the same thing:

- **`export:`** is for files another CAD tool opens as a **part or a sketch** —
  geometry it can go on working with.
- **`render:`** is for **output files in general** — a drawing, a picture, a
  report.
- **`cam:`** is for the instructions that **make** the part.

G-code is the third. Nothing downstream can load it as geometry, and nothing
reads it as a document: what it is for is being executed by a printer, and it is
wrong on any other printer. That is also why PartCAD ships no `cam:`
implementation of its own — which machine, and what it wants said to it, is
knowledge that lives out here — and why `pc cam` writes one file for one part
rather than a directory of them the way `pc render` does.

## Using it

```yaml
dependencies:
  pub:
    type: git
    url: https://github.com/partcad/partcad-index.git

parts:
  bracket:
    type: build123d
    manufacturable: true
    properties:
      material: PLA
    manufacturing:
      method: additive
      tool: //pub/feature/cam/gscrib:fdm   # the machine that makes it
      layerHeight: 0.2
      infill: 0.25

cam:
  gcode:
    package: //pub/feature/cam/gscrib
    path: cam_gscrib.py
    extension: gcode
    visual: stl
```

```shell
pc cam :bracket                 # bracket.gcode, here and in the package
pc cam --visual :bracket        # bracket.stl: what that G-code deposits
pc cam -o job1.gcode :bracket   # the copy, under a name of your own
```

Or read the configuration from here instead of declaring it, which makes the
instructions for a part of any package without that package knowing about this
one:

```shell
pc cam -e //pub/feature/cam/gscrib //your/package:bracket
```

`pc cam` produces the file **twice over**, and on purpose. The package keeps a
copy next to the part — written once and reused afterwards, so the instructions
are something a repository can hold and diff — and the command leaves a copy
where it was run, which is the one to feed to a machine.

## Where the settings come from

Three places, each more specific than the one before it:

| | what it says | example |
|---|---|---|
| the **machine** | what it can do, as ranges, and the `positioning:` sequence the preamble is built from | `//pub/feature/cam/gscrib:fdm` |
| the **part** | the values it is made at, in its own `manufacturing:` section | `layerHeight: 0.2` |
| the **file type** | anything set on `cam: gcode:`, the most specific thing an author can write for one file | `cam: {gcode: {infill: 0.4}}` |

A value nobody gives falls through to a plain default. A part that says nothing
still gets a file — and a warning with it, because a file made entirely of
defaults is not one to print.

`pc test` is where the part and the machine are held to each other: its
`cam-additive` check fails a part whose setting is outside the machine's range,
whose material the machine does not take, or whose bounding box does not fit the
build volume.

## The machine this package describes

`//pub/feature/cam/gscrib:fdm` is a generic desktop FDM printer: a
220 × 220 × 250 mm bed, a 0.4 mm nozzle, and the ranges such a machine works in.
It is there so that a part is printable without describing a machine first.
Declare an `additive` tool of your own once you know which machine — the file
type here works against any of them, because everything it needs is in the tool.

## Seeing it before printing it

`pc cam --visual` writes an STL of what the G-code **puts down** rather than of
the part: every perimeter and every fill line as a solid of the width the nozzle
lays and the height of one layer. The walls, the gaps between them and the fill
pattern are all in it — the things worth looking at before a print, and the
things a picture of the part cannot show. The PartCAD Viewer's **CAM** tab in
the VS Code extension shows the same model.

## What it is not

It is a small slicer. There is **no support generation**, no bridging, no
cooling logic, no seam placement, no retraction and no variable layer height,
and the infill is rectilinear. A part with overhangs needs supports this does
not produce, and one with many separate islands per layer will string between
them, because the filament is never pulled back on a travel move.

Read what it writes before feeding it to a machine. That is true of every
slicer, and more so of this one.

## Licensing

This package is Apache-2.0, like PartCAD itself. It contains no gscrib code:
gscrib is installed from PyPI at run time into the sandbox, and is
[GPL-3.0](https://github.com/joansalasoler/gscrib). Whatever you make with the
G-code is yours; how you distribute software that links gscrib is between you
and that licence.
