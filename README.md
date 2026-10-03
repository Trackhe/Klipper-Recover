# Klipper Print Recover

Klipper-Extra, das während des Drucks **Position, Layer und G-Code-Datei-Offset** speichert — damit du nach **MCU-Disconnect / Klipper-Neustart** möglichst genau (sonst zumindest auf Layerhöhe) weitermachen kannst.

Gleiches Install-/Update-Manager-Muster wie [Klipper-CustomShape-Bed](https://github.com/Trackhe/Klipper-CustomShape-Bed) (`keepout_zone`).

## Qualitätsstufen

Ziel ist vor allem: **Klipper stirbt, Pi läuft weiter** — nicht der kalte Pi.

| Situation | Qualität | Warum |
|-----------|----------|--------|
| Klipper/MCU-Disconnect, geordneter Shutdown | **Hoch** | RAM wird in `klippy:shutdown` / `disconnect` sofort auf die SSD geschrieben |
| Klipper stirbt hart (`kill -9`), Pi bleibt an | Mittel | Letzter periodischer SSD-Flush (Default **30s**) + letzter Layer-Flush |
| Pi komplett aus (Strom weg) | Niedriger | Gleicher SSD-Stand, ggf. bis zu ~30s alt; `MODE=layer` oft sinnvoller |

**Speicher-Modell (bewusst so):**

1. **RAM** — jeder G0/G1 aktualisiert XYZ/`file_position`/Temps live  
2. **SSD** — alle `save_interval` Sekunden (Default 30) + Layer + Shutdown/Disconnect  
3. **Resume** — neuer `RECOVER-*.gcode` (Original bleibt unangetastet)

RAM allein überlebt keinen Klipper-Neustart (Prozess weg). Der Shutdown-Flush ist deshalb der wichtige Pfad für „nur Klipper neu“.

## Installation

```bash
cd ~
git clone https://github.com/Trackhe/Klipper-Recover.git
cd Klipper-Recover
./install.sh
```

```bash
cp ~/Klipper-Recover/config/print_recover.cfg.example \
   ~/printer_data/config/print_recover.cfg
cp ~/Klipper-Recover/config/print_recover_ratos.cfg \
   ~/printer_data/config/print_recover_ratos.cfg
```

In `printer.cfg`:

```ini
[include print_recover.cfg]
[include print_recover_ratos.cfg]
```

`platform: ratos` in der Config (Default im Example). `[virtual_sdcard]` muss aktiv sein. Dann **Save & Restart**.

### RatOS-Resume (wichtig)

Volles `START_PRINT` würde erneut meshen / Beacon / Prime laufen lassen. Stattdessen feuert die Resume-Preamble **`RECOVER_START_PRINT`**:

- heizen (Bed/Extruder aus State bzw. gespeicherter `START_PRINT`-Zeile)
- optional `G28 X Y`
- **`BED_MESH_PROFILE LOAD=…`** (kein `BED_MESH_CALIBRATE`)
- **`SKEW_PROFILE LOAD=…`** bzw. `_LOAD_RATOS_SKEW_PROFILE`
- **`SET_GCODE_VARIABLE MACRO=START_PRINT VARIABLE=is_printing_gcode VALUE=True`** — damit RatOS nicht blockiert
- kein Primeblob, kein QGL, kein Heatsoak

Mesh/Skew-Namen werden während des Drucks mitgespeichert; Fallback in der Config: `mesh_profile` / `skew_profile` (bei dir typisch `ratos` + `my_skew_profile`).

### Slicer (wichtig für Layer-Resume)

In **After layer change** / Layer-Change-G-Code:

```text
RECOVER_LAYER LAYER=[layer_num] Z=[layer_z]
SET_PRINT_STATS_INFO CURRENT_LAYER=[layer_num] TOTAL_LAYER=[total_layers]
```

(Platzhalter an Orca/Prusa/Cura anpassen.)

## Workflow nach Crash

1. Drucker / Klipper wieder online, Bett heizen falls nötig (Print sollte noch kleben).
2. `RECOVER_STATUS` — gespeicherte Datei / Layer / XYZ prüfen.
3. Resume starten:
   - `RECOVER_RESUME MODE=exact` — ab gespeichertem Byte-Offset + XYZ
   - `RECOVER_RESUME MODE=layer` — ab letztem Layer-Marker ≤ gespeichertem Layer/Z
4. Nur vorbereiten ohne Start: `RECOVER_PREPARE MODE=exact`
5. Nach erfolgreichem Resume / Verwerfen: `RECOVER_CLEAR`

## G-Code-Befehle

| Befehl | Funktion |
|--------|----------|
| `RECOVER_STATUS` | Live + gespeicherter State |
| `RECOVER_SAVE` | Sofort flushen |
| `RECOVER_LAYER LAYER=… Z=…` | Layer-Marker (Slicer) + Flush |
| `RECOVER_PREPARE MODE=exact\|layer` | `RECOVER-*.gcode` erzeugen |
| `RECOVER_RESUME MODE=exact\|layer` | Erzeugen + `SDCARD_PRINT_FILE` |
| `RECOVER_CLEAR` | State-Datei löschen |
| `RECOVER_ENABLE ENABLE=0/1` | Tracking soft aus/an |

## Konfiguration

Siehe [`config/print_recover.cfg.example`](config/print_recover.cfg.example).

| Option | Default | Bedeutung |
|--------|---------|-----------|
| `platform` | `generic` | `ratos` → `RECOVER_START_PRINT` statt nackter Heater-Zeilen |
| `mesh_profile` | (leer) | Fallback-Mesh-Name zum Laden |
| `skew_profile` | (leer) | Fallback-Skew-Name (z. B. `my_skew_profile`) |
| `save_interval` | `30.0` | Periodischer SSD-Flush (s); Shutdown flushs RAM sofort |
| `track_moves` | `True` | Jeden Move im RAM tracken |
| `save_on_layer` | `True` | Flush bei `RECOVER_LAYER` |
| `save_on_shutdown` | `True` | Flush bei Shutdown/Disconnect |
| `z_hop_on_resume` | `5.0` | Z-Hop in der Resume-Preamble |
| `resume_xy_home` | `True` | `G28 X Y` vor dem Anfahren |
| `state_path` | `~/printer_data/config/print_recover_state.json` | State-Datei |

Resume-Preamble setzt Temps, Fan, `G28 X Y`, `SET_KINEMATIC_POSITION Z=…`, fährt XY/Z an, setzt `G92 E`. Optional `before_resume_gcode` (z. B. Mesh laden).

## Grenzen

- Nach Power-Loss sind Stepper-Positionen weg — deshalb XY-Homing + Z über `SET_KINEMATIC_POSITION` (kein Blindflug aufs Modell).
- Exact-Resume setzt voraus, dass die Düse wirklich nahe der gespeicherten Stelle war und das Teil noch hält.
- Layer-Mode ist robuster, kann aber eine Teilschicht erneut drucken.
- Zusammen mit `keepout_zone`: beide nutzen Move-Transforms — Klipper verkettet sie; Reihenfolge = Load-Reihenfolge in der Config.

## Entwicklung

```bash
python3 tests/test_recover.py -v
```

## Lizenz

GNU GPLv3 — siehe [LICENSE](LICENSE).
