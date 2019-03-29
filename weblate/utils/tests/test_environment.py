# -*- coding: utf-8 -*-
#
# Copyright © 2012 - 2019 Michal Čihař <michal@cihar.com>
#
# This file is part of Weblate <https://weblate.org/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

from __future__ import unicode_literals

import os

from django.test import SimpleTestCase

from weblate.utils.environment import get_env_list, get_env_map


class EnvTest(SimpleTestCase):
    def test_list(self):
        os.environ['TEST_DATA'] = 'foo,bar,baz'
        self.assertEqual(get_env_list('TEST_DATA'), ['foo', 'bar', 'baz'])
        os.environ['TEST_DATA'] = 'foo'
        self.assertEqual(get_env_list('TEST_DATA'), ['foo'])
        del os.environ['TEST_DATA']
        self.assertEqual(get_env_list('TEST_DATA'), [])
        self.assertEqual(get_env_list('TEST_DATA', ['x']), ['x'])

    def test_map(self):
        os.environ['TEST_DATA'] = 'foo:bar,baz:bag'
        self.assertEqual(
            get_env_map('TEST_DATA'), {'foo': 'bar', 'baz': 'bag'}
        )
        os.environ['TEST_DATA'] = 'foo:bar'
        self.assertEqual(get_env_map('TEST_DATA'), {'foo': 'bar'})
        del os.environ['TEST_DATA']
        self.assertEqual(get_env_map('TEST_DATA'), {})
        self.assertEqual(get_env_map('TEST_DATA', {'x': 'y'}), {'x': 'y'})
