#!/usr/bin/env python2
""" The Eagle Format Parser """

# upconvert.py - A universal hardware design file format converter using
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


from core.design import Design


class Eagle:
    """ The Eagle Format Parser """

    def __init__(self):
        pass


    def parse(self, filename):
        """ Parse an Eagle file into a design """
        design = Design()
        f = open(filename, "w")
        #TODO: Read!
        f.close()
        return design
