"""
Convert a kicad-pcb file to an fec file for use with femm.

Generates polygons from all of the geometry contained in the kicad-pcb file.

Outputs only the geometry that is electrically connected to the pads that match the conductor specification.
"""
import sys
import collections
from math import atan2, pi, degrees
from itertools import chain

from shapely.geometry import Polygon, MultiPolygon, LineString, MultiLineString, Point, box, JOIN_STYLE
from shapely.ops import unary_union
from shapely.affinity import rotate, translate

try:
    from kicad_to_femm import viewer
except ImportError:
    viewer = None
from kicad_to_femm import fec
from kicad_to_femm.layout import Layout
from kicad_to_femm.spinner import spinner


def make_iter(x):
    """ Helper to handle returns from shapely operations that may or may not result in an iterable. """
    return x if isinstance(x, collections.Iterable) else (x,)


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

        This is needed because the auto setting generates unnecessarily dense meshes for lines approximating curves,
        since it doesn't appear to account for segment angles.
    """
    size = -1
    segment_length = distance(p2, p3)
    if segment_length < 1.0:
        if vertex_angle(p1, p2, p3) > 150 and vertex_angle(p2, p3, p4) > 150:
            size = segment_length / 2
    return size


class PadBase:
    def __init__(self, kicad_item=None, pad_type='', layers=None):
        self.kicad_item = kicad_item
        self.layers = []

        if self.kicad_item:
            # Fill in missing values if the source item is a via type
            if self.kicad_item.keyword == 'via':
                self.kicad_item.shape = 'circle'
                self.kicad_item.type = 'thru_hole'

            self.type = self.kicad_item.type
            self.layers = [layer for layer in Layout.layers if self.kicad_item.has_layer(layer)]

        # Set the pad type and layers
        if pad_type:
            self.type = pad_type
        if layers:
            self.layers = layers

        self._center = None
        self._copper_polygon = None

        self._conductor_spec = None
        self._conductor_polygon = None

        # Set of blocks the pad is a part of
        self.blocks = set()

    @property
    def conductor_spec(self):
        return self._conductor_spec

    @conductor_spec.setter
    def conductor_spec(self, spec):
        if not self._conductor_spec:
            self._conductor_spec = spec
        else:
            current_name = ''
            new_name = ''
            try:
                current_name = self._conductor_spec.conductor.name
            except AttributeError:
                pass
            try:
                new_name = spec.conductor.name
            except AttributeError:
                pass
            raise ValueError('Pad conductor already set to <{}>. Cannot set to <{}>'.format(current_name, new_name))

    @conductor_spec.deleter
    def conductor_spec(self):
        self._conductor_spec = None

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

    @center.setter
    def center(self, value):
        self._center = value

    @property
    def conductor_polygon(self):
        if not self._conductor_polygon:
            if self.type == 'thru_hole':

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

            elif self.type == 'smd':
                # Start with the copper polygon.
                initial_area = self.copper_polygon.area
                goal_area = self.conductor_spec.smd_pad_area_ratio * initial_area

                # First estimate of the margin needed
                margin = -(initial_area - goal_area) / self.copper_polygon.length

                # Iteratively reduce area error until 10 iterations have passed, or error is < 1%
                for i in range(10):
                    polygon = self.copper_polygon.buffer(margin, join_style=JOIN_STYLE.mitre, mitre_limit=10)
                    if abs(1 - polygon.area / goal_area) < 0.001:
                        break
                    margin -= (polygon.area - goal_area) / polygon.length
                self._conductor_polygon = polygon.simplify(0.01)

            else:
                raise ValueError('Unknown pad type <{}>'.format(self.type))

        return self._conductor_polygon

    @property
    def copper_polygon(self):
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

    @copper_polygon.setter
    def copper_polygon(self, polygon):
        self._copper_polygon = polygon

    def generate_conductor_segments(self, layer):
        """ Return a list of fec segments that form the conductor polygon.
            Conductors and boundaries can be filled in for the returned segments.

            The provided layer is used to place the polygon in the fec layout space before returning segments.
        """
        segment_list = []
        for linestring in make_iter(Layout.place(self.conductor_polygon.boundary, layer)):
            points = [fec.Point(*coord) for coord in linestring.coords]
            l = len(points) - 1
            for i in range(l):
                mesh_size = calc_mesh_size(points[(i - 1) % l], points[i], points[(i + 1) % l], points[(i + 2) % l])
                segment = fec.Segment((points[i], points[i + 1]), mesh_size=mesh_size)
                segment_list.append(segment)
        return segment_list


class Pad(PadBase):
    def generate_fec(self):
        # Segments only (labels are handled by the CopperLayer class)
        for layer in self.layers:
            for segment in self.generate_conductor_segments(layer):
                assert segment in fec.Segment.instances
                segment.conductor = self.conductor_spec.conductor


class Via(PadBase):
    index = 0  # Index used to give unique names to via segment boundary conditions

    def generate_unrolled_segments(self, rolled_segments):
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

    def generate_fec(self):
        # Get the rolled drill segments
        layer_segments = []
        for layer in self.layers:
            layer_segments.append(self.generate_conductor_segments(layer))

        # Only output unrolled segments if the Layout has 2 layers, and the via is between them
        if len(Layout.layers) == 2 and set(self.layers) == set(Layout.layers):
            # Get the unrolled segments
            top_segments, bottom_segments, side_segments = self.generate_unrolled_segments(layer_segments[0])

            # Place the block label
            x = (top_segments[0].points[0].x + top_segments[-1].points[1].x) / 2
            y = (side_segments[0].points[0].y + side_segments[0].points[1].y) / 2
            fec.Block(x, y, Layout.via_property)

            # Add side segments
            boundary = fec.Boundary('via_{}_vert'.format(Via.index), fec.Boundary.TYPE_PERIODIC)
            for segment in side_segments:
                segment.boundary = boundary

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

            Via.index += 1


class Block:
    def __init__(self, polygon, layer):
        self.polygon = polygon
        self.layer = layer

        # Sets of pads and vias intersecting the block
        self.pads = set()
        self.vias = set()

    def generate_segments(self):
        """ Return a list of fec segments that form the block polygon. """
        placed_boundary = Layout.place(self.polygon.boundary, self.layer)

        segment_list = []
        for linestring in make_iter(placed_boundary):
            points = [fec.Point(*coord) for coord in linestring.coords]
            l = len(points) - 1
            for i in range(l):
                if points[0] == points[-1]:  # Replaces is_ring because is_ring is very slow.
                    mesh_size = calc_mesh_size(points[(i - 1) % l], points[i], points[(i + 1) % l], points[(i + 2) % l])
                else:
                    try:
                        mesh_size = calc_mesh_size(points[i - 1], points[i], points[i + 1], points[i + 2])
                    except IndexError:
                        # Can't find angles for end segments, use the default mesh sizing
                        mesh_size = -1
                segment = fec.Segment((points[i], points[i + 1]), mesh_size=mesh_size)
                segment_list.append(segment)

        return segment_list

    def generate_fec(self):
        # Segments only (labels are handled by the CopperLayer class)
        self.generate_segments()


class CopperLayer:
    def __init__(self, layer):
        self.layer = layer   # Name of the copper layer
        self.blocks = set()  # Blocks are positive area

    def generate_fec(self):
        """ Writes all holes and block labels. """
        # Merge all blocks together
        copper_polygons = unary_union([block.polygon for block in self.blocks])

        if copper_polygons:
            # Cut the copper from a buffered bounding rectangle
            bounding_box = box(*copper_polygons.bounds).buffer(1, join_style=JOIN_STYLE.mitre, mitre_limit=10)
            hole_polygons = bounding_box.difference(copper_polygons)

            # Generate hole labels from all inverted hole polygons (except the outer bounding rectangle)
            for polygon in make_iter(hole_polygons):
                if polygon.bounds != bounding_box.bounds:
                    point = Layout.place(polygon.representative_point(), self.layer)
                    fec.Hole(point.x, point.y)

            # Cut out pad/via conductor holes (use block.pads, and block.vias)
            conductors = unary_union([pad.conductor_polygon for b in self.blocks for pad in b.pads | b.vias])
            copper_polygons = copper_polygons.difference(conductors)

            # Buffer by a small negative amount to prevent labels being placed in small slivers
            copper_polygons = copper_polygons .buffer(-1e-3, join_style=JOIN_STYLE.mitre, mitre_limit=10)

            # Generate copper block labels from all polygons
            for polygon in make_iter(copper_polygons):
                point = Layout.place(polygon.representative_point(), self.layer)
                fec.Block(point.x, point.y, Layout.copper_property)

            # Label conductor holes
            for polygon in conductors:
                point = Layout.place(polygon.representative_point(), self.layer)
                fec.Hole(point.x, point.y)


class Converter:
    def __init__(self, conductor_specs, layers, bounds=None):
        self.conductor_specs = conductor_specs
        self.layers = layers
        self.bounds = box(*bounds) if bounds else None

        self.pads = []
        self.no_conductor_pads = []
        self.vias = []
        self.blocks = []
        self.copper_layers = {layer: CopperLayer(layer) for layer in self.layers}

    def show(self):
        """ Show all blocks, pads, and vias in a viewer window. """
        if not viewer:
            print('Viewer was not loaded, cannot show output. (check for import errors).', file=sys.stderr)
            return

        window = viewer.Window(mirror_y=True)
        try:
            window.add_polygon_group([block.polygon for block in self.blocks if block.layer == self.layers[1]],
                                     fill_color=(0.3, .9, 0.3, 0.7), line_color=(0, 0.4, 0, 0.8))
        except IndexError:
            pass

        window.add_polygon_group([block.polygon for block in self.blocks if block.layer == self.layers[0]],
                                 fill_color=(.9, 0, 0, 0.4), line_color=(0.4, 0, 0, 0.8))

        window.add_polygon_group([via.conductor_polygon for via in self.vias],
                                 fill_color=(0.6, 0.6, 0.6, 1), line_color=(0, 0, 0, 0.8))

        try:
            window.add_polygon_group(
                [pad.conductor_polygon for pad in self.pads if self.layers[1] in pad.layers],
                fill_color=(0, 0.6, 0, 0.5), line_color=(0, 0.3, 0, 0.5))
        except IndexError:
            pass

        window.add_polygon_group(
            [pad.conductor_polygon for pad in self.pads if self.layers[0] in pad.layers],
            fill_color=(0.6, 0, 0, 0.5), line_color=(0.3, 0, 0, 0.5))

        window.show()

    @spinner('Generating FEC output... ')
    def generate_output(self):
        # Set up the layout bounds
        layer_bounds = []
        for layer in self.layers:
            polygons = [block.polygon for block in self.blocks if block.layer == layer]
            layer_bounds.append(MultiPolygon(polygons).bounds)
        Layout.set_bounds(layer_bounds)

        # Generate FEC data for all pads
        for pad in self.pads:
            pad.generate_fec()

        # Generate FEC data for all vias
        # Sort vias by position so the via index roughly corresponds to board position
        self.vias.sort(key=lambda via: (via.center.y, via.center.x))
        for via in self.vias:
            via.generate_fec()

        # Generate FEC data for all blocks
        for block in self.blocks:
            block.generate_fec()

        # Generate FEC data for all copper layers
        for copper_layer in self.copper_layers.values():
            copper_layer.generate_fec()

    def parse_input(self, kicad_pcb):
        # Find all pads (including vias)
        self.find_pads(kicad_pcb)

        # Assign conductors to pads based on the conductor spec
        self.assign_conductors()

        # Find vias (all through-hole pads with no conductor set)
        self.find_vias()

        # Merge pads of the same conductor if they overlap
        self.merge_overlapping_pads()

        # Find blocks (copper area made from zones, traces, pads, and vias)
        self.find_blocks(kicad_pcb)

        # Prune blocks that do not connect back to a conductor (pad)
        # Also removes any vias in the pruned blocks.
        self.prune_blocks()

    @spinner('Finding pads... ')
    def find_pads(self, kicad_pcb):
        """ Find all pads within the bounds (including vias). """
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
                    pad.conductor_spec = conductor_spec
                    conductor_spec.pads.add(pad)

    @spinner('Finding vias... ')
    def find_vias(self):
        """ Convert through-hole pads without a conductor to vias. """
        all_pads = self.pads
        self.pads = []
        self.no_conductor_pads = []
        for pad in all_pads:
            if pad.conductor_spec:
                self.pads.append(pad)
            elif pad.type == 'thru_hole':
                self.vias.append(Via(pad.kicad_item))
            else:
                self.no_conductor_pads.append(pad)

    @spinner('Merging overlapping SMD pads... ')
    def merge_overlapping_pads(self):
        self.pads = set()
        for spec in self.conductor_specs:
            # Separate conductor_spec SMD pads and through hole pads
            all_spec_pads = spec.pads
            spec.pads = set()
            smd_pads = []
            for pad in all_spec_pads:
                if pad.type == 'smd':
                    smd_pads.append(pad)
                else:
                    spec.pads.add(pad)

            for layer in self.layers:
                # Merge overlapping pads with a union, then split into new pads.
                layer_pads = []
                for pad in smd_pads:
                    if layer in pad.layers:
                        layer_pads.append(pad)
                for polygon in make_iter(unary_union([pad.copper_polygon for pad in layer_pads]).simplify(0.01)):
                    new_pad = Pad(layer_pads[0].kicad_item, pad_type='smd', layers=[layer])
                    new_pad.copper_polygon = polygon
                    new_pad.center = polygon.representative_point()
                    new_pad.conductor_spec = spec
                    spec.pads.add(new_pad)
                self.pads |= spec.pads
        self.pads = list(self.pads)

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

            # Merge, and then cut by the bounds, if given (simplify before adding pads, so that pads are added exactly
            # This allows subtracting conductor pads out later if needed.
            polygons = unary_union(polygons).simplify(0.01)
            if self.bounds:
                polygons = self.bounds.intersection(polygons)
            polygons = [polygons]

            # Add pad/via polygons on the given layer
            for pad in self.pads + self.no_conductor_pads + self.vias:
                if layer in pad.layers:
                    polygons.append(pad.copper_polygon)
            polygons = unary_union(polygons)

            self.blocks.extend(Block(polygon, layer) for polygon in make_iter(polygons))

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
                if block.layer in pad.layers and pad.center.within(block.polygon):
                    active_blocks.add(block)
                    block.pads.add(pad)
                    pad.blocks.add(block)
            for via in self.vias:
                if block.layer in via.layers and via.center.within(block.polygon):
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

        # Get all vias from active blocks, also assign blocks to a 'copper_layer'
        active_vias = set()
        for block in active_blocks:
            self.copper_layers[block.layer].blocks.add(block)
            active_vias |= block.vias

        # Replace the lists of vias and blocks with pruned lists
        self.vias = list(active_vias)
        self.blocks = list(active_blocks)


class ConductorSpec:
    """ Conductor Spec, can be assigned to one or more pads. """
    def __init__(self, spec):
        self.net_name = ''

        # SMD pad conductor are ratio
        # This is the ratio of the area of the SMD pad copper to the SMD pad conductor.
        # Reasonable values are typically around 0.4 to 0.6
        # If not set in the spec, the value here is taken as the default
        self.smd_pad_area_ratio = 0.5

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
            elif field == 'pad_area':
                if not (0.0 < item <= 1.0):
                    raise ValueError('smd_pad_area_ratio must be > 0.0 and <= 1.0')
                self.smd_pad_area_ratio = item
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

        # Set of pads matching the conductor spec
        self.pads = set()

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
