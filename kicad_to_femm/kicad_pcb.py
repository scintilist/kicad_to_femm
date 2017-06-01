"""
KiCad *.kicad_pcb file format spec:
http://bazaar.launchpad.net/~stambaughw/kicad/doc-read-only/download/head:/1115%4016bec504-3128-0410-b3e8-8e38c2123bca:trunk%252Fkicad-doc%252Fdoc%252Fhelp%252Ffile_formats%252Ffile_formats.pdf/file_formats.pdf

S-expression format on p. 26
"""
from enum import IntEnum
from collections import defaultdict


class Item:
    """ A single item from the kicad-pcb file.
        Items contain parameters which may be strings or other nested items.
        Items are subclassed based on the item keyword to add convenient item parameter getters.
    """
    DELIMITERS = [' ', '\t', '\n', '(', ')']

    # Parser states
    class STATE(IntEnum):
        KEYWORD   = 0
        DELIMIT   = 1
        STRING    = 2
        QUOTE     = 3
        END_QUOTE = 4

    def __init__(self, parent=None, keyword='', state=STATE.KEYWORD):
        self.parent = parent         # Parent item, 'None' if it is the root item
        self.keyword = keyword       # Item keyword string
        self.parameters = []         # List of item parameters, can be strings or other items
        self.kw = defaultdict(list)  # Dictionary of keyword parameters, sorted by keyword

        # Parser initial state
        self.state = state

    def keyword_match(self, keyword, value):
        """ Return True if the item has a child item of keyword which has a first parameter equaling 'value'. """
        try:
            return self.kw[keyword][0].parameters[0] == value
        except IndexError:
            return False

    def parse(self, c):
        """ Parse a single character of input, returns the item to pass the next character to,
            which can be self, the parent item, or a child item.
        """
        if self.state == self.STATE.KEYWORD:
            # Reading the keyword
            if c not in self.DELIMITERS:
                self.keyword += c
                return self
            else:
                # Replace itself with a more specific subclass if one exists
                try:
                    if self.parent:
                        self = kw_class_map[self.keyword](self.parent, self.keyword, self.state)
                        self.parent.parameters[-1] = self
                except KeyError:
                    pass

                # Add itself to the parent's keyword dictionary when the keyword parsing is complete
                if self.parent:
                    self.parent.kw[self.keyword].append(self)
                self.state = self.STATE.DELIMIT

        if self.state == self.STATE.STRING:
            if c not in self.DELIMITERS:
                self.parameters[-1] += c
                return self
            else:
                self.state = self.STATE.DELIMIT

        if self.state == self.STATE.QUOTE:
            if c == '"':
                self.state = self.STATE.END_QUOTE
                return self
            else:
                self.parameters[-1] += c
                return self

        if self.state == self.STATE.END_QUOTE:
            if c == '"':
                # Add the escaped quote mark, and return to the quote state
                self.parameters[-1] += '"'
                self.state = self.STATE.QUOTE
                return self
            elif c in self.DELIMITERS:
                self.state = self.STATE.DELIMIT
            else:
                # Ignore any string following a closing quote
                return self

        if self.state == self.STATE.DELIMIT:
            if c not in self.DELIMITERS:
                # Start a new string parameter
                if c == '"':
                    self.parameters.append('')
                    self.state = self.STATE.QUOTE
                    return self
                else:
                    self.parameters.append(c)
                    self.state = self.STATE.STRING
                    return self
            elif c == '(':
                # Start a new item parameter
                new_item = Item(self)
                self.parameters.append(new_item)
                return new_item
            elif c == ')':
                # Finished the item, return to its parent
                self.complete()
                return self.parent
            else:
                # Ignore consecutive delimiters
                return self

    def complete(self):
        """ Called after all parameters have been filled to allow post-processing on sub-classes. """
        pass

    def __str__(self):
        """ Recursive complete string representation. """
        s = '(' + self.keyword + ' '
        s += ' '.join(map(str, self.parameters))
        s += ')'
        return s

    def __repr__(self):
        """ String representation one level deep. """
        s = '(' + self.keyword + ' '
        param_strs = []
        for param in self.parameters:
            try:
                meta_param_strs = []
                for meta_param in param.parameters:
                    try:
                        meta_param_strs.append('(' + meta_param.keyword + ')')
                    except AttributeError:
                        meta_param_strs.append(meta_param)
                param_strs.append('(' + param.keyword + ' ' + ' '.join(meta_param_strs) + ')')
            except AttributeError:
                param_strs.append(param)
        s += ' '.join(param_strs)
        s += ')'
        return s


class XY(Item):
    """ XY data item """
    @property
    def x(self):
        return float(self.parameters[0])

    @property
    def y(self):
        return float(self.parameters[1])


class Size(XY):
    """ XY size data item """
    pass


class Offset(XY):
    """ XY offset data item """
    pass


class At(XY):
    """ X/Y/rotation position data item """
    @property
    def rot(self):
        # Rotation clockwise from vertical in degrees
        try:
            return float(self.parameters[2])
        except IndexError:
            # Return default of 0 if no rotation
            return 0


class RectDelta(XY):
    """ X/Y rectangle delta data item """
    pass


class Drill(Item):
    """ Drill data item """
    @property
    def offset(self):
        try:
            return self.kw['offset'][0]
        except IndexError:
            return None

    @property
    def shape(self):
        if self.parameters[0] == 'oval':
            return 'oval'
        else:
            return 'circle'

    @property
    def size(self):
        if self.shape == 'circle':
            return float(self.parameters[0])
        elif self.shape == 'oval':
            return float(self.parameters[1]), float(self.parameters[2])
        else:
            return None


class Net(Item):
    """ Net data item """
    # Get a net name from a net id
    name_map = {}

    def complete(self):
        # Build the mappings
        if self.parent.keyword == 'kicad_pcb':
            Net.name_map[self.id] = self.name

    @property
    def id(self):
        try:
            return self.parameters[0]
        except IndexError:
            return ''

    @property
    def name(self):
        try:
            return self.parameters[1]
        except IndexError:
            try:
                return Net.name_map[self.id]
            except KeyError:
                return ''


class Segment(Item):
    """ Segment (trace) item """
    @property
    def endpoints(self):
        """ Segment end point coordinates. """
        start = self.kw['start'][0].parameters
        end = self.kw['end'][0].parameters
        return (float(start[0]), float(start[1])), (float(end[0]), float(end[1]))

    @property
    def width(self):
        """ Segment width. """
        return float(self.kw['width'][0].parameters[0])

    @property
    def layer(self):
        return self.kw['layer'][0].parameters[0]

    @property
    def net(self):
        return self.kw['net'][0]


class Zone(Item):
    @property
    def net(self):
        return self.kw['net'][0]

    @property
    def layer(self):
        return self.kw['layer'][0].parameters[0]

    @property
    def min_thickness(self):
        return float(self.kw['min_thickness'][0].parameters[0])

    @property
    def filled_polygons(self):
        return self.kw['filled_polygon']


class FilledPolygon(Item):
    @property
    def points(self):
        """ Polygon points. """
        return [(pt.x, pt.y) for pt in self.kw['pts'][0].parameters]


class KiCadPcb(Item):
    @property
    def modules(self):
        return self.kw['module']

    @property
    def segments(self):
        return self.kw['segment']

    @property
    def vias(self):
        return self.kw['via']

    @property
    def zones(self):
        return self.kw['zone']


class Module(Item):
    @property
    def at(self):
        return self.kw['at'][0]

    @property
    def reference(self):
        for fp_text in self.kw['fp_text']:
            if fp_text.parameters[0] == 'reference':
                return fp_text.parameters[1]
        return ''

    @property
    def pads(self):
        return self.kw['pad']


class Pad(Item):
    @property
    def number(self):
        return self.parameters[0]

    @property
    def type(self):
        return self.parameters[1]

    @property
    def shape(self):
        return self.parameters[2]

    @property
    def at(self):
        return self.kw['at'][0]

    @property
    def size(self):
        return self.kw['size'][0]

    @property
    def rect_delta(self):
        return self.kw['rect_delta'][0]

    @property
    def drill(self):
        return self.kw['drill'][0]

    @property
    def layers(self):
        return self.kw['layers'][0].parameters

    def has_layer(self, match_layer):
        """ Returns true if the pad has the layer. """
        match_layer_name, match_layer_type = match_layer.split('.')

        for layer in self.layers:
            layer_name, layer_type = layer.split('.')
            if layer_type == match_layer_type:
                if layer_name in ('*', match_layer_name):
                    return True
        return False

    @property
    def net(self):
        try:
            return self.kw['net'][0]
        except IndexError:
            return None


class Via(Item):
    @property
    def at(self):
        return self.kw['at'][0]

    @property
    def size(self):
        return self.kw['size'][0]

    @property
    def drill(self):
        return self.kw['drill'][0]

    @property
    def layers(self):
        return self.kw['layers'][0].parameters

    def has_layer(self, match_layer):
        """ Returns true if the pad has the layer. """
        match_layer_name, match_layer_type = match_layer.split('.')

        for layer in self.layers:
            layer_name, layer_type = layer.split('.')
            if layer_type == match_layer_type:
                if layer_name in ('*', match_layer_name):
                    return True
        return False

    @property
    def net(self):
        try:
            return self.kw['net'][0]
        except IndexError:
            return None


""" Map of keywords to their classes. """
kw_class_map = {
    'xy': XY,
    'size': Size,
    'at': At,
    'rect_delta': RectDelta,
    'drill': Drill,
    'offset': Offset,
    'net': Net,
    'segment': Segment,
    'zone': Zone,
    'filled_polygon': FilledPolygon,
    'kicad_pcb': KiCadPcb,
    'module': Module,
    'pad': Pad,
    'via': Via
}
