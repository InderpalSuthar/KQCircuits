# This code is part of KQCircuits
# Copyright (C) 2021 IQM Finland Oy
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
# warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not, see
# https://www.gnu.org/licenses/gpl-3.0.html.
#
# The software distribution should follow IQM trademark policy for open-source software
# (meetiqm.com/iqm-open-source-trademark-policy). IQM welcomes contributions to the code.
# Please see our contribution agreements for individuals (meetiqm.com/iqm-individual-contributor-license-agreement)
# and organizations (meetiqm.com/iqm-organization-contributor-license-agreement).


# Convert .oas to .dxf or the other way around
# usage: oas2dxf.py <some.oas>
#    or: oas2dxf.py <some.dxf>

from sys import argv
from os import path
from kqcircuits.pya_resolver import pya
from kqcircuits.util.load_save_layout import load_layout, save_layout

file_in = argv[1]

layout = pya.Layout()
load_layout(file_in, layout)

if file_in.endswith(".dxf"):
    layout.top_cell().name = path.split(file_in)[1][:-4]
    save_layout(file_in[0:-4] + ".oas", layout, oasis_substitution_char="*")
else:
    save_layout(file_in[0:-4] + ".dxf", layout)
