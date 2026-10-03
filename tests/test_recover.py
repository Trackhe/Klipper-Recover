#!/usr/bin/env python3
"""Unit tests for print_recover helpers (no Klipper required)."""

import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, ROOT)

from print_recover import (  # noqa: E402
    atomic_write_json,
    build_resume_preamble,
    find_layer_resume_offset,
    find_start_print_line,
    load_json,
    parse_start_print_params,
    write_resume_gcode,
)


SAMPLE = """\
; start
G28
M104 S200
;LAYER_CHANGE
;Z:0.20
;LAYER:1
G1 X10 Y10 E1
SET_PRINT_STATS_INFO CURRENT_LAYER=2
;LAYER_CHANGE
;Z:0.40
G1 X20 Y20 E2
SET_PRINT_STATS_INFO CURRENT_LAYER=3
;LAYER_CHANGE
;Z:0.60
G1 X30 Y30 E3
; end
"""


class TestLayerSeek(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            mode='w', suffix='.gcode', delete=False
        )
        self.tmp.write(SAMPLE)
        self.tmp.close()
        self.path = self.tmp.name

    def tearDown(self):
        os.unlink(self.path)

    def test_seek_by_layer(self):
        offset, meta = find_layer_resume_offset(self.path, target_layer=2)
        self.assertIsNotNone(offset)
        self.assertEqual(meta['layer'], 2)

    def test_seek_by_z(self):
        offset, meta = find_layer_resume_offset(self.path, target_z=0.40)
        self.assertIsNotNone(offset)
        self.assertAlmostEqual(meta['z'], 0.40, places=3)

    def test_prefers_latest_layer(self):
        offset, meta = find_layer_resume_offset(self.path, target_layer=3)
        self.assertEqual(meta['layer'], 3)


class TestAtomicAndResumeFile(unittest.TestCase):
    def test_atomic_roundtrip(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, 'state.json')
        atomic_write_json(path, {'a': 1, 'b': [1, 2]})
        data = load_json(path)
        self.assertEqual(data['a'], 1)
        os.unlink(path)
        os.rmdir(d)

    def test_write_resume_gcode(self):
        d = tempfile.mkdtemp()
        src = os.path.join(d, 'job.gcode')
        dst = os.path.join(d, 'RECOVER-exact-job.gcode')
        body = 'AAAA\nBBBB\nCCCC\n'
        with open(src, 'w') as f:
            f.write(body)
        # resume from start of BBBB line
        pos = body.index('BBBB')
        state = {
            'filename': 'job.gcode',
            'file_position': pos,
            'layer': 2,
            'position': {'x': 1, 'y': 2, 'z': 3, 'e': 4},
            'gcode_position': {'x': 1, 'y': 2, 'z': 3, 'e': 4},
            'bed': {'target': 60},
            'extruder': {'target': 200},
            'fan': {'speed': 0.5},
            'speed_factor': 1.0,
            'extrude_factor': 1.0,
        }
        preamble = build_resume_preamble(state, z_hop=5, home_xy=True)
        write_resume_gcode(src, dst, pos, preamble)
        with open(dst, 'r') as f:
            out = f.read()
        self.assertIn('G28 X Y', out)
        self.assertIn('SET_KINEMATIC_POSITION Z=3.000', out)
        self.assertIn('BBBB', out)
        self.assertIn('CCCC', out)
        self.assertNotIn('AAAA', out.split('===== end preamble')[-1])
        os.unlink(src)
        os.unlink(dst)
        os.rmdir(d)

    def test_ratos_preamble_calls_recover_start_print(self):
        state = {
            'filename': 'job.gcode',
            'file_position': 10,
            'layer': 5,
            'mesh_profile': 'ratos',
            'skew_profile': 'my_skew_profile',
            'position': {'x': 1, 'y': 2, 'z': 3, 'e': 4},
            'gcode_position': {'x': 1, 'y': 2, 'z': 3, 'e': 4},
            'bed': {'target': 60},
            'extruder': {'target': 210},
            'fan': {'speed': 0},
            'start_print_line': (
                'START_PRINT EXTRUDER_TEMP=215 EXTRUDER_OTHER_LAYER_TEMP=210 '
                'BED_TEMP=60'
            ),
        }
        lines = build_resume_preamble(
            state, platform='ratos', home_xy=True,
            default_mesh_profile='ratos',
            default_skew_profile='my_skew_profile',
        )
        joined = '\n'.join(lines)
        self.assertIn('RECOVER_START_PRINT', joined)
        self.assertIn('MESH_PROFILE="ratos"', joined)
        self.assertIn('SKEW_PROFILE="my_skew_profile"', joined)
        self.assertIn('EXTRUDER_TEMP=210.0', joined)  # other-layer temp
        self.assertNotIn('BED_MESH_CALIBRATE', joined)
        self.assertIn('SET_KINEMATIC_POSITION Z=3.000', joined)


class TestStartPrintParse(unittest.TestCase):
    def test_parse_params(self):
        line = (
            'START_PRINT EXTRUDER_TEMP=215 EXTRUDER_OTHER_LAYER_TEMP=210 '
            'BED_TEMP=60 X0=1.5 Y0=2'
        )
        p = parse_start_print_params(line)
        self.assertEqual(p['BED_TEMP'], '60')
        self.assertEqual(p['EXTRUDER_TEMP'], '215')
        self.assertEqual(p['X0'], '1.5')

    def test_find_start_print_line(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, 't.gcode')
        with open(path, 'w') as f:
            f.write('; comment\nG28\nSTART_PRINT BED_TEMP=60 EXTRUDER_TEMP=200\nG1 X0\n')
        line = find_start_print_line(path)
        self.assertTrue(line.startswith('START_PRINT'))
        os.unlink(path)
        os.rmdir(d)


if __name__ == '__main__':
    unittest.main()
