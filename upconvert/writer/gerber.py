#!/usr/bin/env python2
""" The Gerber RS274-X Format Writer """

# upconvert - A universal hardware design file format converter using
# Format:       upverter.com/resources/open-json-format/
# Development:  github.com/upverter/schematic-file-converter
#
# Copyright 2011 Upverter, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from sys import stdout
from os import path, mkdir, listdir, makedirs, stat
from shutil import rmtree
from collections import namedtuple
from tarfile import TarFile
from zipfile import ZipFile
import copy
import errno
import freetype
import logging

from upconvert.core.component_instance import FootprintPos
from upconvert.core.layout import Aperture, Image, Macro, MacroAperture, Smear, Fill
from upconvert.core.shape import Circle, Rectangle, Obround, RegularPolygon
from upconvert.core.shape import Polygon, Moire, Thermal
from upconvert.core.shape import Point, Arc, Line


log = logging.getLogger('writer.gerber')


# exceptions

class Unwritable(ValueError):
    """ Parent class for all gerber writer errors. """
    pass

class MissingLayout(Unwritable):
    """ Design has no layout information. """
    pass

class NoLayersFound(Unwritable):
    """ Layout doesn't have any layers. """
    pass

class UnitsNotSpecified(Unwritable):
    """ Layout doesn't specify units. """
    pass

class ImageContainsNoData(Unwritable):
    """ One of the layers contains an empty image. """
    pass

class NotBatch(ValueError):
    """ The intended output is a single file. """
    pass


# constants
POS_UNITS_PER_MM = 1000 * 1000
POS_UNITS_PER_MIL = 25400
POS_UNITS_PER_CM = POS_UNITS_PER_MM * 10
POS_UNITS_PER_IN = POS_UNITS_PER_MIL * 1000

LINE = '{0}\r\n'
FORMAT_SPEC = '%FSLAX{int}{dec}Y{int}{dec}*%'
MACRO = LINE.format('%AM{name}*') + '{primitives}%'
PRIMITIVE = '{mods}*'
APERTURE = '%ADD{code}{type}{mods}*%'
AP_MODS = ',{mods}'
PARAM = '%{name}{val}*%'
SR_MODS = 'X{x}Y{y}I{i}J{j}'
FUNCT = '{type}{code}*'
COORD = 'X{x}Y{y}*'
COORD_EXT = 'X{x}Y{y}I{i}J{j}*'
PLOT = 'X{x}Y{y}D01*'
MOVE = 'X{x}Y{y}D02*'
FLASH = 'X{x}Y{y}D03*'
LAYER_NAME = '%LN{layer_name}*%'
LAYER_POLARITY_CLEAR = '%LPC*%'
LAYER_POLARITY_DARK = '%LPD*%'
MM_UNITS = '%MOMM*%'
IN_UNITS = '%MOIN*%'
EOF = 'M02*'
INTERPOLATION = {'LINEAR': '01',
                 'CLOCKWISE_CIRCULAR': '02',
                 'ANTICLOCKWISE_CIRCULAR': '03'}
SHAPE_TAGS = {'circle': {'int': 1, 'char': 'C'},
              'center_rectangle': {'int': 21, 'char': 'R'},
              'rectangle': {'int': 22, 'char': 'R'},
              'obround': {'char': 'O'},
              'reg_polygon': {'int': 5, 'char': 'P'},
              'polygon': {'int': 4},
              'moire': {'int': 6},
              'thermal': {'int': 7}}
LAYERS_CFG = 'layers.cfg'
TAR_MODES = {'.tar': 'w',
             '.gz': 'w:gz',
             '.tgz': 'w:gz',
             '.bz2': 'w:bz2'}


# archive class

Batch = namedtuple('Batch', 'archive add_ rootdir')             # pylint: disable=C0103


# writer

class Gerber:
    """ The Gerber Format Writer. """

    def __init__(self):
        self.coord_format = None
        self.face = freetype.Face('./arial.ttf')
        self._reset()

    def _reset(self):
        self.layers = list()
        self.images = list()
        self.apertures = list()
        self.macros = list()
        self.status = {'x':0,
                       'y':0,
                       'interpolation':None,
                       'aperture':None,
                       'outline_fill':False,
                       'multi_quadrant':False,
                       'units':None,
                       'incremental_coords':None}


    def write(self, design, outfile=None):
        """ Main logic for producing a set of output files. """
        log.debug('starting gerber write to %s', outfile)
        self._check_design(design)
        if outfile:
            dir_ = path.dirname(outfile)
            if dir_:
                try:
                    stat(dir_)
                except OSError:
                    makedirs(dir_)
        batch = self._get_archive(outfile)

        if batch:
            cfg_name = self._config_batch(batch, outfile)
            with open(cfg_name, 'w') as cfg:
                for layer in design.layer_options:
                    member_name = layer.name.lower().replace(' ', '_') + '.ger'
                    with open(batch.rootdir and
                              path.join(batch.rootdir, member_name) or
                              path.join(dir_, member_name), 'w') as member:
                        self._write_layer(design, layer, member)
                    cfg.write(LINE.format(', '.join([layer.name, member_name])))
            if batch.archive:
                #TODO: line below doesn't work
                #batch.add_(batch.rootdir)
                for member_name in listdir(batch.rootdir):
                    batch.add_(path.join(batch.rootdir, member_name))
                batch.archive.close()
                rmtree(batch.rootdir)

        else:
            #TODO: check that there's actually only one layer
            with outfile and open(outfile, 'w') or stdout as f:
                self._write_layer(design, design.layer_options[0], f)


    # primary writer support methods
    def _get_archive(self, outfile):
        """ Establish zip or tarfile for batch output. """
        batch = None
        if outfile:
            filename = path.basename(outfile)
            rootdir, ext = path.splitext(filename)
            if ext in TAR_MODES.keys() + ['.zip']:
                if ext == '.zip':
                    archive = ZipFile(outfile, 'w')
                    add_ = archive.write
                else:
                    archive = TarFile.open(outfile, TAR_MODES[ext])
                    add_ = archive.add
                rootdir = filename.split('.')[0]
                batch = Batch(archive, add_, rootdir)
            elif LAYERS_CFG in filename:
                batch = Batch(False, False, False)
        return batch


    def _config_batch(self, batch, outfile):
        """ Establish config file for batch output. """
        if not batch:
            raise NotBatch
        else:
            if batch.rootdir:
                try:
                    mkdir(batch.rootdir)
                except OSError as exc: # Python >2.5
                    if exc.errno == errno.EEXIST:
                        pass
                    else: raise
                cfg_name = path.join(batch.rootdir, LAYERS_CFG)
            else:
                cfg_name = outfile
        return cfg_name



    def _write_layer(self, design, layer_options, layer_file):
        """ Write the information for `layer_name` from the design to a single gerber file. """

        # Initialize the writer state for the layer
        self._reset()

        # decompose layer data into images and the apertures and macros used to represent the images,
        self._define_images(design, layer_options.name)

        self._define_macros()
        self._define_apertures()

        # write layer parameters
        layer_file.write(LINE.format(self._get_format_spec(design.layout_units)))
        if design.layout_units == 'mm':
            layer_file.write(LINE.format(MM_UNITS))
        elif design.layout_units == 'in':
            layer_file.write(LINE.format(IN_UNITS))
        else:
            raise UnitsNotSpecified('acceptable units not specified')

        # include macros and apertures used on the layer
        for macro in self.macros:
            layer_file.write(self._get_macro_def(macro))
        for aperture in self.apertures:
            if isinstance(aperture, MacroAperture):
                layer_file.write(self._get_macro_ap_def(aperture))
            elif isinstance(aperture, Aperture):
                layer_file.write(self._get_shape_ap_def(aperture))
            else:
                raise TypeError('unexpected aperture class')

        # FIXME(shamer): What is this check doing?
        # select an arbitrary aperture for trace[0] check
        if self.apertures:
            self.status['aperture'] = self.apertures[0]
            layer_file.write(LINE.format(FUNCT.format(type='D', code=self.apertures[0].code)))

        for i in range(len(self.layers)):
            for param in self._get_image_meta(i, self.layers):
                layer_file.write(param)
            for block in self._gen_paths(self.layers[i]):
                layer_file.write(block)

        # build non-layer images - main data section
        for i in range(len(self.images)):
            for param in self._get_image_meta(i, self.images):
                layer_file.write(param)
            for block in self._gen_paths(self.images[i]):
                layer_file.write(block)

        # tidy up
        layer_file.write(EOF)


    def  _define_images(self, design, layer_name):
        """ Define the images that make up the layer information. """
        log.debug('creating images for layer "%s"', layer_name)

        # trace segments on this layer
        traces_image = Image(layer_name + '_traces', font_renderer=self.face)
        for segment in design.trace_segments:
            if segment.layer != layer_name:
                continue
            log.debug('Creating smear for trace: %s', segment)

            # Assumes segment is rounded, straignt
            trace_smear = Smear(Line(segment.p1, segment.p2), Circle(0, 0, segment.width / 2.0))
            traces_image.smears.append(trace_smear)

        # Generated objects in the design (vias, PTHs)
        zero_pos = FootprintPos(0, 0, 0.0, False, 'top')
        for gen_obj in design.layout_objects:
            # XXX(shamer): body attr is only being used to hold the layer, other placement details are contained
            # elsewhere
            for body_attr, body in gen_obj.bodies(zero_pos, {}):
                if body_attr.layer == layer_name:
                    for shape in body.shapes:
                        traces_image.add_shape(shape, design, zero_pos, body_attr)

        self.images.append(traces_image)


        # Pours on this layer
        for pour in design.pours:
            log.debug('adding body for pour: %s points, %s', len(pour.points), pour.layer)
            if layer_name == pour.layer:
                log.debug('adding body for pour: %s points, %s subtractive shapes', len(pour.points), len(pour.subtractive_shapes))
                fill_image = Image('pour fill', font_renderer=self.face)
                fill_image.fills.append(Fill(pour.points))
                self.images.append(fill_image)

                subtractive_image = Image('pour subtractive shapes', font_renderer=self.face, is_additive=False)
                for shape in pour.subtractive_shapes:
                    if shape.type == 'rounded_segment':
                        trace_smear = Smear(Line(shape.p1, shape.p2), Circle(0, 0, shape.width / 2.0))
                        subtractive_image.smears.append(trace_smear)
                    else:
                        subtractive_image.add_shape(shape, None, FootprintPos(0, 0, 0.0, False, ''), FootprintPos(0, 0, 0.0, False, ''))
                self.images.append(subtractive_image)

                readded_image = Image('pour readded shapes', font_renderer=self.face, is_additive=True)
                for shape in pour.readded_shapes:
                    if shape.type == 'rounded_segment':
                        trace_smear = Smear(Line(shape.p1, shape.p2), Circle(0, 0, shape.width / 2.0))
                        readded_image.smears.append(trace_smear)
                    else:
                        readded_image.add_shape(shape, None, FootprintPos(0, 0, 0.0, False, ''), FootprintPos(0, 0, 0.0, False, ''))
                self.images.append(readded_image)

        # trace segments on this layer
        traces_image = Image(layer_name + '_traces', font_renderer=self.face)
        for segment in design.trace_segments:
            if segment.layer != layer_name:
                continue
            log.debug('Creating smear for trace: %s', segment)

            # Assumes segment is rounded, straignt
            trace_smear = Smear(Line(segment.p1, segment.p2), Circle(0, 0, segment.width / 2.0))
            traces_image.smears.append(trace_smear)

        # Generated objects in the design (vias, PTHs)
        zero_pos = FootprintPos(0, 0, 0.0, False, 'top')
        for gen_obj in design.layout_objects:
            # XXX(shamer): body attr is only being used to hold the layer, other placement details are contained
            # elsewhere
            for body_attr, body in gen_obj.bodies(zero_pos, {}):
                if body_attr.layer == layer_name:
                    for shape in body.shapes:
                        traces_image.add_shape(shape, design, zero_pos, body_attr)

        self.images.append(traces_image)


        # Component aspects on this layer
        # a separate image is used for each component
        for component_instance in design.component_instances:
            component = design.components.components[component_instance.library_id]
            component_image = Image(layer_name + ' component ' + component_instance.instance_id, font_renderer=self.face)
            footprint_pos = component_instance.footprint_pos
            if footprint_pos is None:
                continue
            if footprint_pos.side is None:
                continue

            for idx, footprint_attr in enumerate(component_instance.footprint_attributes):
                log.debug('footprint pos: %s, side %s, flip %s', footprint_attr.layer, footprint_pos.side, footprint_pos.flip_horizontal)
                fp_attr_cpy = copy.deepcopy(footprint_attr)
                if footprint_attr.layer:
                    if footprint_pos.side == 'bottom':
                        # XXX(shamer): don't flip, just take the footprint side. This is a temporary fix to address the
                        # tool not saving the layer correctly for moves on the bottom.
                        #rev_sides = {'top': 'bottom', 'bottom': 'top'}
                        rev_sides = {'top': footprint_pos.side, 'bottom': footprint_pos.side}
                        fp_attr_cpy.layer = ' '.join([rev_sides.get(piece, piece) for piece in footprint_attr.layer.split(' ')])
                if fp_attr_cpy.layer == layer_name:
                    footprint_body = component.footprints[component_instance.footprint_index].bodies[idx]
                    log.debug('adding footprint attribute: %s, %d shapes', fp_attr_cpy, len(footprint_body.shapes))
                    body_pos = copy.deepcopy(footprint_pos)
                    if footprint_pos.flip_horizontal != footprint_body.flip_horizontal:
                        body_pos.rotation = (footprint_pos.rotation - footprint_body.rotation) % 2
                    else:
                        body_pos.rotation = (footprint_pos.rotation + footprint_body.rotation) % 2
                    body_pos.flip_horizontal = (footprint_pos.flip_horizontal != footprint_body.flip_horizontal)

                    for shape in footprint_body.shapes:
                        component_image.add_shape(shape, component_instance, body_pos, fp_attr_cpy)

            for idx, gen_obj_attr in enumerate(component_instance.gen_obj_attributes):
                gen_obj = component.footprints[component_instance.footprint_index].gen_objs[idx]
                # FIXME(shamer): check for unplaced generated objects.

                # XXX(shamer): body attr is only being used to hold the layer, other placement details are contained
                # elsewhere
                for body_attr, body in gen_obj.bodies(footprint_pos, gen_obj_attr.attributes):
                    if body_attr.layer == layer_name:
                        log.debug('adding body for generated object: %s, %s', footprint_pos, gen_obj_attr)
                        for shape in body.shapes:
                            component_image.add_shape(shape, component_instance, footprint_pos, body_attr)

            if component_image.not_empty():
                self.images.append(component_image)

        # paths on the layer
        for path in design.paths:
            if layer_name == path.layer:
                log.debug('adding body for path: %s points, %s, %s, is closed: %s', len(path.points), path.width, path.layer, path.is_closed)
                path_image = Image('path', font_renderer=self.face)
                start = path.points[0]
                for point in path.points[1:]:
                    path_image.add_shape(Line(start, point), Circle(0, 0, path.width), zero_pos, zero_pos)
                    start = point
                if path.is_closed:
                    path_image.add_shape(Line(path.points[0], path.points[-1]), Circle(0, 0, path.width), zero_pos, zero_pos)
                self.images.append(path_image)

        # stand alone text on the layer
        text_image = Image('text', font_renderer=self.face)
        for text in design.pcb_text:
            if layer_name == text.layer:
                log.debug('adding body for text: "%s"', text.value)
                text_image.add_shape(text.label, design, text, zero_pos)

        if text_image.not_empty():
            self.images.append(text_image)


    def _define_macros(self):
        """ Define the macros used in the design. """
        for image in self.images:
            self._define_image_macros(image)
        for layer in self.layers:
            self._define_image_macros(layer)


    def _define_image_macros(self, image):
        """ Build the macros needed to make the image. """
        for complex_instance in image.complex_instances:
            self._add_macro(complex_instance)


    def _add_macro(self, complex_instance):
        """ Add a macro definition for a complex collection of shapes. """
        macro = Macro(complex_instance.name, complex_instance.primitives)
        if macro not in self.macros:
            self.macros.append(macro)


    def _define_apertures(self):
        """ Define the apertures used in the design. """
        for image in self.images:
            self._define_image_apertures(image)
        for layer in self.layers:
            self._define_image_apertures(layer)


    def _define_image_apertures(self, image):
        """ Build the apertures needed to make shapes. """
        for smear in image.smears:
            self._add_shape_aperture(smear.shape, None)
        for shape_instance in image.shape_instances:
            self._add_shape_aperture(shape_instance.shape,
                               shape_instance.hole)
        for complex_instance in image.complex_instances:
            self._add_macro_aperture(complex_instance)


    def _get_format_spec(self, units, max_size=False, max_precision=False):
        """ Generate FS parameter with sensible precision. """
        if units == 'inch':
            int_, dec_ = (2, 4)
        elif units == 'mm':
            int_, dec_ = (3, 3)
        else:
            raise ValueError('unknown unit format: ' + units)
        if max_size:
            int_ = 6
        if max_precision:
            dec_ = 6
        self.coord_format = {'int': int_, 'dec': dec_}
        return FORMAT_SPEC.format(int=int_, dec=dec_)


    def _get_macro_def(self, macro):
        """ Convert macro based on core shapes to gerber. """
        prims_def = ''
        for primitive in macro.primitives:
            shape = primitive.shape
            exposure = primitive.is_additive
            rotation = shape.rotation #or primitive.rotation
            rotation = int((2 - rotation) * 180 or 0)

            if isinstance(shape, Circle):
                mods = [SHAPE_TAGS['circle']['int'],
                        exposure,
                        self._convert_units_str(shape.radius * 2),
                        self._convert_units_str(shape.x),
                        self._convert_units_str(shape.y)]
            elif isinstance(shape, Rectangle) and shape.is_centered:
                mods = [SHAPE_TAGS['center_rectangle']['int'],
                        exposure,
                        self._convert_units_str(shape.width),
                        self._convert_units_str(shape.height),
                        self._convert_units_str(shape.x),
                        self._convert_units_str(shape.y),
                        rotation if not shape.flip_horizontal else -rotation]
            elif isinstance(shape, Rectangle) and not shape.is_centered:
                mods = [SHAPE_TAGS['rectangle']['int'],
                        exposure,
                        self._convert_units_str(shape.width),
                        self._convert_units_str(shape.height),
                        self._convert_units_str(shape.x),
                        self._convert_units_str(shape.y),
                        rotation]
            elif isinstance(shape, Polygon):
                vertices = [(self._convert_units_str(p.x), self._convert_units_str(p.y)) for p in shape.points]
                v_args = [vertices[i / 2][i % 2]
                          for i in range(len(vertices) * 2)]
                mods = [SHAPE_TAGS['polygon']['int'],
                        exposure] + v_args + [rotation]
            elif isinstance(shape, RegularPolygon):
                vertices = [(self._convert_units_str(p.x), self._convert_units_str(p.y)) for p in shape.vertices]
                mods = [SHAPE_TAGS['reg_polygon']['int'],
                        exposure,
                        vertices,
                        self._convert_units_str(shape.x),
                        self._convert_units_str(shape.y),
                        self._convert_units_str(shape.outer_diameter),
                        rotation]
            elif isinstance(shape, Moire):
                mods = [SHAPE_TAGS['moire']['int'],
                        self._convert_units_str(shape.x),
                        self._convert_units_str(shape.y),
                        self._convert_units_str(shape.outer_diameter),
                        self._convert_units_str(shape.ring_thickness),
                        self._convert_units_str(shape.gap_thickness),
                        self._convert_units_str(shape.max_rings),
                        self._convert_units_str(shape.hair_thickness),
                        self._convert_units_str(shape.hair_length),
                        rotation]
            elif isinstance(shape, Thermal):
                mods = [SHAPE_TAGS['thermal']['int'],
                        self._convert_units_str(shape.x),
                        self._convert_units_str(shape.y),
                        self._convert_units_str(shape.outer_diameter),
                        self._convert_units_str(shape.inner_diameter),
                        self._convert_units_str(shape.gap_thickness),
                        rotation]
            mods = ','.join(str(m) for m in mods)
            prim_def = PRIMITIVE.format(mods=mods)
            prims_def += LINE.format(prim_def)
        macro_def = MACRO.format(name=macro.name,
                                 primitives=prims_def.strip())
        return LINE.format(macro_def)


    def _get_shape_ap_def(self, aperture):
        """ Convert aperture based on core shapes to gerber. """

        # get type and shape mods
        shape = aperture.shape
        if isinstance(shape, Circle):
            type_ = SHAPE_TAGS['circle']['char']
            mods = [self._convert_units_str(shape.radius * 2)]
        elif isinstance(shape, Rectangle):
            type_ = SHAPE_TAGS['rectangle']['char']
            mods = [self._convert_units_str(shape.width),
                    self._convert_units_str(shape.height)]
        elif isinstance(shape, Obround):
            type_ = SHAPE_TAGS['obround']['char']
            mods = [self._convert_units_str(shape.width),
                    self._convert_units_str(shape.height)]
        elif isinstance(shape, RegularPolygon):
            rot = shape.rotation
            rotation = int(rot and (2 - rot) * 180 or 0)
            vertices = [(self._convert_units_str(p.x), self._convert_units_str(p.y)) for p in shape.vertices]
            type_ = SHAPE_TAGS['reg_polygon']['char']
            mods = [self._convert_units_str(shape.outer_diameter),
                    vertices,
                    rotation]
        elif isinstance(shape, str):
            type_ = shape
            mods = []

        # add hole mods
        hole = aperture.hole
        if isinstance(hole, Circle):
            hole_mods = [self._convert_units_str(hole.radius)]
        elif hole:
            hole_mods = [self._convert_units_str(hole.width), self._convert_units_str(hole.height)]
        else:
            hole_mods = []
        mods += hole_mods

        # generate param
        mods = 'X'.join(str(m) for m in mods)
        mods_def = (mods and AP_MODS.format(mods=mods) or '')
        ap_def = APERTURE.format(code=aperture.code,
                                 type=type_,
                                 mods=mods_def)
        return LINE.format(ap_def)


    def _get_macro_ap_def(self, macro_aperture):
        """ Convert aperture based on core shapes to gerber. """
        ap_def = APERTURE.format(code=macro_aperture.code,
                                 type=macro_aperture.name,
                                 mods='')
        return LINE.format(ap_def)


    def _get_image_meta(self, image_idx, images):
        """ Generate layer params for the image layer. """
        image = images[image_idx]
        prev_additive = True
        if image_idx > 0:
            prev_additive = images[image_idx - 1].is_additive

        if image.name:
            meta = PARAM.format(name='LN',
                                val=image.name)
            yield LINE.format(meta)

        # Image.is_additive can only be set explicitly,
        # so we check the previous image layer.
        if image.is_additive:
            if not prev_additive:
                meta = PARAM.format(name='LP',
                                    val='D')
                yield LINE.format(meta)
        else:
            if prev_additive:
                meta = PARAM.format(name='LP',
                                    val='C')
                yield LINE.format(meta)

        # handle repetition of image layer
        if image.x_repeats > 1 or image.y_repeats > 1:
            val = SR_MODS.format(x=image.x_repeats,
                                 y=image.y_repeats,
                                 i=image.x_step,
                                 j=image.y_step)
            meta = PARAM.format(name='SR',
                                val=val)
            yield LINE.format(meta)


    def _gen_paths(self, image):
        """ Generate functions and coordinates. """
        for smear in image.smears:
            for block in self._gen_smear(smear):
                yield block
        for shin in image.shape_instances:
            for block in self._gen_shape_instance(shin):
                yield block
        for complex_instance in image.complex_instances:
            for block in self._gen_complex_instance(complex_instance):
                yield block
        if image.fills:
            for block in self._gen_fills(image.fills):
                yield block


    # secondary writer support methods

    def _add_shape_aperture(self, shape, hole):
        """ Generate D code and store aperture. """
        next_ap = len(self.apertures) + 10
        aperture = Aperture(next_ap, shape, hole)
        if aperture not in self.apertures:
            self.apertures.append(aperture)


    def _add_macro_aperture(self, complex_instance):
        """ Generate D code and store aperture. """
        next_ap = len(self.apertures) + 10
        aperture = MacroAperture(next_ap, complex_instance.name, complex_instance.primitives)
        if aperture not in self.apertures:
            self.apertures.append(aperture)


    def _gen_smear(self, smear):
        """ Smears are lines drawn with rect apertures. """
        select = self._select_shape_aperture(smear.shape, None)
        if select:
            yield LINE.format(select)
        for block in self._draw_seg(smear.line):
            yield LINE.format(block)


    def _gen_shape_instance(self, shin):
        """ A snapshot of an aperture or macro. """
        select = self._select_shape_aperture(shin.shape,
                                             shin.hole)
        if select:
            yield LINE.format(select)
        block = self._flash(shin.x, shin.y)
        yield LINE.format(block)


    def _gen_complex_instance(self, complex_instance):
        """ An instance of a complex collection of primitives. """
        select = self._select_macro_aperture(complex_instance)

        if select:
            yield LINE.format(select)
        block = self._flash(complex_instance.x, complex_instance.y)
        yield LINE.format(block)


    def _gen_fills(self, fills):
        """ Fills are defined by their line/arc outlines. """
        fill_mode_on = FUNCT.format(type='G', code='36')
        yield LINE.format(fill_mode_on)
        for fill in fills:
            # Move to the first point in the outline
            yield LINE.format(MOVE.format(x=self._fix(fill.outline_points[0].x),
                                          y=self._fix(fill.outline_points[0].y)))

            for point in fill.outline_points[1:]:
                yield LINE.format(PLOT.format(x=self._fix(point.x),
                                              y=self._fix(point.y)))
        fill_mode_off = FUNCT.format(type='G', code='37')
        yield LINE.format(fill_mode_off)


    def _select_shape_aperture(self, shape, hole):
        """ Change the current aperture if necessary. """
        selection = Aperture(None, shape, hole)
        if selection == self.status['aperture']:
            return None
        else:
            index = self.apertures.index(selection)
            self.status['aperture'] = self.apertures[index]
            return FUNCT.format(type='D',
                                code=self.status['aperture'].code)


    def _select_macro_aperture(self, complex_instance):
        """ Change the current aperture if necessary. """
        selection = MacroAperture(None, complex_instance.name, complex_instance.primitives)
        if selection == self.status['aperture']:
            return None
        else:
            index = self.apertures.index(selection)
            self.status['aperture'] = self.apertures[index]
            return FUNCT.format(type='D',
                                code=self.status['aperture'].code)


    def _draw_seg(self, seg):
        """ Define path for a line or arc. """
        init_draw = not self.status['interpolation']
        mq_mode = None
        if isinstance(seg, Arc):
            interpolate = self._interpolate('CLOCKWISE_CIRCULAR')
            start, end = seg.ends()

            # handle clockwise representation of anticlock arcs
            if Point(self.status['x'], self.status['y']) == end:
                interpolate = self._interpolate('ANTICLOCKWISE_CIRCULAR')
                end, start = seg.ends()

            i, j = (seg.x - start.x, seg.y - start.y)

            # further establish interpolation mode
            mq_mode = self._check_mq(seg)
            if not self.status['multi_quadrant']:
                i, j = (abs(i), abs(j))

            move = COORD_EXT.format(x=self._fix(end.x),
                                    y=self._fix(end.y),
                                    i=self._fix(i),
                                    j=self._fix(j))
        else:
            interpolate = self._interpolate('LINEAR')
            start, end = (seg.p1, seg.p2)
            move = COORD.format(x=self._fix(end.x),
                                y=self._fix(end.y))

        # move to the start point of a draw
        stealth_move = self._move_start(start)

        # reset draw mode after stealth move
        if stealth_move or init_draw:
            move = move.replace('*', 'D01*')

        # compile directives for drawing segment
        self.status['x'] = end.x
        self.status['y'] = end.y
        blocks = (mq_mode, interpolate, stealth_move, move)
        return (b for b in blocks if b)


    def _move_start(self, point):
        """ Move the 'photo plotter head' without 'drawing'. """
        loc = Point(self.status['x'], self.status['y'])
        return ((not loc == point) and
                MOVE.format(x=self._fix(point.x),
                               y=self._fix(point.y)))


    def _flash(self, x, y):
        """ Create a shape instance. """
        return FLASH.format(x=self._fix(x),
                            y=self._fix(y))


    def _interpolate(self, code):
        """ Change the interpolation setting if necessary. """
        if self.status['interpolation'] == code:
            return None
        else:
            self.status['interpolation'] = code
            return FUNCT.format(type='G', code=INTERPOLATION[code])


    # general methods

    def _fix(self, ord_):
        """ Convert a float ordinate according to spec. Adjust from the core units to the units of the file. """
        dec = self.coord_format['dec']
        spec = '{{0}}{{1:0<{0}}}'.format(dec)
        unit_ord_ = self._convert_units(ord_)
        padded_ord = spec.format(*str(round(unit_ord_, dec)).split('.'))
        return int(padded_ord) and padded_ord or '0'


    def _convert_units(self, num):
        """ Convert from the core units (nm) to those of the current gerber being written. """
        # FIXME(shamer): adjust to actual units of gerber, is hard coded to mm
        return (num / 1000000.0)


    def _convert_units_str(self, num):
        """ Convert from the core units (nm) to a string of those of the current gerber being written. """
        return ('%.10f' % self._convert_units(num)).rstrip('0')


    def _parse_units(self, unitstr):
        parts = unitstr.split(' ')
        num = float(parts[0])
        units = parts[1].lower()
        if units == 'in':
            return int(num * POS_UNITS_PER_IN)
        elif units == 'mm':
            return int(num * POS_UNITS_PER_MM)
        elif units == 'cm':
            return int(num * POS_UNITS_PER_CM)
        elif units == 'mil':
            return int(num * POS_UNITS_PER_MM)


    def _check_mq(self, seg):
        """ Set multi_quadrant mode on/off if necessary. """

        # handle arcs that traverse the 3 o'clock boundary
        if seg.end_angle <= seg.start_angle:
            seg.end_angle += 2

        mq_seg = seg.end_angle - seg.start_angle > 0.5
        code = None
        if mq_seg and not self.status['multi_quadrant']:
            self.status['multi_quadrant'] = True
            code = '75'
        elif self.status['multi_quadrant'] and not mq_seg:
            self.status['multi_quadrant'] = False
            code = '74'
        return code and FUNCT.format(type='G', code=code)


    def _check_design(self, design):
        """ Ensure the design contains the necessary information to generate a gerber. """
        #TODO: check fills for closure and self-intersection
        if not design.layer_options:
            raise NoLayersFound
        return True

        for layer in design.layout.layers:
            for image in layer.images:
                if not (image.traces or
                        image.fills or
                        image.smears or
                        image.shape_instances):
                    raise ImageContainsNoData(image.name)
