"""
Layout converts the multi-layer geometry of the kicad_pcb file to a single plane for outputting to the FEC file.

The bottom layer is shifted to the right of the top layer and mirrored relative to the y-axis.

The unrolled vias are placed in rows below the top and bottom layers,
wrapping when the via row extends past the right edge top and bottom layers.
"""
from shapely.affinity import translate, affine_transform


class Layout:
    """ Layout and arrange multiple layers in a single 2d plane. """
    layers = []
    top = ''
    bottom = ''
    bottom_x_off = 0
    via_y_max = 0
    via_x_min = 0
    via_row_x_min = 0
    via_row_x_max = 0
    via_row_height = 0

    board_thickness = 0
    config_initialized = False
    bounds_initialized = False

    # Placement clearance between different layers / unrolled vias
    clearance = 0

    # Copper and via block properties
    copper_property = None
    via_property = None

    @staticmethod
    def set_config(layers, board_thickness, copper_property, via_property, clearance=0.5):
        # Unpack names and bounds for the top and bottom layers
        Layout.layers = layers
        try:
            Layout.top = layers[0]
        except IndexError:
            raise IndexError('Layout has not been given any layers.')
        try:
            Layout.bottom = layers[1]
        except IndexError:
            Layout.bottom = ''

        # Set the board thickness
        Layout.board_thickness = board_thickness

        # Copper and via block properties
        Layout.copper_property = copper_property
        Layout.via_property = via_property

        # Clearance between placed elements
        Layout.clearance = clearance

        # Set config initialized flag
        Layout.config_initialized = True

    @staticmethod
    def set_bounds(bounds=((0, 0, 0, 0), (0, 0, 0, 0))):
        try:
            t_x_min, t_y_min, t_x_max, t_y_max = bounds[0]
        except (IndexError, ValueError):
            t_x_min, t_y_min, t_x_max, t_y_max = (0, 0, 0, 0)

        try:
            b_x_min, b_y_min, b_x_max, b_y_max = bounds[1]
        except (IndexError, ValueError):
            b_x_min, b_y_min, b_x_max, b_y_max = (0, 0, 0, 0)

        # Bottom layer offset
        Layout.bottom_x_off = t_x_max + b_x_max + Layout.clearance

        # Tracking via placement
        Layout.via_y_max = -max(t_y_max, b_y_max) - Layout.clearance  # Set to top layer lower edge - clearance
        Layout.via_x_min = t_x_min  # Start aligned with the top layer left edge
        Layout.via_row_x_min = t_x_min
        Layout.via_row_x_max = t_x_max + b_x_max - b_x_min + Layout.clearance
        Layout.via_row_height = 0  # Tracks the tallest via in the current row, reset when starting a new row

        # Set the bounds initialized flag
        Layout.bounds_initialized = True

    @staticmethod
    def place(polygon, layer):
        """ Place a polygon into the layout on the given layer.
            Returns the polygon transformed to it's layout coordinates.
        """
        if not Layout.config_initialized or not Layout.bounds_initialized:
            raise AttributeError('Layout has not been initialized.')

        if layer == Layout.top:
            # Reflect over x-axis
            return affine_transform(polygon, [1, 0, 0, -1, 0, 0])

        elif layer == Layout.bottom:
            # Reflect over origin, translate next to the top
            return affine_transform(polygon, [-1, 0, 0, -1, Layout.bottom_x_off, 0])

        else:
            raise ValueError("Layer <{}> not found in the layout.".format(layer))

    @staticmethod
    def place_via(polygon):
        """ Place a via polygon into the layout.
            Returns the polygon transformed to it's layout coordinates.
        """
        if not Layout.config_initialized or not Layout.bounds_initialized:
            raise AttributeError('Layout has not been initialized.')

        x_min, y_min, x_max, y_max = polygon.bounds
        width = x_max - x_min
        height = y_max - y_min

        # Set translation amount
        dx = Layout.via_x_min - x_min
        dy = Layout.via_y_max - y_max

        # Update the the next placement
        Layout.via_x_min += width + Layout.clearance
        Layout.via_row_height = max(Layout.via_row_height, height)

        # Wrap the next placement if it is too far right
        if Layout.via_x_min > Layout.via_row_x_max:
            Layout.via_y_max -= Layout.via_row_height + Layout.clearance
            Layout.via_row_height = 0
            Layout.via_x_min = Layout.via_row_x_min

        return translate(polygon, dx, dy)
