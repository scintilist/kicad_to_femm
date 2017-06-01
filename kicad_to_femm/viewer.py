"""
Simple OpenGL 2d viewer for groups of shapely polygons
"""
from itertools import chain

import pyglet
from pyglet.window import key
from pyglet.gl import *

from OpenGL.GLU import *
from OpenGL.GL import *

from shapely.geometry import MultiPolygon, box


def triangulate(polygon):
    """ Return the triangulation of a shapely polygon as a list of triangle points.
        Uses OpenGL GLU Tesselator
        See: http://stackoverflow.com/a/38810954
    """
    holes = [interior.coords[:-1] for interior in polygon.interiors]
    polygon = polygon.exterior.coords[:-1]

    vertices = []

    def edge_flag_callback(param1, param2):
        pass

    def begin_callback(param=None):
        vertices = []

    def vertex_callback(vertex, otherData=None):
        vertices.append(vertex[:2])

    def combine_callback(vertex, neighbors, neighborWeights, out=None):
        out = vertex
        return out

    def end_callback(data=None):
        pass

    tess = gluNewTess()
    gluTessProperty(tess, GLU_TESS_WINDING_RULE, GLU_TESS_WINDING_ODD)
    gluTessCallback(tess, GLU_TESS_EDGE_FLAG_DATA, edge_flag_callback)
    gluTessCallback(tess, GLU_TESS_BEGIN, begin_callback)
    gluTessCallback(tess, GLU_TESS_VERTEX, vertex_callback)
    gluTessCallback(tess, GLU_TESS_COMBINE, combine_callback)
    gluTessCallback(tess, GLU_TESS_END, end_callback)
    gluTessBeginPolygon(tess, 0)

    # First handle the main polygon
    gluTessBeginContour(tess)
    for point in polygon:
        point3d = (point[0], point[1], 0)
        gluTessVertex(tess, point3d, point3d)
    gluTessEndContour(tess)

    # Then handle each of the holes, if applicable
    for hole in holes:
        gluTessBeginContour(tess)
        for point in hole:
            point3d = (point[0], point[1], 0)
            gluTessVertex(tess, point3d, point3d)
        gluTessEndContour(tess)

    gluTessEndPolygon(tess)
    gluDeleteTess(tess)
    return vertices


class Group:
    def __init__(self, tri_vl, line_vl, point_color=(0, 0, 0, 1), line_color=(0, 0, 0, 1), fill_color=(1, 1, 1, 1)):
        self.tri_vl = tri_vl
        self.line_vl = line_vl
        self.point_color = point_color
        self.line_color = line_color
        self.fill_color = fill_color


class Mouse:
    def __init__(self, x, y):
        self.x = x
        self.y = y


class Window(pyglet.window.Window):
    def __init__(self, mirror_y=False):
        config = Config(double_buffer=True, depth_size=0, sample_buffers=1, samples=8)
        platform = pyglet.window.get_platform()
        display = platform.get_default_display()
        screen = display.get_default_screen()
        super().__init__(round(screen.width * 0.75), round(screen.height * 0.75), config=config, resizable=True)
        glClearColor(0.8, 0.8, 0.8, 1.0)
        glEnable(GL_BLEND)  # Enable transparency / alpha blending
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)

        self.mirror_y = mirror_y
        self.polygon_groups = []
        self.bounds = box(0, 0, 1, 1)

        # Viewport
        self.v_size = 1
        self.h_origin = 0
        self.v_origin = 0

        # Update flags
        self.resize_flag = False
        self.draw_flag = False

        # Mouse position label
        self.mouse_label = pyglet.text.Label('', font_size=18, x=10, y=10,
                                             anchor_x='left', anchor_y='baseline',
                                             color=(0, 0, 0, 160))
        # Current mouse position
        self.mouse = Mouse(self.width/2, self.height/2)

        # Help label
        help_doc = pyglet.text.decode_text(
            'Keyboard commands:\n'
            '\tESC, Q \t- Close viewer.\n'
            '\tHOME  \t- Center and set default zoom.\n'
            '\tF         \t- Toggle polygon fill.\n'
            '\tL         \t- Toggle polygon lines.\n'
            '\tP         \t- Toggle polygon points.\n'
            '\tH, ?, F1\t- Toggle help overlay.\n'
            '\n'
            'Zoom with mouse scroll or + and - keys.\n'
            'Pan with middle mouse drag or arrow keys.'
        )
        help_doc.set_style(0, len(help_doc.text), dict(font_size=14, color=(0, 0, 0, 160)))

        self.help_label = pyglet.text.layout.TextLayout(help_doc, width=0, height=0, multiline=True, wrap_lines=False)

        # Display flags
        self.points = False
        self.lines = True
        self.fill = True
        self.help = True

    def add_polygon_group(self, polygons, point_color=(0, 0, 0, 1), line_color=(0, 0, 0, 1), fill_color=(1, 1, 1, 1)):
        """ Add a group of polygons with color settings. """
        # Update bounds
        try:
            if not self.polygon_groups:
                self.bounds = box(*MultiPolygon(polygons).bounds)
            else:
                self.bounds = box(*MultiPolygon((*polygons, self.bounds)).bounds)
        except TypeError:
            # Input polygons can't generate a bounding box, add failed.
            return

        # Convert polygons to vertex lists
        tri_vl = []
        line_vl = []
        for polygon in polygons:
            triangle_vertices = triangulate(polygon)
            tri_vl.append(list(chain.from_iterable(triangle_vertices)))

            line_vl.append(list(chain.from_iterable(polygon.exterior.coords[:-1])))
            for interior in polygon.interiors:
                line_vl.append(list(chain.from_iterable(interior.coords[:-1])))

        # Add the vertex lists and colors to the groups list
        self.polygon_groups.append(Group(tri_vl, line_vl, point_color, line_color, fill_color))

    def show(self):
        self.home()
        pyglet.clock.schedule_interval(self.update, 1 / 60)
        pyglet.app.run()

    def home(self):
        # Fractional margin between the polygons and the window edges.
        # For example, 0.05 means the polygon will fill the middle 90% of the window
        margin = 0.05

        x_min, y_min, x_max, y_max = self.bounds.bounds

        poly_width = x_max - x_min
        poly_height = y_max - y_min

        # Viewport vertical size (horizontal will follow based on window aspect ratio)
        scale = min(self.height / poly_height, self.width / poly_width)
        self.v_size = (1 + margin * 2) * self.height / scale

        # Viewport origin
        self.h_origin = ((x_min + x_max) - ((1 + margin * 2) * self.width / scale)) / 2
        self.v_origin = ((y_min + y_max) - ((1 + margin * 2) * self.height / scale)) / 2

        self.resize_flag = True

    def update(self, realtime_dt):
        if self.resize_flag:
            self.resize_flag = False
            self.on_resize(self.width, self.height)
        if self.draw_flag:
            self.draw_flag = False
            self.on_draw()

    def on_draw(self):
        self.clear()

        # Adjust viewport for the polygon
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        h_size = self.v_size * self.width / self.height
        if self.mirror_y:
            gluOrtho2D(self.h_origin, self.h_origin + h_size,
                       self.v_origin + self.v_size, self.v_origin)
        else:
            gluOrtho2D(self.h_origin, self.h_origin + h_size,
                       self.v_origin, self.v_origin + self.v_size)
        glMatrixMode(GL_MODELVIEW)

        # Draw polygons
        for group in self.polygon_groups:
            if self.fill:
                glColor4f(*group.fill_color)
                for tri_vl in group.tri_vl:
                    pyglet.graphics.draw(len(tri_vl)//2, GL_TRIANGLES, ('v2f', tri_vl))

            if self.lines:
                glColor4f(*group.line_color)
                for vl in group.line_vl:
                        glLineWidth(2)
                        pyglet.graphics.draw(len(vl)//2, GL_LINE_LOOP, ('v2f', vl))

            if self.points:
                glColor4f(*group.point_color)
                for vl in group.line_vl:
                    glPointSize(8)
                    pyglet.graphics.draw(len(vl)//2, GL_POINTS, ('v2f', vl))

        # Adjust viewport for the text labels
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        glViewport(0, 0, self.width, self.height)
        gluOrtho2D(0, self.width, 0, self.height)
        glMatrixMode(GL_MODELVIEW)

        # Draw text labels
        x_coord = self.h_origin + self.mouse.x / self.height * self.v_size
        if self.mirror_y:
            y_coord = self.v_origin + (1 - self.mouse.y / self.height) * self.v_size
        else:
            y_coord = self.v_origin + self.mouse.y / self.height * self.v_size
        self.mouse_label.text = 'X {:.3f}  Y {:.3f}'.format(x_coord, y_coord)
        self.mouse_label.draw()

        if self.help:
            self.help_label.width = self.width
            self.help_label.height = self.height
            self.help_label.x = 10
            self.help_label.y = -10
            self.help_label.draw()

    def on_resize(self, width, height):
        self.draw_flag = True
        return pyglet.event.EVENT_HANDLED

    def zoom(self, levels):
        """ Zoom in or out by levels. """
        # Determine what is under the mouse
        mx = self.h_origin + self.mouse.x / self.height * self.v_size
        if self.mirror_y:
            my = self.v_origin + (1 - self.mouse.y / self.height) * self.v_size
        else:
            my = self.v_origin + self.mouse.y / self.height * self.v_size
        # Scale the vertical size
        self.v_size *= 1.25 ** -levels

        # Adjust the origin to return what was under the mouse
        self.h_origin = mx - self.mouse.x / self.height * self.v_size
        if self.mirror_y:
            self.v_origin = my - (1 - self.mouse.y / self.height) * self.v_size
        else:
            self.v_origin = my - self.mouse.y / self.height * self.v_size

        self.resize_flag = True

    def on_mouse_scroll(self, x, y, scroll_x, scroll_y):
        self.zoom(scroll_y)

    def on_mouse_motion(self, x, y, dx, dy):
        self.mouse.x = x
        self.mouse.y = y

    def on_mouse_drag(self, x, y, dx, dy, buttons, modifiers):
        self.mouse.x = x
        self.mouse.y = y
        if buttons & pyglet.window.mouse.MIDDLE:
            self.h_origin -= dx * self.v_size / self.height
            if self.mirror_y:
                dy = -dy
            self.v_origin -= dy * self.v_size / self.height

    def on_key_release(self, symbol, modifiers):
        KeyRepeat(symbol).stop()

    def on_key_press(self, symbol, modifiers):
        if symbol == key.P:
            self.points = not self.points
        elif symbol == key.L:
            self.lines = not self.lines
        elif symbol == key.F:
            self.fill = not self.fill
        elif symbol == key.HOME:
            self.home()
        elif symbol in (key.ESCAPE, key.Q):
            pyglet.app.exit()
        elif symbol in (key.F1, key.H, key.QUESTION, key.SLASH):
            self.help = not self.help
        elif symbol in (key.PLUS, key.EQUAL):
            KeyRepeat(key.PLUS, key.EQUAL).start(self.zoom, levels=1)
        elif symbol in (key.MINUS, key.UNDERSCORE):
            KeyRepeat(key.MINUS, key.UNDERSCORE).start(self.zoom, levels=-1)

        return pyglet.event.EVENT_HANDLED

    def on_text_motion(self, motion):
        if motion == pyglet.window.key.MOTION_UP:
            dy = 100 * self.v_size / self.height
            if self.mirror_y:
                dy = -dy
            self.v_origin += dy
        elif motion == pyglet.window.key.MOTION_DOWN:
            dy = 100 * self.v_size / self.height
            if self.mirror_y:
                dy = -dy
            self.v_origin -= dy
        elif motion == pyglet.window.key.MOTION_LEFT:
            self.h_origin -= 100 * self.v_size / self.height
        elif motion == pyglet.window.key.MOTION_RIGHT:
            self.h_origin += 100 * self.v_size / self.height


class KeyRepeat:
    repeaters = set()

    def __new__(cls, *symbols):
        # Returns the existing instance, or a new instance if one doesn't exist
        symbols = set(symbols)
        for repeater in KeyRepeat.repeaters:
            if repeater.symbols & symbols:
                repeater.symbols |= symbols
                return repeater
        new_repeater = object.__new__(cls)
        new_repeater.__init__(*symbols)
        KeyRepeat.repeaters.add(new_repeater)
        return new_repeater

    def __init__(self, *symbols):
        self.symbols = set(symbols)
        self.func = None
        self.args = None
        self.kwargs = None

    def stop(self):
        pyglet.clock.unschedule(self.run)
        KeyRepeat.repeaters.discard(self)

    def start(self, func, *args, **kwargs):
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.func(*self.args, **self.kwargs)
        pyglet.clock.schedule_once(self.run, 0.4)  # Start delay

    def run(self, dt):
        self.func(*self.args, **self.kwargs)
        pyglet.clock.schedule_once(self.run, 0.1)  # Repeat delay