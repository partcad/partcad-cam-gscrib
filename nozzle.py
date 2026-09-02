# The likeness of an FDM hot end, for PartCAD to draw where material is put
# down.
#
# Nothing is manufactured from this and nothing is measured off it. What it does
# have to get right is where it sits, and that is the convention every tool
# visual in PartCAD follows:
#
#   the working end of the tool is at the origin, and the tool extends along
#   -Z, because a port's +Z points *into* the object it belongs to.
#
# So a nozzle drawn at a point on a part stands above that point, pointing at
# it, which is where a nozzle is.

import build123d as bd

TIP_DIAMETER = 0.4
TIP_LENGTH = 1.0
CONE_DIAMETER = 7.0
CONE_LENGTH = 6.0
THROAT_DIAMETER = 6.0
THROAT_LENGTH = 8.0
BLOCK = (23.0, 16.0, 12.0)

with bd.BuildPart() as result:
    # The 0.4mm land the plastic actually leaves from, at the origin.
    with bd.Locations(bd.Location((0, 0, -TIP_LENGTH / 2))):
        bd.Cylinder(TIP_DIAMETER / 2, TIP_LENGTH)
    # The cone of the nozzle, widening away from the part.
    with bd.Locations(bd.Location((0, 0, -TIP_LENGTH - CONE_LENGTH / 2))):
        bd.Cone(CONE_DIAMETER / 2, TIP_DIAMETER / 2, CONE_LENGTH)
    # The heater block it screws into.
    with bd.Locations(bd.Location((0, 0, -TIP_LENGTH - CONE_LENGTH - BLOCK[2] / 2))):
        bd.Box(BLOCK[0], BLOCK[1], BLOCK[2])
    # And the throat above it.
    with bd.Locations(bd.Location((0, 0, -TIP_LENGTH - CONE_LENGTH - BLOCK[2] - THROAT_LENGTH / 2))):
        bd.Cylinder(THROAT_DIAMETER / 2, THROAT_LENGTH)

if "show_object" in locals():
    show_object(result.part.wrapped, name="nozzle")
