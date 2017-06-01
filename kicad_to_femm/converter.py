"""
Convert a kicad-pcb file to an fec file for use with femm.

Generates polygons from all of the geometry contained in the kicad-pcb file.

Outputs only the geometry that is electrically connected to the pads that match the conductor specification.
"""
from math import atan2, pi, degrees
from itertools import chain

from shapely.geometry import Polygon, MultiPolygon, LineString, MultiLineString, Point, box
from shapely.ops import unary_union
from shapely.affinity import rotate, translate

from kicad_to_femm import viewer
import kicad_to_femm.fec_file as fec
from kicad_to_femm.layout import Layout
from kicad_to_femm.spinner import spinner


def distance(p1, p2):
    """ Distance between 2 points. """
    return ((p1.x-p2.x)**2 + (p1.y-p2.y)**2)**.5


def vertex_angle(p1, p2, p3):
    """ Vertex angle between 3 points (p2 vertex). Always returns the smaller angle formed. """
    a = atan2(p1.y - p2.y, p1.x - p2.x) - atan2(p3.y - p2.y, p3.x - p2.x)
    a %= 2 * pi
    if a > pi:
        a = 2*pi - a
    return degrees(a)


def calc_mesh_size(p1, p2, p3, p4):
    """ Determine the optimal mesh size for a segment given the segment points and the points before and after.
        A mesh size of -1 is 'auto'.
    """
    size = -1
    segment_length = distance(p2, p3)
    if segment_length < 1.0:
        if vertex_angle(p1, p2, p3) > 150 and vertex_angle(p2, p3, p4) > 150:
            size = segment_length / 2
    return size


class PadBase:
    DEFAULT_SMD_PAD_SIZE = 0.6

    def __init__(self, kicad_item):
        # SMD pad conductor size ratio
        # For example, a 2mm x 1mm pad with a ratio of 0.8 will be converted to a 1.6mm x 0.8mm pad
        # Values that give a reasonable real-world approximation would generally be between 0.5 - 0.8
        self._smd_pad_size = PadBase.DEFAULT_SMD_PAD_SIZE

        self.kicad_item = kicad_item

        # Fill in missing values if the source item is a via type
        if self.kicad_item.keyword == 'via':
            self.kicad_item.shape = 'circle'
            self.kicad_item.type = 'thru_hole'

        self._center = None
        self._hole_center = None
        self._copper_polygon = None
        self._conductor_polygon = None

        # Set of blocks the pad is a part of
        self.blocks = set()

    @property
    def smd_pad_size(self):
        return self._smd_pad_size

    @smd_pad_size.setter
    def smd_pad_size(self, value):
        if not (0.0 < value < 1.0):
            raise ValueError('smd_pad_size must be > 0.0 and < 1.0')
        self._smd_pad_size = value

    @property
    def center(self):
        """ Pad center point.
            If the source was a kicad pad item, the location is needs to be converted from module relative.
            If the source was a kicad via item, the location is already absolute.
        """
        if not self._center:
            self._center = Point(self.kicad_item.at.x, self.kicad_item.at.y)
            if self.kicad_item.keyword == 'pad':
                # Apply module rotation
                self._center = rotate(self._center, -self.kicad_item.parent.at.rot, (0, 0))

                # Apply module offset
                self._center = translate(self._center, self.kicad_item.parent.at.x, self.kicad_item.parent.at.y)

        return self._center

    @property
    def hole_center(self):
        """ Pad hole center point. """
        if not self._hole_center:
            if self.kicad_item.type == 'smd':
                self._hole_center = self.center

                # Apply Pad offset if it exists
                try:
                    offset = self.kicad_item.drill.offset
                    self._hole_center = translate(self._hole_center, offset.x, offset.y)
                except (AttributeError, IndexError):
                    pass

                # Rotate pad to it's final orientation
                self._hole_center = rotate(self._hole_center, -self.kicad_item.at.rot, self.center)

            elif self.kicad_item.type == 'thru_hole':
                self._hole_center = self.center

        return self._hole_center

    @property
    def conductor_polygon(self):
        """ Conductor polygon. Does not have any holes. """
        if not self._conductor_polygon:
            if self.kicad_item.type == 'thru_hole':

                if self.kicad_item.drill.shape == 'circle':
                    pad_poly = self.center.buffer(self.kicad_item.drill.size / 2)

                elif self.kicad_item.drill.shape == 'oval':
                    size_x, size_y = self.kicad_item.drill.size
                    radius = min(size_x, size_y) / 2
                    pad_line = LineString(((self.center.x + radius - size_x / 2, self.center.y + radius - size_y / 2),
                                           (self.center.x + size_x / 2 - radius, self.center.y + size_y / 2 - radius)))
                    pad_poly = pad_line.buffer(radius)

                else:
                    raise ValueError('Unknown pad drill shape <{}>.'.format(self.kicad_item.drill.shape))

                # Rotate pad drill to it's final orientation
                self._conductor_polygon = rotate(pad_poly, -self.kicad_item.at.rot, self.center).simplify(0.01)

            elif self.kicad_item.type == 'smd':
                # Start with the copper polygon.
                initial_area = self.copper_polygon.area
                goal_area = self.smd_pad_size * initial_area
                area_to_remove = initial_area - goal_area
                initial_perimeter = self.copper_polygon.length

                # First estimate of the margin needed
                margin = -area_to_remove / initial_perimeter

                # Iteratively reduce area error until 10 iterations have passed, or error is <1%
                for i in range(10):
                    self._conductor_polygon = self.copper_polygon.buffer(margin)
                    error = area_to_remove / (initial_area - self._conductor_polygon.area)
                    if abs(1 - error) < 0.01:
                        break
                    margin *= error

            else:
                raise ValueError('Unknown pad type <{}>'.format(self.kicad_item.type))

        return self._conductor_polygon

    @property
    def copper_polygon(self):
        """ Copper polygon. Does not have any holes. """
        if not self._copper_polygon:
            if self.kicad_item.shape == 'circle':
                pad_poly = self.center.buffer(self.kicad_item.size.x / 2)

            elif self.kicad_item.shape == 'oval':
                radius = min(self.kicad_item.size.x, self.kicad_item.size.y) / 2
                pad_line = LineString(((self.center.x + radius - self.kicad_item.size.x / 2,
                                        self.center.y + radius - self.kicad_item.size.y / 2),
                                       (self.center.x + self.kicad_item.size.x / 2 - radius,
                                        self.center.y + self.kicad_item.size.y / 2 - radius)))
                pad_poly = pad_line.buffer(radius)

            elif self.kicad_item.shape == 'rect':
                pad_poly = box(self.center.x - self.kicad_item.size.x / 2, self.center.y - self.kicad_item.size.y / 2,
                               self.center.x + self.kicad_item.size.x / 2, self.center.y + self.kicad_item.size.y / 2)

            elif self.kicad_item.shape == 'trapezoid':
                size_x = self.kicad_item.size.x
                size_y = self.kicad_item.size.y
                d = self.kicad_item.rect_delta
                pad_poly = Polygon((((-size_x - d.y) / 2, (size_y + d.x) / 2),
                                    ((size_x + d.y) / 2, (size_y - d.x) / 2),
                                    ((size_x - d.y) / 2, (-size_y + d.x) / 2),
                                    ((-size_x + d.y) / 2, (-size_y - d.x) / 2)))
                pad_poly = translate(pad_poly, self.center.x, self.center.y)

            else:
                raise ValueError('Unknown pad shape <{}>.'.format(self.kicad_item.shape))

            # Apply Pad offset if it exists
            try:
                offset = self.kicad_item.drill.offset
                pad_poly = translate(pad_poly, offset.x, offset.y)
            except (AttributeError, IndexError):
                pass

            # Rotate pad to it's final orientation
            self._copper_polygon = rotate(pad_poly, -self.kicad_item.at.rot, self.center).simplify(0.01)

        return self._copper_polygon

    def segments(self, layer):
        """ Return a list of fec segments that form the conductor polygon.
            Conductors and boundaries can be filled in for the returned segments.

            The provided layer is used to place the polygon in the fec layout space before returning segments.
        """
        segment_list = []
        placed_polygon = Layout.place(self.conductor_polygon, layer)
        points = [fec.Point(*coord) for coord in placed_polygon.exterior.coords[:-1]]
        l = len(points)
        for i in range(l):
            mesh_size = calc_mesh_size(points[(i - 1) % l], points[i], points[(i + 1) % l], points[(i + 2) % l])
            segment = fec.Segment((points[i], points[(i + 1) % l]), mesh_size=mesh_size)
            segment_list.append(segment)
        return segment_list


class Pad(PadBase):
    def __init__(self, kicad_item):
        # kicad_item can have the keyword 'via' or 'pad'
        super().__init__(kicad_item)
        self._conductor = None

    @property
    def conductor(self):
        return self._conductor

    @conductor.setter
    def conductor(self, value):
        if not self._conductor:
            self._conductor = value
        else:
            raise ValueError('Pad conductor already set to <{}>. Cannot set to <{}>'.format(
                    self._conductor.name, value.name))

    @conductor.deleter
    def conductor(self):
        self._conductor = None

    def to_fec(self, fec_file):
        for layer in Layout.layers:
            if self.kicad_item.has_layer(layer):
                for segment in self.segments(layer):
                    segment.conductor = self.conductor
                    fec_file.add_segment(segment)
                hole = Layout.place(self.hole_center, layer)
                fec_file.holes.append(fec.Hole(hole.x, hole.y))


class Via(PadBase):
    index = 0  # Index used to give unique names to via segment boundary conditions

    def __init__(self, kicad_item):
        super().__init__(kicad_item)

    def unrolled_via_segments(self, rolled_segments):
        """ Return fec segments that form the unrolled via.
            Segments are returned in separate lists based on which boundaries they will be a part of.
            Mesh size is set to match the corresponding segment in the rolled via.
        """
        # Unroll
        poly_coords = self.conductor_polygon.exterior.coords
        total_length = 0
        flat_coords = [(total_length, 0)]
        for i in range(len(poly_coords) - 1):
            total_length += distance(Point(poly_coords[i]), Point(poly_coords[i + 1]))
            # '-total_length' is used so the periodic boundary conditions applied to each segment line up end to end.
            # If 'total_length' was used instead, the boundary conditions would be reversed for each segment.
            flat_coords.append((-total_length, 0))

        # Create top line
        top_line = LineString(flat_coords)

        # Create bottom line
        bottom_line = translate(LineString(flat_coords), 0, -Layout.board_thickness)

        # Create side lines
        left_side = LineString((top_line.coords[0], bottom_line.coords[0]))
        right_side = LineString((top_line.coords[-1], bottom_line.coords[-1]))

        # Combine
        unrolled_via = MultiLineString((top_line, bottom_line, left_side, right_side))

        # Place in layout
        top_line, bottom_line, left_side, right_side = Layout.place_via(unrolled_via)

        # Collect segments
        top_segments = []
        top_points = [fec.Point(*coord) for coord in top_line.coords]
        for i in range(len(top_line.coords)-1):
            segment = fec.Segment((top_points[i], top_points[i + 1]), mesh_size=rolled_segments[i].mesh_size)
            top_segments.append(segment)

        bottom_segments = []
        bottom_points = [fec.Point(*coord) for coord in bottom_line.coords]
        for i in range(len(top_line.coords)-1):
            segment = fec.Segment((bottom_points[i], bottom_points[i + 1]), mesh_size=rolled_segments[i].mesh_size)
            bottom_segments.append(segment)

        side_segments = [fec.Segment((top_points[0], bottom_points[0]), mesh_size=-1),
                         fec.Segment((top_points[-1], bottom_points[-1]), mesh_size=-1)]

        return top_segments, bottom_segments, side_segments

    def to_fec(self, fec_file):
        # Get the rolled segments
        layer_segments = []
        for layer in Layout.layers:
            if self.kicad_item.has_layer(layer):
                layer_segments.append(self.segments(layer))
                hole = Layout.place(self.center, layer)
                fec_file.holes.append(fec.Hole(hole.x, hole.y))

        # Get the unrolled segments
        top_segments, bottom_segments, side_segments = self.unrolled_via_segments(layer_segments[0])

        # Place the block label
        x = (top_segments[0].points[0].x + top_segments[-1].points[1].x) / 2
        y = (side_segments[0].points[0].y + side_segments[0].points[1].y) / 2
        fec_file.blocks.append(fec.Block(x, y, 2))  # Via material index hard coded at 2, can change if needed

        # Add side segments
        boundary = fec.Boundary('via_{}_vert'.format(Via.index), fec.Boundary.TYPE_PERIODIC)
        for segment in side_segments:
            segment.boundary = boundary
            fec_file.add_segment(segment)

        # Add top and bottom segments
        for i in range(len(layer_segments[0])):
            top_rolled_seg = layer_segments[0][i]
            bottom_rolled_seg = layer_segments[1][i]

            top_unrolled_seg = top_segments[i]
            bottom_unrolled_seg = bottom_segments[i]

            top_boundary = fec.Boundary('via_{}_s{}_t'.format(Via.index, i), fec.Boundary.TYPE_PERIODIC)
            bottom_boundary = fec.Boundary('via_{}_s{}_b'.format(Via.index, i), fec.Boundary.TYPE_PERIODIC)

            top_rolled_seg.boundary = top_boundary
            top_unrolled_seg.boundary = top_boundary
            bottom_rolled_seg.boundary = bottom_boundary
            bottom_unrolled_seg.boundary = bottom_boundary

            fec_file.add_segment(top_rolled_seg)
            fec_file.add_segment(top_unrolled_seg)
            fec_file.add_segment(bottom_rolled_seg)
            fec_file.add_segment(bottom_unrolled_seg)

        Via.index += 1


class Block:
    def __init__(self, polygon, layer):
        self.polygon = polygon.simplify(0.01)
        self.layer = layer

        # Sets of pads and vias intersecting the block
        self.pads = set()
        self.vias = set()

    @property
    def segments(self):
        """ Return a list of fec segments that form the block polygon. """
        segment_list = []
        placed_polygon = Layout.place(self.polygon, self.layer)
        for ring in (placed_polygon.exterior, *placed_polygon.interiors):
            points = [fec.Point(*coord) for coord in ring.coords[:-1]]
            l = len(points)
            for i in range(l):
                mesh_size = calc_mesh_size(points[(i - 1) % l], points[i], points[(i + 1) % l], points[(i + 2) % l])
                segment = fec.Segment((points[i], points[(i + 1) % l]), mesh_size=mesh_size)
                segment_list.append(segment)
        return segment_list

    def to_fec(self, fec_file):
        # Segments
        for segment in self.segments:
            fec_file.add_segment(segment)

        # Holes
        for interior in self.polygon.interiors:
            hole = Polygon(interior.coords[:-1]).representative_point()
            hole = Layout.place(hole, self.layer)
            fec_file.holes.append(fec.Hole(hole.x, hole.y))

        # Block (remove intersecting pads from the block before placing the block label)
        intersecting_pads = unary_union([pad.conductor_polygon for pad in self.pads | self.vias])
        point = self.polygon.difference(intersecting_pads).representative_point()
        point = Layout.place(point, self.layer)
        fec_file.blocks.append(fec.Block(point.x, point.y))


class Converter:
    def __init__(self, conductor_specs, layers, bounds=None, board_thickness=None):
        self.conductor_specs = conductor_specs
        self.layers = layers
        self.bounds = box(*bounds) if bounds else None
        self.board_thickness = board_thickness

        self.pads = []
        self.vias = []
        self.blocks = []

    def show(self):
        """ Show all blocks, pads, and vias in a viewer window. """
        window = viewer.Window(mirror_y=True)

        window.add_polygon_group([block.polygon for block in self.blocks if block.layer == self.layers[1]],
                                 fill_color=(0.3, .9, 0.3, 0.7), line_color=(0, 0.4, 0, 0.8))

        window.add_polygon_group([block.polygon for block in self.blocks if block.layer == self.layers[0]],
                                 fill_color=(.9, 0, 0, 0.4), line_color=(0.4, 0, 0, 0.8))

        window.add_polygon_group([via.conductor_polygon for via in self.vias],
                                 fill_color=(0.6, 0.6, 0.6, 1), line_color=(0, 0, 0, 0.8))

        window.add_polygon_group(
            [pad.conductor_polygon for pad in self.pads if pad.kicad_item.has_layer(self.layers[1])],
            fill_color=(0, 0.6, 0, 0.5), line_color=(0, 0.3, 0, 0.5))

        window.add_polygon_group(
            [pad.conductor_polygon for pad in self.pads if pad.kicad_item.has_layer(self.layers[0])],
            fill_color=(0.6, 0, 0, 0.5), line_color=(0.3, 0, 0, 0.5))

        window.show()

    @spinner('Writing FEC output... ')
    def write_out(self, fec_file):
        # Set up the layout
        layer_bounds = []
        for layer in self.layers:
            polygons = [block.polygon for block in self.blocks if block.layer == layer]
            layer_bounds.append(MultiPolygon(polygons).bounds)
        Layout(self.layers, layer_bounds, self.board_thickness)

        # Generate FEC data for all pads
        for pad in self.pads:
            pad.to_fec(fec_file)

        # Generate FEC data for all vias
        # Sort by x-coord, then y so the via index roughly corresponds to board position
        self.vias.sort(key=lambda via: via.center.x)
        self.vias.sort(key=lambda via: via.center.y)
        for via in self.vias:
            via.to_fec(fec_file)

        # Generate FEC data for all blocks
        for block in self.blocks:
            block.to_fec(fec_file)

    def read_in(self, kicad_pcb):
        # Find all pads (including vias)
        self.find_pads(kicad_pcb)

        # Assign conductors to pads based on the conductor spec
        self.assign_conductors()

        # Find vias (all through-hole pads with no conductor set)
        self.find_vias()

        # Find blocks (copper area made from zones, traces, pads, and vias)
        self.find_blocks(kicad_pcb)

        # Remove pads that have no conductors assigned to them
        self.remove_unconnected_pads()

        # Prune blocks that do not connect back to a conductor (pad)
        # Also removes any vias in the pruned blocks.
        self.prune_blocks()

    @spinner('Finding pads... ')
    def find_pads(self, kicad_pcb):
        """ Find all pads matching the net, within the bounds (including vias). """
        pads_and_vias = chain(*(module.pads for module in kicad_pcb.modules), kicad_pcb.vias)
        for kicad_pad in pads_and_vias:
            if any(kicad_pad.has_layer(layer) for layer in self.layers):
                new_pad = Pad(kicad_pad)
                if (not self.bounds) or self.bounds.contains(new_pad.center):
                    self.pads.append(new_pad)

    @spinner('Assigning conductors... ')
    def assign_conductors(self):
        """ Assign conductors to pads based on the conductor specs. """
        for pad in self.pads:
            for conductor_spec in self.conductor_specs:
                if conductor_spec.match(pad):
                    pad.conductor = conductor_spec.conductor
                    if conductor_spec.smd_pad_size:
                        pad.smd_pad_size = conductor_spec.smd_pad_size

    @spinner('Finding vias... ')
    def find_vias(self):
        """ Convert through-hole pads without a conductor to vias. """
        remaining_pads = []
        for pad in self.pads:
            if (not pad.conductor) and pad.kicad_item.type == 'thru_hole':
                self.vias.append(Via(pad.kicad_item))
            else:
                remaining_pads.append(pad)
        self.pads = remaining_pads

    @spinner('Finding blocks... ')
    def find_blocks(self, kicad_pcb):
        """ Find all blocks (blocks are made of the union of zones, segments, vias, and pads)
            Cut to the bounds if given.
        """
        for layer in self.layers:
            # Add zone filled polygons to the list
            polygons = []
            for zone in kicad_pcb.zones:
                if zone.layer == layer:
                    for filled_polygon in zone.filled_polygons:
                        base = Polygon(filled_polygon.points)
                        polygons.append(base.buffer(zone.min_thickness / 2))

            # Add trace polygons to the list
            for segment in kicad_pcb.segments:
                if segment.layer == layer:
                    base = LineString(segment.endpoints)
                    polygons.append(base.buffer(segment.width / 2))

            # Merge, and then cut by the bounds, if given
            polygons = unary_union(polygons)
            if self.bounds:
                polygons = self.bounds.intersection(polygons)
            polygons = [polygons]

            # Add pad/via polygons on the given layer
            for pad in self.pads + self.vias:
                if pad.kicad_item.has_layer(layer):
                    polygons.append(pad.copper_polygon)
            polygons = unary_union(polygons)

            # Result may be a single polygon, or a list of polygons
            try:
                self.blocks.extend(Block(polygon, layer) for polygon in polygons)
            except TypeError:
                self.blocks.append(Block(polygons, layer))

    @spinner('Removing unconnected pads... ')
    def remove_unconnected_pads(self):
        # Remove pads that have no conductors assigned to them
        remaining_pads = []
        for pad in self.pads:
            if pad.conductor:
                remaining_pads.append(pad)
        self.pads = remaining_pads

    @spinner('Pruning blocks... ')
    def prune_blocks(self):
        """ Use vias to connect all blocks, starting from conductors (pads).
            Remove any blocks that do not connect back to a conductor (pad).
            Also removes any vias that are in the unconnected blocks.
        """
        # Get the set of blocks containing a pad (active blocks)
        # Match pads/vias to their containing blocks
        active_blocks = set()
        for block in self.blocks:
            for pad in self.pads:
                if pad.kicad_item.has_layer(block.layer) and pad.center.within(block.polygon):
                    active_blocks.add(block)
                    block.pads.add(pad)
                    pad.blocks.add(block)
            for via in self.vias:
                if via.kicad_item.has_layer(block.layer) and via.center.within(block.polygon):
                    block.vias.add(via)
                    via.blocks.add(block)

        # Traverse the connected blocks starting from each active block
        unchecked_active_blocks = active_blocks.copy()
        while unchecked_active_blocks:
            block = unchecked_active_blocks.pop()
            for via in block.vias:
                for block in via.blocks:
                    if block not in active_blocks:
                        active_blocks.add(block)
                        unchecked_active_blocks.add(block)

        # Get all vias from active blocks
        active_vias = set()
        for block in active_blocks:
            active_vias |= block.vias

        # Replace the lists of vias and blocks with pruned lists
        self.vias = list(active_vias)
        self.blocks = list(active_blocks)


class ConductorSpec:
    """ Conductor Spec, can be assigned to one or more pads. """
    def __init__(self, spec):
        self.net_name = ''
        self.smd_pad_size = None
        self.modules = []
        self.regions = []

        name = ''
        value_str = '0V'
        for field, item in spec.items():
            if field == 'name':
                name = item
            elif field == 'value':
                value_str = item
            elif field == 'net':
                self.net_name = item
            elif field == 'smd_pad_size':
                self.smd_pad_size = item
            elif field == 'modules':
                self.modules = item
            elif field == 'regions':
                self.regions = item

        # Parse the value
        value = float(value_str[:-1])
        type_str = value_str[-1]
        if type_str in 'Vv':
            conductor_type = fec.Conductor.TYPE_VOLTAGE
        elif type_str in 'AaIi':
            conductor_type = fec.Conductor.TYPE_CURRENT
        else:
            raise ValueError('Unknown conductor type <{}>'.format(type_str))

        # Set the fec conductor for the spec
        self.conductor = fec.Conductor(name, conductor_type, value)

    def match(self, pad):
        """ Returns true if the pad matches the conductor spec. """
        # Does not match the specification if a net name was given and it doesn't match
        if self.net_name and pad.kicad_item.net.name != self.net_name:
            return False

        # Check against the regions
        for region_spec in self.regions:
            if box(*region_spec).contains(pad.center):
                return True

        # Check against the modules
        try:
            for module_spec in self.modules:
                if pad.kicad_item.parent.reference == module_spec[0]:
                    # If the module spec length is 1, then there are no pads named and all are allowed
                    if len(module_spec) == 1 or pad.kicad_item.number in module_spec[1:]:
                        return True
        except AttributeError:
            # Pad is not part of a module (does not have a reference)
            pass

        # Does not match any region or module spec
        return False
