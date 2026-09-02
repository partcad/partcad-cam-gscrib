#
# PartCAD CAM implementation backed by gscrib.
#
# Licensed under Apache License, Version 2.0.
#
"""Turn a PartCAD part into the G-code that makes it, with gscrib.

This is a 'cam:' implementation, which is neither an export nor a render. What
it writes is not a file another tool opens as geometry and not a picture of the
part: it is a **program for one machine**, and it is correct only for that
machine. That is why the section exists and why PartCAD ships no implementation
of its own - which machine, and what it wants said to it, is knowledge that
lives out here.

It runs inside a PartCAD sandbox, driven by PartCAD's output meta-wrapper
('wrappers/wrapper_export.py' - one wrapper serves all three sections). The
wrapper hands it two globals and calls one of two entry points:

    request  -- the shape in 'request["wrapped"]' (an OCP 'TopoDS_Shape'), what
                the part says about how it is made ('manufacturing'), what the
                machine says it can do ('tool'), and every parameter declared
                for this file type in 'partcad.yaml'
    path     -- the absolute path of the file to write

    process(path, request)         -- the G-code
    process_visual(path, request)  -- an STL of what that G-code deposits

Nothing here imports 'wrapper_common': that module is a PartCAD internal, and a
package published on its own should not be pinned to its shape. Failures are
reported the documented way instead - by returning {"success": False,
"exception": ...}, and anything worth saying about a file that was still
produced correctly by returning {"warnings": [...]}, which PartCAD logs against
the object.

What this is and is not
-----------------------

It is a real slicer in the sense that matters here: it sections the solid at
every layer, walks the contours in, lays perimeters along them and fills what is
left, and writes moves and extrusions that a printer will execute. It is a small
one. There is no support generation, no bridging, no cooling logic, no seam
placement, no variable layer height, and the infill is rectilinear. A part with
overhangs will need supports this does not produce.

Read what it writes before feeding it to a machine. That is true of every
slicer, and more so of this one.
"""

import math
import traceback

import build123d as b3d
from gscrib import GCodeBuilder
from gscrib.enums import DistanceMode, ExtrusionMode, LengthUnits

# What the machine gets told when neither the part, the machine nor the file
# type says. Every one of them is a value a printer will accept and none of them
# is a value tuned for any particular printer: they exist so that a package that
# says nothing still gets a file, not so that it gets a good one.
DEFAULTS = {
    "layerHeight": 0.2,  # mm
    "spotSize": 0.4,  # mm, the width of one extruded bead
    "speed": 60.0,  # mm/s
    "temperature": 210.0,  # degrees C, at the nozzle
    "bedTemperature": 60.0,  # degrees C
    "infill": 0.2,  # a fraction of the interior
    "perimeters": 2,
    "supports": False,
}

# The settings that come from the part and may be bounded by the machine. Named
# the same on every side, which is what lets them be merged rather than
# translated (see 'partcad.tool.ADDITIVE_RANGES').
SETTINGS = tuple(DEFAULTS.keys())

# The filament the extrusion arithmetic assumes, in mm. Set 'filamentDiameter'
# on the file type for anything else; 1.75 is what almost every FDM machine
# built this decade takes.
DEFAULT_FILAMENT_DIAMETER = 1.75

# How finely a curved edge is walked, in mm. A straight edge is not walked at
# all - its two ends are the whole of it - so this only costs points where the
# geometry actually curves.
DEFAULT_RESOLUTION = 0.4

# How far apart two points have to be to be two points, in mm. Below this the
# machine cannot tell them apart anyway (a printer's smallest step is around
# 0.01mm) and the move is noise in the file.
EPSILON = 1e-3


def _shape(wrapped):
    """The shape PartCAD sent, as something build123d can section.

    Borrowed the way PartCAD's own renderers do it: take any Shape instance and
    replace what it wraps. Deliberately not 'b3d.Shape.cast()', which reads like
    the honest downcast but is an abstract method with an empty body on the base
    class - on build123d 0.11 it answers None instead of raising.
    """
    shape = b3d.Solid.make_box(1, 1, 1)
    shape.wrapped = wrapped
    return shape


def _number(value, default=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def settings_of(request):
    """What this part is made at, from the three places that may say.

    Lowest to highest:

      1. the machine's own default for the property, where its range has one;
      2. what the part says in its 'manufacturing:' section;
      3. what the file type says, which is the most specific thing the package
         author wrote for this one file.

    A value nobody gives falls through to 'DEFAULTS'.
    """
    settings = dict(DEFAULTS)

    tool = request.get("tool") or {}
    for field in SETTINGS:
        declared = tool.get(field)
        if isinstance(declared, dict) and declared.get("default") is not None:
            settings[field] = declared["default"]

    manufacturing = (request.get("manufacturing") or {}).get("settings") or {}
    for field in SETTINGS:
        if manufacturing.get(field) is not None:
            settings[field] = manufacturing[field]

    for field in SETTINGS:
        if request.get(field) is not None:
            settings[field] = request[field]

    settings["perimeters"] = max(1, int(settings["perimeters"]))
    settings["infill"] = min(1.0, max(0.0, float(settings["infill"])))
    for field in ("layerHeight", "spotSize", "speed", "temperature", "bedTemperature"):
        settings[field] = float(settings[field])
    if settings["layerHeight"] <= 0.0 or settings["spotSize"] <= 0.0:
        raise ValueError("'layerHeight' and 'spotSize' have to be above zero")

    settings["material"] = manufacturing.get("material") or request.get("material")
    settings["filamentDiameter"] = _number(request.get("filamentDiameter"), DEFAULT_FILAMENT_DIAMETER)
    settings["resolution"] = _number(request.get("resolution"), DEFAULT_RESOLUTION)
    settings["positioning"] = dict(tool.get("positioning") or {})
    # Whether the machine can pull the filament back, and by how much. Empty for
    # a machine that says nothing, which PartCAD already collapses "said nothing"
    # and "said no distance" into - so an empty section here means one thing:
    # never retract. The file type may override any of it, for a spool that
    # oozes more than the machine was measured with.
    retraction = dict(tool.get("retraction") or {})
    for field in ("distance", "feedRate", "zHop", "minTravel"):
        if request.get("retraction" + field[0].upper() + field[1:]) is not None:
            retraction[field] = _number(request.get("retraction" + field[0].upper() + field[1:]), retraction.get(field))
    settings["retraction"] = retraction if _number(retraction.get("distance"), 0.0) > 0.0 else {}
    settings["buildVolume"] = tool.get("buildVolume")
    settings["machine"] = tool.get("name")
    place = request.get("place") or "bed"
    if place not in ("bed", "asIs"):
        raise ValueError("'place' is 'bed' or 'asIs', not %r" % (place,))
    settings["place"] = place
    return settings


def place_on_bed(shape, settings):
    """The part where the machine will actually make it.

    A printer cannot reach below its own bed, and a part modelled about the
    origin is half under it. So the part is set down - its lowest point on the
    bed - and put in the middle of the build area, or over the machine's start
    point when it never said how big the bed is.

    'place: asIs' leaves it exactly where the model has it, for a package whose
    coordinates already are the machine's.
    """
    if settings.get("place") == "asIs":
        return shape

    positioning = settings["positioning"]
    origin = positioning.get("origin") or [0.0, 0.0, 0.0]
    volume = settings.get("buildVolume")
    if volume:
        target = (origin[0] + volume[0] / 2.0, origin[1] + volume[1] / 2.0)
    else:
        target = (origin[0], origin[1])

    box = shape.bounding_box()
    return b3d.Pos(
        target[0] - (box.min.X + box.max.X) / 2.0,
        target[1] - (box.min.Y + box.max.Y) / 2.0,
        origin[2] - box.min.Z,
    ) * shape


def _polyline(wire, resolution):
    """A wire as a list of points, walking curves and stepping over straights.

    'order_edges()' is what makes it a path rather than a bag of edges: it hands
    them back chained end to end and pointing the same way round, which is the
    order the tool has to travel them in.
    """
    points = []

    def append(point):
        candidate = (round(point.X, 4), round(point.Y, 4), round(point.Z, 4))
        if points and _distance(points[-1], candidate) < EPSILON:
            return
        points.append(candidate)

    for edge in wire.order_edges():
        if edge.geom_type == b3d.GeomType.LINE:
            samples = (0.0, 1.0)
        else:
            steps = max(2, int(math.ceil(edge.length / max(resolution, EPSILON))))
            samples = tuple(index / steps for index in range(steps + 1))
        for parameter in samples:
            append(edge @ parameter)
    return points


def _distance(one, other):
    return math.dist(one[:2], other[:2])


def _closed(points):
    """The same polyline, with the first point repeated at the end."""
    if len(points) > 1 and _distance(points[0], points[-1]) > EPSILON:
        points = points + [points[0]]
    return points


def _region_wires(region):
    """Every closed contour of a 2D region, outer boundaries and holes alike."""
    wires = []
    for face in region.faces():
        wires.append(face.outer_wire())
        wires.extend(face.inner_wires())
    return wires


def _offset_region(face, amount):
    """The region 'amount' inside 'face', or None when nothing is left of it.

    An offset that eats the whole region is the normal way a thin wall ends, not
    a failure: OCCT reports it by raising, by handing back nothing, or by
    handing back a region with no area, and all three mean the same thing here.
    """
    if amount == 0.0:
        return face
    try:
        region = b3d.offset(face, amount)
    except Exception:
        return None
    if region is None or not region.faces():
        return None
    return region


def _infill_lines(region, spacing, angle, resolution):
    """Rectilinear infill: parallel lines clipped to the region.

    The lines are laid out in the region's own plane, long enough to cross it
    whichever way they are turned, and then cut to it. What comes back is the
    pieces that fall inside, which is what the tool actually travels.
    """
    if spacing <= 0.0:
        return []
    box = region.bounding_box()
    z = (box.min.Z + box.max.Z) / 2.0
    centre = ((box.min.X + box.max.X) / 2.0, (box.min.Y + box.max.Y) / 2.0)
    reach = math.hypot(box.max.X - box.min.X, box.max.Y - box.min.Y) / 2.0 + spacing
    direction = (math.cos(math.radians(angle)), math.sin(math.radians(angle)))
    normal = (-direction[1], direction[0])

    lines = []
    steps = int(math.floor(reach / spacing))
    for step in range(-steps, steps + 1):
        offset = step * spacing
        anchor = (centre[0] + normal[0] * offset, centre[1] + normal[1] * offset)
        start = (anchor[0] - direction[0] * reach, anchor[1] - direction[1] * reach, z)
        end = (anchor[0] + direction[0] * reach, anchor[1] + direction[1] * reach, z)
        try:
            clipped = region.intersect(b3d.Edge.make_line(start, end))
        except Exception:
            continue
        if clipped is None:
            continue
        for edge in clipped.edges():
            if edge.length < EPSILON:
                continue
            lines.append(_polyline(b3d.Wire([edge]), resolution))
    return lines


class Layer:
    """One layer of the sliced part: where the tool goes, in the order it goes."""

    def __init__(self, z, perimeters, infill):
        self.z = z
        self.perimeters = perimeters
        self.infill = infill

    @property
    def empty(self):
        return not self.perimeters and not self.infill


def slice_shape(shape, settings):
    """Every layer of the part, bottom to top.

    Sectioned at the middle of each layer rather than at its floor or its
    ceiling, which is what a layer of finite thickness actually covers and what
    keeps a sloped wall from stepping a whole layer out of place.
    """
    layer_height = settings["layerHeight"]
    spot = settings["spotSize"]
    perimeters = settings["perimeters"]
    infill = settings["infill"]
    resolution = settings["resolution"]

    box = shape.bounding_box()
    height = box.max.Z - box.min.Z
    if height <= 0.0:
        raise ValueError("the shape is flat: there is nothing to print")
    count = max(1, int(math.ceil(round(height / layer_height, 6))))

    layers = []
    for index in range(count):
        floor = box.min.Z + index * layer_height
        middle = min(floor + layer_height / 2.0, box.max.Z - EPSILON)
        section = b3d.section(shape, section_by=b3d.Plane.XY.offset(middle))
        faces = [face for face in section.faces() if face.area > EPSILON]
        if not faces:
            continue

        walls = []
        fill = []
        for face in faces:
            for ring in range(perimeters):
                region = _offset_region(face, -(ring + 0.5) * spot)
                if region is None:
                    break
                walls += [_closed(_polyline(wire, resolution)) for wire in _region_wires(region)]
            if infill > 0.0:
                interior = _offset_region(face, -(perimeters + 0.5) * spot)
                if interior is not None:
                    # 45 degrees, turned by a right angle each layer: what makes
                    # the fill of one layer bond to the one under it instead of
                    # lying in the same grooves.
                    angle = 45.0 + 90.0 * (index % 2)
                    fill += _infill_lines(interior, spot / max(infill, 1e-6), angle, resolution)

        # The nozzle sits at the top of the layer it is laying down.
        layers.append(Layer(round(floor + layer_height, 4), walls, fill))
    return layers


def _extrusion(length, settings):
    """How much filament a move of this length puts down, in mm of filament."""
    area = math.pi * (settings["filamentDiameter"] / 2.0) ** 2
    return length * settings["layerHeight"] * settings["spotSize"] / area


def _preamble(g, settings, shape):
    """Get the machine to a known point over the bed, as the machine says to.

    Every step comes from the tool's 'positioning' section, which is PartCAD's
    own description of the sequence rather than the G-code for it - so what is
    written here is this machine's way of doing what that section describes.
    """
    positioning = settings["positioning"]
    g.comment("Generated by PartCAD with gscrib")
    if settings.get("machine"):
        g.comment("Machine: %s" % settings["machine"])
    if settings.get("material"):
        g.comment("Material: %s" % settings["material"])
    g.comment(
        "Layer %.2fmm, bead %.2fmm, %d perimeters, %.0f%% infill"
        % (settings["layerHeight"], settings["spotSize"], settings["perimeters"], settings["infill"] * 100.0)
    )

    g.set_length_units(LengthUnits.INCHES if positioning.get("units") == "inch" else LengthUnits.MILLIMETERS)
    g.set_distance_mode(DistanceMode.ABSOLUTE if positioning.get("absolute", True) else DistanceMode.RELATIVE)
    g.set_extrusion_mode(ExtrusionMode.RELATIVE)

    if settings["bedTemperature"] > 0.0:
        g.set_bed_temperature(settings["bedTemperature"])
    g.set_hotend_temperature(settings["temperature"])

    home = positioning.get("home")
    if home is None or home:
        g.auto_home()
    if positioning.get("bedLeveling") in ("auto", "mesh"):
        g.write("G29")

    origin = positioning.get("origin") or [0.0, 0.0, 0.0]
    safe_z = _number(positioning.get("safeZ"), 5.0)
    travel = _number(positioning.get("travelFeedRate"), settings["speed"] * 60.0 * 2.0)
    g.set_feed_rate(travel)
    g.rapid(z=origin[2] + safe_z)
    g.rapid(x=origin[0], y=origin[1])
    _prime(g, positioning, settings, shape)


def _prime(g, positioning, settings, shape):
    """The purge line, where the machine asks for one."""
    prime = positioning.get("prime") or {}
    length = _number(prime.get("length"), 0.0)
    if length <= 0.0:
        return
    extrude = _number(prime.get("extrude"), 0.0)
    feed = _number(prime.get("feedRate"), 1200.0)
    box = shape.bounding_box()
    g.comment("Prime")
    g.rapid(x=box.min.X, y=box.min.Y - settings["spotSize"] * 2.0, z=settings["layerHeight"])
    g.set_feed_rate(feed)
    g.move(x=box.min.X + length, e=extrude)


def _pull(g, settings, feed, sign):
    """Pull the filament back, or push it in again: 'sign' says which.

    Extrusion is relative (the preamble sets M83), so this is a move on the
    extruder alone and nothing else has to be tracked. The caller sets whatever
    feed rate it needs next, so the one used here is not restored.
    """
    retraction = settings["retraction"]
    if not retraction:
        return
    g.set_feed_rate(_number(retraction.get("feedRate"), feed))
    g.move(e=sign * retraction["distance"])


def _write_layers(g, layers, settings):
    """Every layer, perimeters first and then the fill, in travel order."""
    feed = settings["speed"] * 60.0  # gscrib feed rates are per minute
    travel = _number(settings["positioning"].get("travelFeedRate"), feed * 2.0)
    safe = _number(settings["positioning"].get("safeZ"), 5.0)
    retraction = settings["retraction"]
    # How far to lift to travel between two paths. The machine's 'zHop' is the
    # answer where it gives one: what a travel inside a layer has to clear is
    # what is already down at that layer, which a fraction of a millimetre does.
    # 'safeZ' is the fallback and is the height the preamble crosses the machine
    # at - far more than this needs, and what every file this wrote before the
    # machine could state a hop used.
    hop = _number(retraction.get("zHop"), 0.0) if retraction else 0.0
    lift = hop if hop > 0.0 else safe
    # Below this a travel strings less than the retraction itself costs.
    min_travel = _number(retraction.get("minTravel"), 0.0) if retraction else 0.0

    here = None
    for number, layer in enumerate(layers, start=1):
        if layer.empty:
            continue
        g.comment("Layer %d of %d, z=%.3f" % (number, len(layers), layer.z))
        for path in layer.perimeters + layer.infill:
            if len(path) < 2:
                continue
            # Retract only for a travel worth retracting for, and never for the
            # very first one: there is nothing behind the nozzle to pull back
            # from, and the prime line put it there on purpose.
            pulled = here is not None and _distance(here, path[0]) >= min_travel
            if pulled:
                _pull(g, settings, feed, -1.0)
            g.set_feed_rate(travel)
            g.rapid(z=layer.z + lift)
            g.rapid(x=path[0][0], y=path[0][1])
            g.rapid(z=layer.z)
            if pulled:
                _pull(g, settings, feed, 1.0)
            g.set_feed_rate(feed)
            previous = path[0]
            for point in path[1:]:
                length = _distance(previous, point)
                if length < EPSILON:
                    continue
                g.move(x=point[0], y=point[1], e=_extrusion(length, settings))
                previous = point
            here = previous


def _epilogue(g, settings, shape):
    """Leave the machine safe, which is the other half of `_preamble`.

    A file that simply stops after the last bead leaves the hotend at printing
    temperature, the bed hot and the steppers holding, with the nozzle parked on
    top of the print. Every step here undoes something the preamble did, and the
    order is the part that matters: lift clear of the print *before* turning the
    heat off, because a nozzle cooling while resting on the last layer welds
    itself to it.
    """
    positioning = settings["positioning"]
    origin = positioning.get("origin") or [0.0, 0.0, 0.0]
    safe_z = _number(positioning.get("safeZ"), 5.0)
    travel = _number(positioning.get("travelFeedRate"), settings["speed"] * 60.0 * 2.0)

    g.comment("Done")
    # Pulled back before the lift, so the last thing the nozzle does over the
    # print is stop pushing rather than trail a thread across it on the way out.
    _pull(g, settings, settings["speed"] * 60.0, -1.0)
    g.set_feed_rate(travel)
    g.rapid(z=shape.bounding_box().max.Z + safe_z)
    g.set_hotend_temperature(0)
    if settings["bedTemperature"] > 0.0:
        g.set_bed_temperature(0)
    # Out of the way, so the print can be lifted off without reaching past the
    # nozzle to do it.
    g.rapid(x=origin[0], y=origin[1])
    # Written out rather than called: gscrib's `power_off()` is `M05`, which
    # stops a spindle. What an FDM machine wants at the end is its steppers
    # released.
    g.write("M84 ; Disable steppers")


def process(path, request):
    """Write the G-code that makes this part."""
    try:
        settings = settings_of(request)
        shape = place_on_bed(_shape(request["wrapped"]), settings)
        layers = slice_shape(shape, settings)
        if not layers:
            return {"success": False, "exception": "the shape sliced into no layers"}

        g = GCodeBuilder(output=path)
        try:
            _preamble(g, settings, shape)
            _write_layers(g, layers, settings)
            _epilogue(g, settings, shape)
        finally:
            g.teardown()

        warnings = []
        if request.get("manufacturing") is None:
            warnings.append(
                "the part says nothing about how it is made, so every setting is a default;"
                " declare a 'manufacturing:' section with a 'tool:'"
            )
        if (request.get("manufacturing") or {}).get("settings", {}).get("supports"):
            warnings.append("this implementation generates no supports, and the part asks for them")
        return {"success": True, "warnings": warnings}
    except Exception as e:
        traceback.print_exc()
        return {"success": False, "exception": "%s: %s" % (type(e).__name__, e)}


def process_visual(path, request):
    """Write an STL of what the G-code puts down, rather than the G-code.

    Not the part: the beads. Each perimeter and each fill line becomes a solid
    of the width the nozzle lays and the height of one layer, so what comes back
    shows the walls, the gaps between them and the fill pattern - the things
    that are worth looking at before a print, and the things a picture of the
    part cannot show.
    """
    try:
        settings = settings_of(request)
        shape = place_on_bed(_shape(request["wrapped"]), settings)
        layers = slice_shape(shape, settings)

        beads = []
        for layer in layers:
            paths = [path_points for path_points in layer.perimeters + layer.infill if len(path_points) > 1]
            for points in paths:
                bead = _bead(points, settings, layer.z)
                if bead is not None:
                    beads.append(bead)
        if not beads:
            return {"success": False, "exception": "the shape sliced into nothing to draw"}

        b3d.export_stl(b3d.Compound(children=beads), path)
        return {"success": True}
    except Exception as e:
        traceback.print_exc()
        return {"success": False, "exception": "%s: %s" % (type(e).__name__, e)}


def _bead(points, settings, z):
    """One extruded path as a solid of the width it is laid at."""
    try:
        flat = [(x, y) for x, y, _ in points]
        line = b3d.Polyline(*flat) if len(flat) > 2 else b3d.Line(flat[0], flat[1])
        face = b3d.trace(line, line_width=settings["spotSize"])
        solid = b3d.extrude(face, amount=settings["layerHeight"])
        return b3d.Pos(0, 0, z - settings["layerHeight"]) * solid
    except Exception:
        # One bead that cannot be drawn is one bead missing from a picture, not
        # a picture that cannot be drawn.
        return None
