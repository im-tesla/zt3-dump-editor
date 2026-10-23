# ZT3 Dump Editor

Disable the PIN/pattern lock, change the scooter serial or select a region in a
ZT3 VCU dump.

Python 3.10 or newer. No dependencies.

## Usage

~~~console
python zt3_dump_editor.py
~~~

Enter the dump path, choose whether to disable the lock, then enter a new serial
or leave it blank. Choose a region or leave it blank to keep the serial's region.
The script saves a separate file. It won't overwrite the original dump or an
existing output file.

Command-line options:

~~~console
python zt3_dump_editor.py dump.bin --inspect
python zt3_dump_editor.py dump.bin --disable-pin
python zt3_dump_editor.py dump.bin --serial 1K1DA0000P0001
python zt3_dump_editor.py dump.bin --region U
python zt3_dump_editor.py dump.bin --disable-pin --serial 1K1DA0000P0001 --region U -o edited.bin
~~~

The serial above is an example. Use quotes around paths containing spaces.
On Windows, you can use `py -3` instead of `python`.

## Region

The fourth character of a `1K1` serial selects the region.

| Option | Region | Firmware cap |
| --- | --- | --- |
| `--region D` | Germany | 20 km/h |
| `--region E` | Europe | 25 km/h, or 32 with the existing 32 km/h option enabled |
| `--region U` | USA | 40 km/h |

Region changes replace that character in both settings records and update their
checksums. If `--serial` is also supplied, the region is applied to the new serial.

For example, `1K1DA0000P0001` becomes `1K1UA0000P0001` with `--region U`.
The remaining characters stay the same. You may need to pair again using the
changed serial.

The 1.5.5 startup code was traced with the edited settings: D selected 20 km/h,
E selected 25 km/h and U selected 40 km/h for the tested dump. Speed on the
scooter hasn't been tested. The motor controller may impose another limit.
After flashing and restarting, check the Sport mode limit in the app.

The Europe setting uses bit `0x10` at record + `0x7E` to choose 32 instead of
25 km/h. This script leaves that setting alone.

The serial-region field is also used by [ZT3Tools](https://github.com/scooterteam/ZT3Tools/blob/main/zt3tool.cpp).

## Notes

- Tested on one ZT3 running VCU 1.5.5 for PIN removal and serial changes.
- Input must be a 131,072-byte raw flash dump. RAM dumps and update packages
  aren't supported.
- The script checks the firmware hash and both settings checksums before editing.
  Other firmware builds are rejected.
- Serials must contain exactly 14 uppercase letters or digits. The region option
  requires the `1K1` prefix.
- Pairing worked after changing to an unused serial. The script can't check
  whether a serial is unused.
- Disabling the lock leaves the stored PIN and pattern in place.
- Changes are applied to both settings records. Firmware, device keys, hardware
  identifiers and other fields in the dump are left alone.
- Flashing requires a separate tool that accepts full raw images. Keep the
  original dump.
- Whether these changes survive a firmware update hasn't been tested. Take a
  fresh dump after updating.

## Layout

Offsets are relative to the start of the dump.

| Field | Offset / value |
| --- | --- |
| Dump size | `0x20000` |
| Settings records | `0x1F000` and `0x1F400` |
| Record size | `0x200` |
| Magic | record + `0x00`, uint16 LE, `0x515C` |
| Scooter serial | record + `0x20`, 14 ASCII bytes |
| Region character | record + `0x23` |
| Flags | record + `0x3A`, uint16 LE |
| Lock enable bits | `0x0180` |
| Checksum | record + `0x1F4`, uint32 LE |

The checksum adds the signed little-endian 16-bit words in each record, skipping
the two words used by the checksum itself. The result is reduced modulo `2^32`.

The firmware ranges `[0x1440, 0xF45C)` and `[0x10440, 0x1E45C)` must both have
this SHA-256:

~~~text
1f678c9b9a5eac3902052f909380122f1584aed7be8f589982798573bd21d09f
~~~

## License

[MIT](LICENSE).

