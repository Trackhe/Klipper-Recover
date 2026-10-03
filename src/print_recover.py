# Print recovery for Klipper — persist position / layer / file offset for resume
#
# Copyright (C) 2026
# This file may be distributed under the terms of the GNU GPLv3 license.

import json
import logging
import os
import tempfile
import time


def atomic_write_json(path, data):
    """Write JSON atomically (temp file in same dir + rename)."""
    directory = os.path.dirname(path) or '.'
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    fd, tmp = tempfile.mkstemp(prefix='.print_recover_', suffix='.tmp',
                               dir=directory)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json(path):
    if not path or not os.path.isfile(path):
        return None
    with open(path, 'r') as f:
        return json.load(f)


def _parse_current_layer_arg(line):
    for token in line.replace('\t', ' ').split():
        tu = token.upper()
        if tu.startswith('CURRENT_LAYER='):
            try:
                return int(token.split('=', 1)[1])
            except ValueError:
                return None
    return None


def _parse_layer_comment(line):
    upper = line.upper()
    for prefix in (';;LAYER:', ';LAYER:'):
        if upper.startswith(prefix):
            rest = line[len(prefix):].strip().split()[0]
            try:
                return int(rest.split(':')[-1])
            except ValueError:
                return None
    return None


def _candidate_acceptable(layer, z, target_layer, target_z):
    if target_layer is not None and layer is not None:
        return layer <= int(target_layer)
    if target_z is not None and z is not None:
        return float(z) <= float(target_z) + 1e-6
    return False


def find_layer_resume_offset(gcode_path, target_layer=None, target_z=None):
    """Byte offset of the latest layer start at/before target_layer or target_z.

    Recognizes SET_PRINT_STATS_INFO CURRENT_LAYER=, ;LAYER_CHANGE, ;LAYER:N,
    and ;Z: comments. Returns (offset, meta) or (None, reason).
    """
    best = None  # (offset, layer, z)
    pending_offset = None
    pending_layer = None

    def consider(offset, layer, z):
        nonlocal best
        if not _candidate_acceptable(layer, z, target_layer, target_z):
            return
        if best is None:
            best = (offset, layer, z)
            return
        # Prefer the latest acceptable layer/z (closest to crash point)
        _, bl, bz = best
        if layer is not None and bl is not None and layer >= bl:
            best = (offset, layer, z)
        elif z is not None and bz is not None and z >= bz:
            best = (offset, layer, z)
        elif layer is not None and bl is None:
            best = (offset, layer, z)

    with open(gcode_path, 'rb') as f:
        while True:
            offset = f.tell()
            raw = f.readline()
            if not raw:
                break
            line = raw.decode('utf-8', errors='replace').strip()
            upper = line.upper()

            if upper.startswith('SET_PRINT_STATS_INFO'):
                cur = _parse_current_layer_arg(line)
                if cur is not None:
                    consider(offset, cur, None)
                continue

            if upper.startswith(';LAYER_CHANGE'):
                pending_offset = offset
                pending_layer = None
                continue

            parsed = _parse_layer_comment(line)
            if parsed is not None:
                consider(offset, parsed, None)
                pending_offset = None
                pending_layer = None
                continue

            if upper.startswith(';Z:') or upper.startswith('; Z:'):
                try:
                    z = float(line.split(':', 1)[1].strip().split()[0])
                except ValueError:
                    z = None
                if z is not None and pending_offset is not None:
                    consider(pending_offset, pending_layer, z)
                    pending_offset = None
                    pending_layer = None
                continue

            # Flush bare LAYER_CHANGE once we leave its comment block
            if pending_offset is not None and not line.startswith(';'):
                if pending_layer is not None or target_layer is not None:
                    consider(pending_offset, pending_layer, None)
                pending_offset = None
                pending_layer = None

    if best is None:
        return None, 'no matching layer marker found'
    return best[0], {'layer': best[1], 'z': best[2]}


def parse_start_print_params(line):
    """Parse KEY=VALUE tokens from a START_PRINT ... line."""
    if not line:
        return {}
    out = {}
    # strip command name
    parts = line.strip().split()
    if not parts:
        return {}
    for token in parts[1:]:
        if '=' not in token:
            continue
        key, val = token.split('=', 1)
        out[key.strip().upper()] = val.strip().strip('"').strip("'")
    return out


def find_start_print_line(gcode_path, max_bytes=512 * 1024):
    """Return the first START_PRINT ... line from the head of a gcode file."""
    try:
        with open(gcode_path, 'rb') as f:
            data = f.read(max_bytes)
    except OSError:
        return None
    text = data.decode('utf-8', errors='replace')
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(';'):
            continue
        if line.upper().startswith('START_PRINT'):
            return line
    return None


def build_resume_preamble(state, z_hop=5.0, home_xy=True, platform='generic',
                          default_mesh_profile='', default_skew_profile=''):
    """G-code lines to restore temps / fans / XYZ before continuing the file."""
    pos = state.get('position') or {}
    gpos = state.get('gcode_position') or pos
    x = float(gpos.get('x', pos.get('x', 0.0)))
    y = float(gpos.get('y', pos.get('y', 0.0)))
    z = float(gpos.get('z', pos.get('z', 0.0)))
    e = float(gpos.get('e', pos.get('e', 0.0)))
    bed = state.get('bed') or {}
    extruder = state.get('extruder') or {}
    fan = state.get('fan') or {}
    speed_factor = float(state.get('speed_factor', 1.0) or 1.0)
    extrude_factor = float(state.get('extrude_factor', 1.0) or 1.0)

    sp = state.get('start_print_params') or parse_start_print_params(
        state.get('start_print_line')
    )
    bed_target = bed.get('target')
    ext_target = extruder.get('target')
    if (not bed_target or bed_target <= 0) and sp.get('BED_TEMP'):
        try:
            bed_target = float(sp['BED_TEMP'])
        except ValueError:
            pass
    if (not ext_target or ext_target <= 0) and sp.get('EXTRUDER_TEMP'):
        try:
            ext_target = float(sp['EXTRUDER_TEMP'])
        except ValueError:
            pass
    # Prefer other-layer nozzle temp when resuming mid-print
    if sp.get('EXTRUDER_OTHER_LAYER_TEMP') and (state.get('layer') or 0) not in (0, 1, None):
        try:
            ext_target = float(sp['EXTRUDER_OTHER_LAYER_TEMP'])
        except ValueError:
            pass

    fan_speed = fan.get('speed', 0.0)
    mesh = (state.get('mesh_profile') or default_mesh_profile or '').strip()
    skew = (state.get('skew_profile') or default_skew_profile or '').strip()

    lines = [
        '; ===== print_recover resume preamble =====',
        '; original: %s' % (state.get('filename') or state.get('file_path') or '?'),
        '; saved_file_position: %s' % (state.get('file_position'),),
        '; saved_layer: %s  saved_z: %.3f' % (state.get('layer'), z),
        '; platform: %s  mesh=%s  skew=%s' % (platform, mesh or '-', skew or '-'),
    ]

    if platform == 'ratos':
        # Lite START_PRINT: heat + load mesh/skew + unblock RatOS (no calib/prime)
        args = []
        if bed_target and bed_target > 0:
            args.append('BED_TEMP=%.1f' % (bed_target,))
        if ext_target and ext_target > 0:
            args.append('EXTRUDER_TEMP=%.1f' % (ext_target,))
        if mesh:
            args.append('MESH_PROFILE="%s"' % (mesh,))
        if skew:
            args.append('SKEW_PROFILE="%s"' % (skew,))
        if home_xy:
            args.append('HOME_XY=1')
        else:
            args.append('HOME_XY=0')
        lines.append('RECOVER_START_PRINT %s' % (' '.join(args),))
    else:
        lines.extend(['G90', 'M83'])
        if bed_target and bed_target > 0:
            lines.append('M140 S%.1f' % (bed_target,))
        if ext_target and ext_target > 0:
            lines.append('M104 S%.1f' % (ext_target,))
        if bed_target and bed_target > 0:
            lines.append('M190 S%.1f' % (bed_target,))
        if ext_target and ext_target > 0:
            lines.append('M109 S%.1f' % (ext_target,))
        if mesh:
            lines.append('BED_MESH_PROFILE LOAD="%s"' % (mesh,))
        if skew:
            lines.append('SKEW_PROFILE LOAD=%s' % (skew,))
        if home_xy:
            lines.append('G28 X Y')

    # Trust last Z via SET_KINEMATIC_POSITION after XY home (no Z endstop needed)
    lines.append('SET_KINEMATIC_POSITION Z=%.3f' % (z,))
    lines.append('G90')
    lines.append('G0 Z%.3f F600' % (z + max(0.0, z_hop),))
    lines.append('G0 X%.3f Y%.3f F6000' % (x, y))
    lines.append('G0 Z%.3f F300' % (z,))
    lines.append('G92 E%.5f' % (e,))
    lines.append('M82')
    if fan_speed and fan_speed > 0:
        lines.append('M106 S%d' % (int(max(0, min(255, round(fan_speed * 255)))),))
    else:
        lines.append('M107')
    # Restore feedrate / flow multipliers (M220 / M221 are percent)
    lines.append('M220 S%.1f' % (speed_factor * 100.0,))
    lines.append('M221 S%.1f' % (extrude_factor * 100.0,))
    lines.append('; ===== end preamble / continue gcode =====')
    return lines


def write_resume_gcode(src_path, dst_path, file_position, preamble_lines):
    """Write dst = preamble + src[file_position:].

    If file_position lands mid-line, skip forward to the next full line so we
    never execute a truncated G-code command. Start-of-line offsets are kept.
    """
    file_position = max(0, int(file_position))
    with open(src_path, 'rb') as src, open(dst_path, 'wb') as dst:
        for line in preamble_lines:
            dst.write((line + '\n').encode('utf-8'))
        if file_position > 0:
            src.seek(file_position - 1)
            prev = src.read(1)
            if prev not in (b'\n', b'\r'):
                # Mid-line: discard rest of this line
                src.readline()
            else:
                # Already at start of a line (pos points at first byte after \n)
                src.seek(file_position)
        else:
            src.seek(0)
        while True:
            chunk = src.read(1024 * 256)
            if not chunk:
                break
            dst.write(chunk)
    return dst_path

class PrintRecover:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode_move = self.printer.load_object(config, 'gcode_move')

        self.enabled = config.getboolean('enabled', True)
        self.track_moves = config.getboolean('track_moves', True)
        self.save_interval = config.getfloat(
            'save_interval', 30.0, minval=0.0, maxval=600.0
        )
        self.save_on_layer = config.getboolean('save_on_layer', True)
        self.save_on_shutdown = config.getboolean('save_on_shutdown', True)
        self.z_hop_on_resume = config.getfloat(
            'z_hop_on_resume', 5.0, minval=0.0
        )
        self.resume_xy_home = config.getboolean('resume_xy_home', True)
        self.min_file_position = config.getint('min_file_position', 0, minval=0)
        self.platform = (config.get('platform', 'generic') or 'generic').strip().lower()
        if self.platform not in ('generic', 'ratos'):
            raise config.error(
                "print_recover: platform must be 'generic' or 'ratos'"
            )
        self.default_mesh_profile = config.get('mesh_profile', '').strip()
        self.default_skew_profile = config.get('skew_profile', '').strip()
        self._start_print_cache_path = None

        default_state = os.path.expanduser(
            '~/printer_data/config/print_recover_state.json'
        )
        self.state_path = os.path.expanduser(
            config.get('state_path', default_state)
        )

        # Optional extra G-code injected into resume preamble
        self.before_resume_gcode = [
            line.strip()
            for line in config.get('before_resume_gcode', '').split('\n')
            if line.strip()
        ]

        self.toolhead = None
        self.next_transform = None
        self._timer = None
        self._dirty = False
        self._live = {}
        self._last_flush_time = 0.0
        self._resuming = False
        self._move_count = 0

        self.printer.register_event_handler('klippy:connect', self._handle_connect)
        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        self.printer.register_event_handler(
            'klippy:shutdown', self._handle_shutdown
        )
        self.printer.register_event_handler(
            'klippy:disconnect', self._handle_disconnect
        )

        self.gcode.register_command(
            'RECOVER_STATUS', self.cmd_RECOVER_STATUS,
            desc=self.cmd_RECOVER_STATUS_help)
        self.gcode.register_command(
            'RECOVER_SAVE', self.cmd_RECOVER_SAVE,
            desc=self.cmd_RECOVER_SAVE_help)
        self.gcode.register_command(
            'RECOVER_CLEAR', self.cmd_RECOVER_CLEAR,
            desc=self.cmd_RECOVER_CLEAR_help)
        self.gcode.register_command(
            'RECOVER_LAYER', self.cmd_RECOVER_LAYER,
            desc=self.cmd_RECOVER_LAYER_help)
        self.gcode.register_command(
            'RECOVER_ENABLE', self.cmd_RECOVER_ENABLE,
            desc=self.cmd_RECOVER_ENABLE_help)
        self.gcode.register_command(
            'RECOVER_PREPARE', self.cmd_RECOVER_PREPARE,
            desc=self.cmd_RECOVER_PREPARE_help)
        self.gcode.register_command(
            'RECOVER_RESUME', self.cmd_RECOVER_RESUME,
            desc=self.cmd_RECOVER_RESUME_help)

        logging.info(
            "print_recover: enabled=%s interval=%.2fs track_moves=%s "
            "platform=%s state=%s",
            self.enabled, self.save_interval, self.track_moves,
            self.platform, self.state_path
        )

    def _handle_connect(self):
        self.toolhead = self.printer.lookup_object('toolhead')

    def _handle_ready(self):
        if self.track_moves and self.next_transform is None:
            self.next_transform = self.gcode_move.set_move_transform(
                self, force=True
            )
        if self.save_interval > 0 and self._timer is None:
            self._timer = self.reactor.register_timer(
                self._timer_event, self.reactor.NOW
            )

    def _has_recoverable_live(self):
        if self._live.get('printing'):
            return True
        if self._live.get('filename') or self._live.get('file_path'):
            return True
        return int(self._live.get('file_position') or 0) > 0

    def _handle_shutdown(self):
        if self.save_on_shutdown and self.enabled and not self._resuming:
            try:
                # Prefer last known live state if lookups fail during teardown
                try:
                    self._collect_live(force=True)
                except Exception:
                    logging.exception("print_recover: collect during shutdown")
                if self._has_recoverable_live():
                    self._flush(reason='shutdown')
            except Exception:
                logging.exception("print_recover: shutdown flush failed")

    def _handle_disconnect(self):
        if self.save_on_shutdown and self.enabled and not self._resuming:
            try:
                try:
                    self._collect_live(force=True)
                except Exception:
                    logging.exception("print_recover: collect during disconnect")
                if self._has_recoverable_live():
                    self._flush(reason='disconnect')
            except Exception:
                logging.exception("print_recover: disconnect flush failed")

    # --- gcode move transform (in-memory position on every move) ---

    def get_position(self):
        return self.next_transform.get_position()

    def move(self, newpos, speed):
        self.next_transform.move(newpos, speed)
        if not self.enabled or self._resuming:
            return
        self._move_count += 1
        self._live['position'] = {
            'x': float(newpos[0]),
            'y': float(newpos[1]),
            'z': float(newpos[2]),
            'e': float(newpos[3]),
        }
        self._live['last_move_speed'] = float(speed)
        self._live['move_count'] = self._move_count
        self._dirty = True

    # --- state collection ---

    def _is_printing(self):
        try:
            ps = self.printer.lookup_object('print_stats')
            st = ps.get_status(self.reactor.monotonic()).get('state')
            return st in ('printing', 'paused')
        except Exception:
            return False

    def _collect_live(self, force=False):
        if not force and not self.enabled:
            return
        eventtime = self.reactor.monotonic()
        printing = self._is_printing()

        # Toolhead / gcode positions
        pos = None
        gpos = None
        try:
            if self.toolhead is not None:
                p = self.toolhead.get_position()
                pos = {'x': p[0], 'y': p[1], 'z': p[2], 'e': p[3]}
        except Exception:
            pos = self._live.get('position')
        try:
            gm = self.gcode_move.get_status(eventtime)
            gp = gm.get('gcode_position')
            if gp is not None:
                gpos = {
                    'x': float(gp[0]), 'y': float(gp[1]),
                    'z': float(gp[2]), 'e': float(gp[3]),
                }
            self._live['speed_factor'] = float(gm.get('speed_factor', 1.0))
            self._live['extrude_factor'] = float(gm.get('extrude_factor', 1.0))
            self._live['absolute_coordinates'] = bool(
                gm.get('absolute_coordinates', True)
            )
            self._live['absolute_extrude'] = bool(
                gm.get('absolute_extrude', True)
            )
        except Exception:
            pass

        if pos is not None:
            self._live['position'] = pos
        if gpos is not None:
            self._live['gcode_position'] = gpos

        # virtual_sdcard
        try:
            vsd = self.printer.lookup_object('virtual_sdcard')
            st = vsd.get_status(eventtime)
            self._live['file_path'] = st.get('file_path')
            self._live['file_position'] = int(st.get('file_position') or 0)
            self._live['file_size'] = int(st.get('file_size') or 0)
            self._live['sd_active'] = bool(st.get('is_active'))
            if self._live.get('file_path'):
                self._live['filename'] = os.path.basename(self._live['file_path'])
        except Exception:
            pass

        # print_stats / layers
        try:
            ps = self.printer.lookup_object('print_stats')
            st = ps.get_status(eventtime)
            self._live['print_state'] = st.get('state')
            self._live['print_duration'] = st.get('print_duration')
            self._live['filament_used'] = st.get('filament_used')
            if st.get('filename'):
                self._live['filename'] = st.get('filename')
            info = st.get('info') or {}
            if info.get('current_layer') is not None:
                self._live['layer'] = info.get('current_layer')
            if info.get('total_layer') is not None:
                self._live['total_layer'] = info.get('total_layer')
        except Exception:
            pass

        # heaters
        try:
            extruder = self.printer.lookup_object('extruder')
            est = extruder.get_status(eventtime)
            self._live['extruder'] = {
                'temperature': est.get('temperature'),
                'target': est.get('target'),
            }
        except Exception:
            pass
        try:
            bed = self.printer.lookup_object('heater_bed', None)
            if bed is not None:
                bst = bed.get_status(eventtime)
                self._live['bed'] = {
                    'temperature': bst.get('temperature'),
                    'target': bst.get('target'),
                }
        except Exception:
            pass

        # part fan (fan object if present)
        try:
            fan = self.printer.lookup_object('fan', None)
            if fan is not None:
                fst = fan.get_status(eventtime)
                self._live['fan'] = {'speed': fst.get('speed')}
        except Exception:
            pass

        # bed mesh profile currently applied
        try:
            bed_mesh = self.printer.lookup_object('bed_mesh', None)
            if bed_mesh is not None:
                bm = bed_mesh.get_status(eventtime)
                pname = bm.get('profile_name')
                if pname:
                    self._live['mesh_profile'] = pname
        except Exception:
            pass
        if self.default_mesh_profile and not self._live.get('mesh_profile'):
            self._live['mesh_profile'] = self.default_mesh_profile

        # skew profile (RatOS tracks loaded_profile on SKEW_PROFILE macro)
        skew = self.default_skew_profile or None
        try:
            skew_obj = self.printer.lookup_object('gcode_macro SKEW_PROFILE', None)
            if skew_obj is not None:
                loaded = (skew_obj.get_status(eventtime) or {}).get('loaded_profile')
                if loaded:
                    skew = loaded
        except Exception:
            pass
        try:
            ratos = self.printer.lookup_object('gcode_macro RatOS', None)
            if ratos is not None:
                rst = ratos.get_status(eventtime) or {}
                if not skew and rst.get('skew_profile'):
                    skew = rst.get('skew_profile')
                if not self._live.get('mesh_profile') and rst.get('bed_mesh_profile'):
                    self._live['mesh_profile'] = rst.get('bed_mesh_profile')
                elif (not self._live.get('mesh_profile')
                      and str(rst.get('calibrate_bed_mesh', '')).lower() == 'true'):
                    self._live['mesh_profile'] = 'ratos'
        except Exception:
            pass
        if skew:
            self._live['skew_profile'] = skew

        # Cache START_PRINT line from the active gcode (once per file)
        fpath = self._live.get('file_path')
        if fpath and fpath != self._start_print_cache_path:
            line = find_start_print_line(fpath)
            self._start_print_cache_path = fpath
            if line:
                self._live['start_print_line'] = line
                self._live['start_print_params'] = parse_start_print_params(line)
        elif self._live.get('start_print_line') and not self._live.get('start_print_params'):
            self._live['start_print_params'] = parse_start_print_params(
                self._live.get('start_print_line')
            )

        self._live['platform'] = self.platform
        self._live['printing'] = printing
        self._live['updated_at'] = time.time()
        self._live['mono_time'] = eventtime
        self._dirty = True

    def _should_persist(self):
        if not self.enabled or self._resuming:
            return False
        if not self._dirty:
            return False
        fp = int(self._live.get('file_position') or 0)
        if fp < self.min_file_position and not self._live.get('printing'):
            return False
        # Persist while printing/paused, or if we have meaningful progress
        if self._live.get('printing'):
            return True
        if fp > 0 and self._live.get('filename'):
            return True
        return False

    def _flush(self, reason='timer'):
        if not self._should_persist() and reason not in (
            'shutdown', 'disconnect', 'manual', 'layer'
        ):
            return False
        if reason in ('shutdown', 'disconnect', 'manual', 'layer'):
            # still require some data
            if not self._live.get('filename') and not self._live.get('file_path'):
                if reason not in ('manual',):
                    return False
        state = dict(self._live)
        state['save_reason'] = reason
        state['saved_at'] = time.time()
        try:
            atomic_write_json(self.state_path, state)
            self._dirty = False
            self._last_flush_time = self.reactor.monotonic()
            logging.debug("print_recover: flushed state (%s) pos=%s layer=%s",
                          reason, state.get('file_position'), state.get('layer'))
            return True
        except Exception:
            logging.exception("print_recover: failed to write %s", self.state_path)
            return False

    def _timer_event(self, eventtime):
        try:
            if self.enabled and not self._resuming:
                self._collect_live()
                if self._should_persist():
                    # Always refresh memory; flush on interval
                    if (eventtime - self._last_flush_time) >= self.save_interval:
                        self._flush(reason='timer')
        except Exception:
            logging.exception("print_recover: timer error")
        delay = self.save_interval if self.save_interval > 0 else 1.0
        return eventtime + max(0.25, delay)

    # --- commands ---

    cmd_RECOVER_STATUS_help = "Show live + saved print recovery state"

    def cmd_RECOVER_STATUS(self, gcmd):
        self._collect_live(force=True)
        live = self._live
        gcmd.respond_info(
            "print_recover: enabled=%s resuming=%s dirty=%s moves=%d"
            % (self.enabled, self._resuming, self._dirty, self._move_count)
        )
        pos = live.get('gcode_position') or live.get('position') or {}
        gcmd.respond_info(
            "  live: file=%s pos=%s layer=%s/%s xyz=(%.3f,%.3f,%.3f) state=%s"
            % (
                live.get('filename'),
                live.get('file_position'),
                live.get('layer'), live.get('total_layer'),
                float(pos.get('x', 0)), float(pos.get('y', 0)),
                float(pos.get('z', 0)),
                live.get('print_state'),
            )
        )
        saved = load_json(self.state_path)
        if not saved:
            gcmd.respond_info("  saved: (none) path=%s" % (self.state_path,))
            return
        spos = saved.get('gcode_position') or saved.get('position') or {}
        gcmd.respond_info(
            "  saved: file=%s pos=%s layer=%s xyz=(%.3f,%.3f,%.3f) reason=%s"
            % (
                saved.get('filename'),
                saved.get('file_position'),
                saved.get('layer'),
                float(spos.get('x', 0)), float(spos.get('y', 0)),
                float(spos.get('z', 0)),
                saved.get('save_reason'),
            )
        )

    cmd_RECOVER_SAVE_help = "Force-save current recovery state to disk"

    def cmd_RECOVER_SAVE(self, gcmd):
        self._collect_live(force=True)
        ok = self._flush(reason='manual')
        if ok:
            gcmd.respond_info("print_recover: state saved to %s" % (self.state_path,))
        else:
            gcmd.respond_info(
                "print_recover: nothing to save (not printing / empty state)"
            )

    cmd_RECOVER_CLEAR_help = "Delete saved recovery state"

    def cmd_RECOVER_CLEAR(self, gcmd):
        if os.path.isfile(self.state_path):
            os.unlink(self.state_path)
            gcmd.respond_info("print_recover: cleared %s" % (self.state_path,))
        else:
            gcmd.respond_info("print_recover: no state file")
        self._live = {}
        self._dirty = False

    cmd_RECOVER_LAYER_help = (
        "Mark layer change for recovery (call from slicer layer-change G-code)"
    )

    def cmd_RECOVER_LAYER(self, gcmd):
        layer = gcmd.get_int('LAYER', None)
        z = gcmd.get_float('Z', None)
        self._collect_live(force=True)
        if layer is not None:
            self._live['layer'] = layer
        if z is not None:
            # keep z in positions if toolhead lags
            for key in ('position', 'gcode_position'):
                if key in self._live and isinstance(self._live[key], dict):
                    self._live[key]['z'] = z
            self._live['layer_z'] = z
        self._dirty = True
        if self.save_on_layer and self.enabled:
            self._flush(reason='layer')
            gcmd.respond_info(
                "print_recover: layer saved layer=%s z=%s pos=%s"
                % (self._live.get('layer'), z, self._live.get('file_position'))
            )
        else:
            gcmd.respond_info("print_recover: layer noted (disk save disabled)")

    cmd_RECOVER_ENABLE_help = "Enable/disable print recovery tracking"

    def cmd_RECOVER_ENABLE(self, gcmd):
        enable = gcmd.get_int('ENABLE', None)
        if enable is None:
            gcmd.respond_info("print_recover: enabled=%s" % (self.enabled,))
            return
        self.enabled = bool(enable)
        gcmd.respond_info("print_recover: enabled=%s" % (self.enabled,))

    def _resolve_source_file(self, state):
        path = state.get('file_path')
        if path and os.path.isfile(path):
            return path
        name = state.get('filename')
        if not name:
            return None
        # Search virtual_sdcard directory
        try:
            vsd = self.printer.lookup_object('virtual_sdcard')
            sd = vsd.sdcard_dirname
            candidate = os.path.join(sd, name)
            if os.path.isfile(candidate):
                return candidate
            # walk once
            for root, dirs, files in os.walk(sd):
                if name in files:
                    return os.path.join(root, name)
        except Exception:
            pass
        return None

    def _prepare_resume(self, mode='exact'):
        state = load_json(self.state_path)
        if not state:
            raise self.printer.command_error(
                "print_recover: no saved state at %s" % (self.state_path,)
            )
        src = self._resolve_source_file(state)
        if not src:
            raise self.printer.command_error(
                "print_recover: original gcode not found (%s)"
                % (state.get('filename') or state.get('file_path'),)
            )

        if mode == 'layer':
            offset, meta = find_layer_resume_offset(
                src,
                target_layer=state.get('layer'),
                target_z=(state.get('layer_z')
                          or (state.get('gcode_position') or {}).get('z')
                          or (state.get('position') or {}).get('z')),
            )
            if offset is None:
                # fall back to exact byte position
                logging.warning(
                    "print_recover: layer seek failed (%s), using file_position",
                    meta,
                )
                offset = int(state.get('file_position') or 0)
                mode = 'exact-fallback'
            else:
                logging.info(
                    "print_recover: layer resume offset=%s meta=%s", offset, meta
                )
        else:
            offset = int(state.get('file_position') or 0)

        preamble = build_resume_preamble(
            state,
            z_hop=self.z_hop_on_resume,
            home_xy=self.resume_xy_home,
            platform=self.platform,
            default_mesh_profile=self.default_mesh_profile,
            default_skew_profile=self.default_skew_profile,
        )
        if self.before_resume_gcode:
            # insert after header comment block
            preamble = preamble[:1] + self.before_resume_gcode + preamble[1:]

        base = os.path.basename(src)
        root, ext = os.path.splitext(base)
        out_name = 'RECOVER-%s-%s%s' % (mode, root, ext or '.gcode')
        out_path = os.path.join(os.path.dirname(src), out_name)
        write_resume_gcode(src, out_path, offset, preamble)
        return {
            'state': state,
            'src': src,
            'out_path': out_path,
            'out_name': out_name,
            'offset': offset,
            'mode': mode,
        }

    cmd_RECOVER_PREPARE_help = (
        "Build a resume gcode (MODE=exact|layer) without starting it"
    )

    def cmd_RECOVER_PREPARE(self, gcmd):
        mode = (gcmd.get('MODE', 'exact') or 'exact').lower()
        if mode not in ('exact', 'layer'):
            raise gcmd.error("MODE must be exact or layer")
        info = self._prepare_resume(mode=mode)
        gcmd.respond_info(
            "print_recover: wrote %s (mode=%s offset=%s from %s)"
            % (info['out_name'], info['mode'], info['offset'],
               os.path.basename(info['src']))
        )

    cmd_RECOVER_RESUME_help = (
        "Prepare resume gcode and start it via virtual_sdcard (MODE=exact|layer)"
    )

    def cmd_RECOVER_RESUME(self, gcmd):
        mode = (gcmd.get('MODE', 'exact') or 'exact').lower()
        if mode not in ('exact', 'layer'):
            raise gcmd.error("MODE must be exact or layer")
        self._resuming = True
        try:
            info = self._prepare_resume(mode=mode)
            # Start print of the recovery file
            self.gcode.run_script_from_command(
                'SDCARD_PRINT_FILE FILENAME="%s"' % (info['out_name'],)
            )
            gcmd.respond_info(
                "print_recover: started %s (mode=%s offset=%s)"
                % (info['out_name'], info['mode'], info['offset'])
            )
        finally:
            # keep resuming flag until print actually runs a bit — clear on next timer
            self.reactor.register_callback(self._clear_resuming)

    def _clear_resuming(self, eventtime):
        self._resuming = False
        return self.reactor.NEVER

    def get_status(self, eventtime=None):
        saved = None
        try:
            saved = load_json(self.state_path)
        except Exception:
            saved = None
        pos = self._live.get('gcode_position') or self._live.get('position')
        return {
            'enabled': self.enabled,
            'dirty': self._dirty,
            'state_path': self.state_path,
            'filename': self._live.get('filename'),
            'file_position': self._live.get('file_position'),
            'layer': self._live.get('layer'),
            'position': pos,
            'has_saved_state': bool(saved),
            'saved_layer': (saved or {}).get('layer'),
            'saved_file_position': (saved or {}).get('file_position'),
            'move_count': self._move_count,
        }


def load_config(config):
    return PrintRecover(config)
