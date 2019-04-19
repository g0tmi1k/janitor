#!/usr/bin/python3
# Copyright (C) 2018 Jelmer Vernooij <jelmer@jelmer.uk>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA

import breezy  # noqa: E402
breezy.initialize()

from breezy.trace import note
from breezy.plugins.propose.propose import hosters

import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

from janitor import state  # noqa: E402

open_proposals = []
merged_proposals = []
closed_proposals = []

mps_by_state = {}

for name, hoster_cls in hosters.items():
    for instance in hoster_cls.iter_instances():
        note('Checking merge proposals on %r...', instance)
        for status in ['open', 'merged', 'closed']:
            for mp in instance.iter_my_proposals(status=status):
                state.set_proposal_status(mp.url, status)
