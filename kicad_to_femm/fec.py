"""
FEMM *.fec file writing

Provides abstraction to the FEC file sections to allow adding/removing geometry, and then outputting the file.
"""
import gc
from collections import defaultdict
from weakref import WeakKeyDictionary, ref, WeakSet
from math import floor, ceil

from kicad_to_femm.spinner import spinner

# A small distance, points within this distance of each other should be considered a single point.
small_distance = 1e-3


def bresenham(segment, grid_size=1.0):
    """ Yield integer coordinates for every box painted along the segment according to bresenham's algorithm.

        The boxes have side length grid_size.
        No point on the line will be more than 0.5 * grid_size away from a box in the x or y directions.
    """
    x0, y0 = segment.points[0].x / grid_size, segment.points[0].y / grid_size
    x1, y1 = segment.points[1].x / grid_size, segment.points[1].y / grid_size

    if abs(x1 - x0) > abs(y1 - y0):
        flip_xy = 1
    else:
        flip_xy = -1
        x0, y0 = y0, x0
        x1, y1 = y1, x1

    if x1 >= x0:
        x_sign = 1
    else:
        x_sign = -1
        x0 = -x0 + 1
        x1 = -x1 + 1

    if y1 >= y0:
        y_sign = 1
    else:
        y_sign = -1
        y0 = -y0 + 1
        y1 = -y1 + 1

    if x1 - x0:
        d_error = (y1 - y0) / (x1 - x0)
    else:
        d_error = 0
    y = d_error * (floor(x0) + 0.5 - x0) + y0
    y, error = floor(y), y - floor(y)

    for x in range(floor(x0), ceil(x1)):
        yield [x * x_sign, y * y_sign][::flip_xy]
        error += d_error
        if error >= 1:
            y += 1
            error -= 1


def distance(segment, point):
    """ Return the distance from a segment to a point """
    x0, y0 = point.x, point.y
    x1, y1 = segment.points[0].x, segment.points[0].y
    x2, y2 = segment.points[1].x, segment.points[1].y

    # ax + by + c = 0
    if x1 == x2:  # if vertical line
        a = 1
        b = 0
        c = -x1
    elif y1 == y2:  # if horizontal line
        a = 0
        b = 1
        c = -y1
    else:
        a = 1 / (x2 - x1)
        b = -1 / (y2 - y1)
        c = -a * x1 - b * y1

    # Calculate point F (closest point on line to point)
    xf = (b * (b * x0 - a * y0) - a * c) / (a ** 2 + b ** 2)
    yf = (a * (-b * x0 + a * y0) - b * c) / (a ** 2 + b ** 2)

    if min(x1, x2) <= xf <= max(x1, x2) and min(y1, y2) <= yf <= max(y1, y2):
        return ((xf-x0)**2+(yf-y0)**2)**0.5
    else:
        return min(((x1-x0)**2+(y1-y0)**2)**0.5, ((x2-x0)**2+(y2-y0)**2)**0.5)


class Hashable:
    """ Subclass and implement the __key__ property to make hashable. """
    def __hash__(self):
        return hash(self.__key__)

    def __eq__(self, other):
        return self.__key__ == other.__key__


class Sortable:
    """ Subclass and implement the __key__ property to make sortable. """

    def __lt__(self, other):
        return self.__key__ < other.__key__


class Multiton(Hashable, Sortable):
    """ Subclass must:
            -provide the Dictionary or WeakKeyDictionary 'instances'.
            -have an instance '__key__' property

        Subclass may:
            -check if it has already been initialized when __init__ is called using the 'initialized' flag.
                if it has, new arguments can be merged into the existing instance.
            -provide a non-zero start index
    """
    START_INDEX = 0

    def __new__(cls, *args, **kwargs):
        new_inst = super().__new__(cls)
        new_inst.i = None
        new_inst._initialized = False
        new_inst.__init__(*args, **kwargs)
        if new_inst not in cls.instances:
            # Weak value so instances are not kept alive if a WeakKeyDictionary is used in the subclass
            cls.instances[new_inst] = ref(new_inst)
        return cls.instances[new_inst]()

    @classmethod
    def indexed(cls):
        """ Return a list of all instances indexed by their keys. """
        # Force garbage collection to ensure there are no orphaned objects left in the weak dictionary
        gc.collect()
        sorted_instances = sorted(cls.instances, key=lambda instance: instance.__key__)
        for i, inst in enumerate(sorted_instances):
            inst.i = i + cls.START_INDEX
        return sorted_instances


class Boundary(Multiton):
    TYPE_PERIODIC = 3

    instances = WeakKeyDictionary()  # Strong reference held by the segment that contains it
    START_INDEX = 1  # Boundaries are 1-indexed (0 is reserved for no boundary)

    def __init__(self, name, boundary_type=TYPE_PERIODIC):
        if self._initialized:
            if boundary_type != self.type:
                raise ValueError('Boundary <{0:}> is type <{1:}>, can\'t create new <{0:}> of type <{2:}>.'.format(
                    self.name, self.type, boundary_type))
        else:
            self._initialized = True

            self.name = name
            self.type = boundary_type

    @property
    def __key__(self):
        return self.name

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


class BlockProperty(Multiton):
    instances = WeakKeyDictionary()  # Strong references held by the blocks that use it
    START_INDEX = 1  # BlockProperties are 1-indexed

    def __init__(self, name, conductivity=5.8e7):
        if self._initialized:
            if conductivity != self.conductivity:
                raise ValueError('BlockProperty <{}> has conductivity <{}>, can\'t change to <{}>.'.format(
                    self.name, self.conductivity, conductivity))
        else:
            self._initialized = True

            self.name = name
            self.conductivity = conductivity

    @property
    def __key__(self):
        return self.name

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


class Conductor(Multiton):
    TYPE_CURRENT = 0
    TYPE_VOLTAGE = 1

    instances = WeakKeyDictionary()  # Strong reference held by the segment that contains it
    START_INDEX = 1  # Conductors are 1-indexed (0 is reserved for no conductor)

    def __init__(self, name, conductor_type=TYPE_VOLTAGE, value=0.0):
        if self._initialized:
            if conductor_type != self.type:
                raise ValueError('Conductor <{0:}> is type <{1:}>, can\'t create new <{0:}> of type <{2:}>.'.format(
                    self.name, self.type, conductor_type))
            if value != self.value:
                raise ValueError('Conductor <{0:}> is value <{1:}>, can\'t create new <{0:}> of value <{2:}>.'.format(
                    self.name, self.value, value))
        else:
            self._initialized = True

            self.name = name
            self.type = conductor_type
            self.value = value

    @property
    def __key__(self):
        return self.name

    def __str__(self):
        if self.type == Conductor.TYPE_CURRENT:
            vcr = 0
            vci = 0
            qcr = self.value.real
            qci = self.value.imag
        elif self.type == Conductor.TYPE_VOLTAGE:
            vcr = self.value.real
            vci = self.value.imag
            qcr = 0
            qci = 0
        else:
            raise ValueError('Unknown conductor type <{}>'.format(self.type))

        return ('  <BeginConductor>\n'
                '    <ConductorName> = "{}"\n'
                '    <vcr> = {}\n'
                '    <vci> = {}\n'
                '    <qcr> = {}\n'
                '    <qci> = {}\n'
                '    <ConductorType> = {}\n'
                '  <EndConductor>\n').format(self.name, vcr, vci, qcr, qci, self.type)


class Point(Multiton):
    """ Point is a multiton, but points are put into small square bins, rather than being unique for every location. """
    instances = WeakKeyDictionary()  # Strong reference held by the segment that contains it
    START_INDEX = 0  # Points are 0-indexed

    # X and Y grid offsets.
    X_GRID_OFF = 0
    Y_GRID_OFF = 0

    def __init__(self, x, y, segments=None):
        if self._initialized:
            if segments:
                self.segments |= WeakSet(segments)
        else:
            self._initialized = True

            self.x = x
            self.y = y

            # Weak set of segments that contain the point to allow replacing points in segments.
            self.segments = WeakSet(segments) if segments else WeakSet()
            self._cached_key = None

    @property
    def __key__(self):
        if not self._cached_key:
            self._cached_key = (round(self.x / small_distance + Point.X_GRID_OFF),
                                round(self.y / small_distance + Point.Y_GRID_OFF))
        return self._cached_key

    def __str__(self):
        return '{}\t{}\t0\t0\t0\n'.format(self.x, self.y)


class Segment(Multiton):
    """ Segment is a multiton with strong references (must explicitly remove segments from the instances dict). """
    instances = {}

    def __init__(self, points, conductor=None, boundary=None, mesh_size=-1):
        if self._initialized:
            if conductor:
                self.conductor = conductor
            if boundary:
                self.boundary = boundary
            self.mesh_size = min(self.mesh_size, mesh_size)
        else:
            self._initialized = True

            self._points = None
            self._conductor = None
            self._boundary = None

            self.points = points
            self.conductor = conductor
            self.boundary = boundary
            self.mesh_size = mesh_size

    @property
    def points(self):
        return self._points

    @points.setter
    def points(self, points):
        self._points = tuple(points)

        # Add self to the weak set of each point used to allow points to back reference segments.
        for point in points:
            point.segments.add(self)

    @property
    def conductor(self):
        return self._conductor

    @conductor.setter
    def conductor(self, value):
        if value and value is not self._conductor:
            if self._conductor:
                raise ValueError('Can\'t assign segment conductor <{}>, already has conductor <{}>.'.format(
                    value.name, self._conductor.name))

            if self._boundary:
                raise ValueError('Can\'t assign segment conductor <{}>, already has boundary <{}>.'.format(
                    value.name, self._boundary.name))

            self._conductor = value

    @conductor.deleter
    def conductor(self):
        self._conductor = None

    @property
    def boundary(self):
        return self._boundary

    @boundary.setter
    def boundary(self, value):
        if value and value is not self._boundary:
            if self._conductor:
                raise ValueError('Can\'t assign segment boundary <{}>, already has conductor <{}>.'.format(
                    value.name, self._conductor.name))

            if self._boundary:
                raise ValueError('Can\'t assign segment boundary <{}>, already has boundary <{}>.'.format(
                    value.name, self._boundary.name))

            self._boundary = value

    @boundary.deleter
    def boundary(self):
        self._boundary = None

    @property
    def __key__(self):
        return self.points

    def __str__(self):
        bound = self.boundary.i if self.boundary else 0
        cond = self.conductor.i if self.conductor else 0
        return '{}\t{}\t{}\t{}\t0\t0\t{}\n'.format(self.points[0].i, self.points[1].i, self.mesh_size, bound, cond)


class Hole(Multiton):
    """ Holes are coordinates of 'No Mesh' labels. """
    instances = {}

    def __init__(self, x, y):
        if self._initialized:
            pass
        else:
            self._initialized = True

            self.x = x
            self.y = y

    @property
    def __key__(self):
        return self.x, self.y

    def __str__(self):
        return '{}\t{}\t0\n'.format(self.x, self.y)


class Block(Multiton):
    """ Blocks are coordinates and names of 'Block' labels. """
    instances = {}

    def __init__(self, x, y, block_property):
        if self._initialized:
            if self.block_property.name != block_property.name:
                raise ValueError('Block <{}> already at <({}, {})>,  can\'t create new block <{}>.'.format(
                    self.block_property.name, self.x, self.y, block_property.name))
        else:
            self._initialized = True

            self.x = x
            self.y = y
            self.block_property = block_property

    @property
    def __key__(self):
        return self.x, self.y

    def __str__(self):
        return '{}\t{}\t{}\t-1\t0\t0\n'.format(self.x, self.y, self.block_property.i)


@spinner('FEC post-process: Merging close points... ')
def _merge_close_points():
    """ Shift the point grid around by 0.5 in all directions to collect points that are close but just barely in
        different grid boxes.
    """
    # Loop over possible grid offsets
    for i, offset in enumerate([(0, 0.5), (0.5, 0.5), (0.5, 0)]):
        # Get a list of all points, then clear the point dictionary
        all_points = list(Point.instances)
        Point.instances = WeakKeyDictionary()

        # Change the grid offset
        Point.X_GRID_OFF, Point.Y_GRID_OFF = offset

        # Map old points to new points.
        new_point = {}
        for point in all_points:
            new_point[point] = Point(point.x, point.y)

        # Loop through segments, updating points according to the map.
        for segment in Segment.instances:
            segment.points = [new_point[point] for point in segment.points]


@spinner('FEC post-process: Resolving overlapping segments... ')
def _resolve_overlapping_segments():
    """ Process segments to remove overlaps.

        Assumes that no segments cross in the middle, since this should not be possible after the converter processing.
        Splits segments into two new segments wherever they are very nearly touching a point.
        No new points are created.
    """
    # Size the grid to cover the entire area with a small enough size that any single box contains very few points
    # but large enough that there are not too many boxes along any segment.
    grid_size = 0.1

    # Must also be at least twice as large as the 'small_distance' so that every point within 'small_distance' of a
    # segment is guaranteed to be found when enumerating the grid boxes along the segment.
    assert grid_size > 2 * small_distance

    # Place each point into its grid box, as will as the surrounding 8.
    point_map = defaultdict(set)
    for point in Point.instances:
        x_base, y_base = int(point.x // grid_size), int(point.y // grid_size)
        for x in range(x_base-1, x_base+2):
            for y in range(y_base - 1, y_base + 2):
                point_map[(x, y)].add(point)

    # Get a list of all segments, then clear the segment dictionary
    segments_to_process = list(Segment.instances)
    Segment.instances = {}

    # Pop segments and check for point intersections until none are left to process.
    while segments_to_process:
        segment = segments_to_process.pop()

        # Test points from all grid boxes the segment passes through
        split_points = set()
        for grid_box in bresenham(segment, grid_size):
            split_points |= point_map[tuple(grid_box)]

        unbroken = True
        for split_point in split_points:
            if (split_point not in segment.points) and (distance(segment, split_point) < small_distance):
                # Line broken by a point, and the point is not one of it's endpoints.
                # Split into 2 segments and add each to the list to process.
                for end_points in [(segment.points[0], split_point), (split_point, segment.points[1])]:
                    new_seg = Segment(end_points, segment.conductor, segment.boundary, segment.mesh_size)
                    Segment.instances.pop(new_seg, None)
                    segments_to_process.append(new_seg)
                unbroken = False
                break
        if unbroken:
            Segment.instances[segment] = ref(segment)


@spinner('FEC post-process: Removing zero length segments... ')
def _remove_zero_length_segments():
    """ Remove all segments whose start and end points are equal (zero length). """
    for segment in list(Segment.instances):
        if segment.points[0] == segment.points[1]:
            Segment.instances.pop(segment, None)


def write_out(file_name, precision=1e-8, frequency=0, min_angle=25, thickness=.035,
              comment="Auto generated by \'kicad_to_femm.py\'."):
    """ Write out the FEC File.
    Output is sorted so that meaningful diffs can be done between output files.

    Args:
        file_name: Output file path.
        precision: Solver precision, default is probably fine.
        frequency: Problem frequency. 0 for pure resistive DC calculations.
        min_angle: Minimum side angle for generated element triangles.
            Lower numbers result in fewer triangles (faster to solve), but may result in odd  output artifacts.
        thickness: Problem thickness in mm.
        comment:   Text comment to include in the fec file header.
    """
    with open(file_name, 'w') as f:
        # Write the header
        f.write(('[Format]      =  1\n'
                 '[Precision]   =  {:.1e}\n'
                 '[Frequency]   =  {}\n'
                 '[MinAngle]    =  {}\n'
                 '[Depth]       =  {}\n'
                 '[LengthUnits] =  millimeters\n'
                 '[ProblemType] =  planar\n'
                 '[Coordinates] =  cartesian\n'
                 '[Comment]     =  "{}"\n'
                 '[PointProps]  = 0\n').format(precision, frequency, min_angle, thickness, comment))

        # Geometry post-processing functions
        _merge_close_points()
        _resolve_overlapping_segments()
        _remove_zero_length_segments()

        # Write boundaries
        boundaries = Boundary.indexed()
        f.write('[BdryProps] = {}\n'.format(len(boundaries)))
        for boundary in boundaries:
            f.write(str(boundary))

        # Write block properties
        properties = BlockProperty.indexed()
        f.write('[BlockProps] = {}\n'.format(len(properties)))
        for block_prop in properties:
            f.write(str(block_prop))

        # Write conductors
        conductors = Conductor.indexed()
        f.write('[ConductorProps] = {}\n'.format(len(conductors)))
        for conductor in conductors:
            f.write(str(conductor))

        # Write points
        points = Point.indexed()
        f.write('[NumPoints] = {}\n'.format(len(points)))
        for point in points:
            f.write(str(point))

        # Write segments
        segments = Segment.indexed()
        f.write('[NumSegments] = {}\n'.format(len(segments)))
        for segment in segments:
            f.write(str(segment))

        # write arcs (Not Implemented)
        f.write('[NumArcSegments] = 0\n')

        # Write holes
        holes = Hole.indexed()
        f.write('[NumHoles] = {}\n'.format(len(holes)))
        for hole in holes:
            f.write(str(hole))

        # Write blocks
        blocks = Block.indexed()
        f.write('[NumBlockLabels] = {}\n'.format(len(blocks)))
        for block in blocks:
            f.write(str(block))
