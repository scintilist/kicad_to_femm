"""
FEMM *.fec file writing

Provides abstraction to the FEC file sections to allow adding/removing geometry, and then outputting the file.
"""
from weakref import WeakSet, WeakValueDictionary


# Convert float to string with set precision, and no trailing 0's
def float_to_str(f, p=12):
    return '{:.{}f}'.format(f, p).rstrip('0').rstrip('.')


class Boundary:
    TYPE_PERIODIC = 3

    def __init__(self, name, boundary_type=TYPE_PERIODIC):
        self.i = None  # Boundaries are 1-indexed (0 is reserved for no boundary)

        self.name = name
        self.type = boundary_type

    def __str__(self):
        return ('  <BeginBdry>\n'
                '    <BdryName> = "{}"\n'
                '    <BdryType> = {}\n'
                '    <vsr> = 0\n'
                '    <vsi> = 0\n'
                '    <qsr> = 0\n'
                '    <qsi> = 0\n'
                '    <c0r> = 0\n'
                '    <c0i> = 0\n'
                '    <c1r> = 0\n'
                '    <c1i> = 0\n'
                '  <EndBdry>\n').format(self.name, self.type)


class BlockProperty:
    def __init__(self, name, conductivity=5.8e7):
        self.name = name
        self.conductivity = conductivity

    def __str__(self):
        return ('  <BeginBlock>\n'
                '   <BlockName> = "{0:}"\n'
                '    <ox> = {1:}\n'
                '    <oy> = {1:}\n'
                '    <ex> = 1\n'
                '    <ey> = 1\n'
                '    <ltx> = 0\n'
                '    <lty> = 0\n'
                '  <EndBlock>\n').format(self.name, self.conductivity)


class Conductor:
    TYPE_CURRENT = 0
    TYPE_VOLTAGE = 1

    def __init__(self, name, conductor_type=TYPE_VOLTAGE, value=0.0):
        self.i = None  # Conductors are 1-indexed (0 is reserved for no conductor)

        self.name = name
        self.type = conductor_type
        if self.type == Conductor.TYPE_CURRENT:
            self.vcr = 0
            self.vci = 0
            self.qcr = value.real
            self.qci = value.imag
        elif self.type == Conductor.TYPE_VOLTAGE:
            self.vcr = value.real
            self.vci = value.imag
            self.qcr = 0
            self.qci = 0

    def __str__(self):
        return ('  <BeginConductor>\n'
                '    <ConductorName> = "{}"\n'
                '    <vcr> = {}\n'
                '    <vci> = {}\n'
                '    <qcr> = {}\n'
                '    <qci> = {}\n'
                '    <ConductorType> = {}\n'
                '  <EndConductor>\n').format(self.name, self.vcr, self.vci, self.qcr, self.qci, self.type)


class Point:
    """ Points compare equal if they have the same coordinate within a grid of size 1e-6.
        This allows close points to automatically be merged into one when adding to a set.
    """
    def __init__(self, x, y):
        self.i = None  # Points are 0-indexed
        self.x = x
        self.y = y

    @property
    def __key__(self):
        return round(self.x * 1e6), round(self.y * 1e6)

    def __hash__(self):
        return hash(self.__key__)

    def __eq__(self, other):
        return self.__key__ == other.__key__

    def __str__(self):
        return '{}\t{}\t0\t0\t0\n'.format(float_to_str(self.x), float_to_str(self.y))


class Segment:
    def __init__(self, points, conductor=None, boundary=None, mesh_size=-1):
        self.points = list(points)
        self.conductor = conductor
        self.boundary = boundary
        self.mesh_size = mesh_size

    def __str__(self):
        bound = self.boundary.i if self.boundary else 0
        cond = self.conductor.i if self.conductor else 0
        return '{}\t{}\t{}\t{}\t0\t0\t{}\n'.format(self.points[0].i, self.points[1].i, self.mesh_size, bound, cond)


class Arc:
    pass


class Hole:
    holes = []

    def __init__(self, x, y):
        self.x = x
        self.y = y
        Hole.holes.append(self)

    def __str__(self):
        return '{}\t{}\t0\n'.format(float_to_str(self.x), float_to_str(self.y))


class Block:
    def __init__(self, x, y, block_prop_index=1):
        self.x = x
        self.y = y
        self.block_prop_index = block_prop_index

    def __str__(self):
        return '{}\t{}\t{}\t-1\t0\t0\n'.format(float_to_str(self.x), float_to_str(self.y), self.block_prop_index)


class File:
    def __init__(self, file_name, thickness=.035, frequency=0):
        self.file_name = file_name
        self.thickness = thickness
        self.frequency = frequency

        self.segments = []
        self.arcs = []
        self.holes = []
        self.blocks = []
        self.block_properties = []

        # Weak sets used so that elements not used by any segments are automatically removed.
        self.boundaries = WeakSet()
        self.conductors = WeakSet()
        self.points = WeakValueDictionary()

    def add_segment(self, segment):
        # Add the segment, and associated boundaries, conductors, and points
        if segment.boundary:
            self.boundaries.add(segment.boundary)

        if segment.conductor:
            self.conductors.add(segment.conductor)

        for i, point in enumerate(segment.points):
            if point in self.points:
                segment.points[i] = self.points[point]
            else:
                self.points[point] = point

        self.segments.append(segment)

    def header(self):
        """ FEC File header """
        return ('[Format]      =  1\n'
                '[Precision]   =  1.0e-008\n'
                '[Frequency]   =  {}\n'
                '[MinAngle]    =  25\n'
                '[Depth]       =  {}\n'
                '[LengthUnits] =  millimeters\n'
                '[ProblemType] =  planar\n'
                '[Coordinates] =  cartesian\n'
                '[Comment]     =  "Auto generated by \'pcb_to_femm.py\'."\n'
                '[PointProps]  = 0\n').format(float_to_str(self.frequency), float_to_str(self.thickness))

    def write_out(self):
        """ Write out the FEC File. """
        with open(self.file_name, 'w') as f:
            # Write the header
            f.write(self.header())

            # Set indexes for boundaries, conductors, and points
            # Sort all sets so the same file will always be generated for a given input.
            # Sorting also makes it possible to do meaningful diffs of the output files.
            sorted_boundaries = sorted(self.boundaries, key=lambda b: b.name)
            for i, boundary in enumerate(sorted_boundaries):
                boundary.i = i + 1
            sorted_conductors = sorted(self.conductors, key=lambda c: c.name)
            for i, conductor in enumerate(sorted_conductors):
                conductor.i = i + 1
            sorted_points = sorted(self.points.values(), key=lambda p: (p.x, p.y))
            for i, point in enumerate(sorted_points):
                point.i = i

            # Write boundaries
            f.write('[BdryProps] = {}\n'.format(len(sorted_boundaries)))
            for boundary in sorted_boundaries:
                f.write(str(boundary))

            # Write block properties
            f.write('[BlockProps] = {}\n'.format(len(self.block_properties)))
            for block_prop in self.block_properties:
                f.write(str(block_prop))

            # Write conductors
            f.write('[ConductorProps] = {}\n'.format(len(sorted_conductors)))
            for conductor in sorted_conductors:
                f.write(str(conductor))

            # Write points
            f.write('[NumPoints] = {}\n'.format(len(sorted_points)))
            for point in sorted_points:
                f.write(str(point))

            # Write segments
            f.write('[NumSegments] = {}\n'.format(len(self.segments)))
            for segment in self.segments:
                f.write(str(segment))

            # write arcs
            f.write('[NumArcSegments] = {}\n'.format(len(self.arcs)))
            for arc in self.arcs:
                f.write(str(arc))

            # Write holes
            f.write('[NumHoles] = {}\n'.format(len(self.holes)))
            for hole in self.holes:
                f.write(str(hole))

            # Write blocks
            f.write('[NumBlockLabels] = {}\n'.format(len(self.blocks)))
            for block in self.blocks:
                f.write(str(block))
