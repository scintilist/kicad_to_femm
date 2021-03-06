#!/usr/bin/python3
# Kyle Smith
# 5-10-2017
#

from ast import literal_eval
import argparse
from textwrap import dedent
import glob
from os.path import split, splitext, abspath

from kicad_to_femm.kicad_pcb import KiCadPcb
from kicad_to_femm.converter import Converter, ConductorSpec
from kicad_to_femm.layout import Layout
from kicad_to_femm import fec


if __name__ == '__main__':
    """ Convert the KiCad board file to an FEC file for FEMM modeling. """

    # Parse arguments
    parser = argparse.ArgumentParser(description='Convert *.kicad_pcd file into an FEC file for FEMM modeling.',
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=dedent('''\
                                     Examples:
                                     >%(prog)s -i in.kicad_pcb -o out.FEC -f 0 -t 70 -c conductors.txt
                                        Open 'in.kicad_pcb' and output 'out.FEC', simulation frequency 0Hz (DC),
                                        copper thickness 70um (2oz), conductor file 'conductors.txt'
                                     '''))
    parser.add_argument('-i', '--in_file', action='store', type=str,
                        help='*.kicad_pcb file path, defaults to the first *.kicad_pcb file found in the current dir')
    parser.add_argument('-o', '--out_file', action='store', type=str,
                        help='*.FEC file path, defaults to \'<infile basename>.FEC\'')
    parser.add_argument('-c', '--conductor_file', action='store', type=str,
                        help=('Conductor specification file path. Conductors are specified as nested tuples/lists.'
                              'See the README for the detailed format specification.'))
    parser.add_argument('-b', '--bounds', nargs=4, type=float,
                        help='bounding box of the format xmin ymin xmax ymax')
    parser.add_argument('-f', '--frequency', action='store', default=0, type=float,
                        help='simulation frequency in Hz, default is 0 (DC)')
    parser.add_argument('-t', '--thickness', action='store', default=35, type=float,
                        help='copper thickness in um, default is 35um (1oz)')
    parser.add_argument('-k', '--board_thickness', action='store', default=1.5, type=float,
                        help='board thickness in mm, default is 1.5mm')
    parser.add_argument('-v', '--via_thickness', action='store', default=17, type=float,
                        help='via copper thickness in um, default is 17um')
    parser.add_argument('-l', '--layers', nargs='*', default=['F.Cu', 'B.Cu'],
                        help='layers to model (supports up to 2), defaults to \'F.Cu\' and \'B.Cu\'')
    parser.add_argument('-s', '--show', action='store_true',
                        help='show the generated polygons')

    args = parser.parse_args()

    # Set the input file if not given
    if not args.in_file:
        try:
            args.in_file = glob.glob('*.kicad_pcb')[0]
        except IndexError:
            raise FileNotFoundError('No kicad_pcb files found in the current directory.')

    # Set the default output file if not given
    if not args.out_file:
        args.out_file = splitext(split(args.in_file)[1])[0] + '.FEC'

    # Limit to 2 layers
    if len(args.layers) > 2:
        print('WARNING: only the first 2 layers given ({} and {}) will be used'.format(*args.layers[:2]))
    args.layers = args.layers[:2]

    # Parse the *.kicad_pcb file
    kicad_pcb = KiCadPcb()
    item = kicad_pcb
    with open(args.in_file, 'r') as f:
        print("Opened input file '{}'.".format(abspath(args.in_file)))

        # Read until the opening paren which creates the root item
        try:
            while f.read(1) != '(':
                pass
        except EOFError:
            raise EOFError('Root item start token \'(\' not found.')

        # Parse until closing paren or EOF
        while True:
            try:
                c = f.read(1)
                item = item.parse(c)
            except EOFError:
                break
            except AttributeError:
                break

    # Parse the conductor specification
    with open(args.conductor_file, 'r') as f:
        conductor_specs = [ConductorSpec(spec) for spec in literal_eval(f.read())]

    # Create the block properties
    copper_conductivity = 5.8e7
    copper_prop = fec.BlockProperty('Copper', copper_conductivity)

    # Since the simulation is done with a single material thickness,
    # scaling via wall thickness is done by scaling copper conductivity.
    via_conductivity = copper_conductivity * args.via_thickness / args.thickness
    via_prop = fec.BlockProperty('Via', via_conductivity)

    # Set up the layout configuration
    Layout.set_config(args.layers, args.board_thickness, copper_prop, via_prop)

    # Convert the kicad_pcb file to fec
    converter = Converter(conductor_specs, args.layers, args.bounds)
    converter.parse_input(kicad_pcb)
    converter.generate_output()

    # Write the output FEC file
    fec.write_out(args.out_file, thickness=args.thickness/1000, frequency=args.frequency)
    print("Wrote output file '{}'.".format(abspath(args.out_file)))

    # Show the generated output polygons
    if args.show:
        converter.show()
