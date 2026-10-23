from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import struct
import sys

__version__ = "1.1.0"
DUMP_SIZE = 0x20000
RECORD_BASES = (0x1F000, 0x1F400)
RECORD_SIZE = 0x200
RECORD_MAGIC = 0x515C
SERIAL_OFFSET = 0x20
SERIAL_LENGTH = 14
FLAGS_OFFSET = 0x3A
CODELOCK_MASK = 0x0180
CHECKSUM_OFFSET = 0x1F4
REGIONS = {"D": ("Germany", 20), "E": ("Europe", "25 or 32"), "U": ("USA", 40)}

CODE_RANGES = ((0x1440, 0xF45C), (0x10440, 0x1E45C))
KNOWN_CODE_SHA256 = "1f678c9b9a5eac3902052f909380122f1584aed7be8f589982798573bd21d09f"
PROFILE_NAME = "ZT3 VCU 1.5.5 (recognized firmware body)"


class DumpError(ValueError):
    pass


@dataclass(frozen=True)
class SettingsRecord:
    base: int
    serial: str
    flags: int
    checksum: int


def sha256(data: bytes | bytearray) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_serial(serial: str) -> str:
    if not isinstance(serial, str) or re.fullmatch(r"[A-Z0-9]{14}", serial) is None:
        raise DumpError("Serial must contain exactly 14 uppercase ASCII letters/digits.")
    return serial


def serial_with_region(serial: str, region: str) -> str:
    validate_serial(serial)
    if region not in REGIONS:
        raise DumpError("Region must be D, E or U.")
    if not serial.startswith("1K1"):
        raise DumpError("Region changes require a ZT3 serial starting with 1K1.")
    return serial[:3] + region + serial[4:]


def describe_region(serial: str) -> str:
    if serial.startswith("1K1") and serial[3] in REGIONS:
        name, speed = REGIONS[serial[3]]
        return f"{name} ({serial[3]}, {speed} km/h firmware cap)"
    return "unknown"


def record_checksum(data: bytes | bytearray, base: int) -> int:
    words = struct.unpack_from("<256h", data, base)
    skip = CHECKSUM_OFFSET // 2
    return (sum(words[:skip]) + sum(words[skip + 2:])) & 0xFFFFFFFF


def inspect_dump(data: bytes | bytearray) -> tuple[SettingsRecord, ...]:
    if len(data) != DUMP_SIZE:
        raise DumpError(
            f"Expected a {DUMP_SIZE:,}-byte raw VCU flash dump; got {len(data):,} bytes."
        )
    for start, end in CODE_RANGES:
        if sha256(data[start:end]) != KNOWN_CODE_SHA256:
            raise DumpError(
                f"Unrecognized firmware body at 0x{start:05X}. "
                "Only the recognized ZT3 VCU 1.5.5 build is supported."
            )

    records = []
    for base in RECORD_BASES:
        if struct.unpack_from("<H", data, base)[0] != RECORD_MAGIC:
            raise DumpError(f"Invalid settings magic at 0x{base:05X}.")
        stored = struct.unpack_from("<I", data, base + CHECKSUM_OFFSET)[0]
        if stored != record_checksum(data, base):
            raise DumpError(f"Invalid settings checksum at 0x{base:05X}.")
        raw_serial = data[base + SERIAL_OFFSET:base + SERIAL_OFFSET + SERIAL_LENGTH]
        try:
            serial = validate_serial(bytes(raw_serial).decode("ascii"))
        except (UnicodeDecodeError, DumpError) as exc:
            raise DumpError(f"Invalid serial field at 0x{base + SERIAL_OFFSET:05X}.") from exc
        flags = struct.unpack_from("<H", data, base + FLAGS_OFFSET)[0]
        records.append(SettingsRecord(base, serial, flags, stored))
    if records[0].serial != records[1].serial:
        raise DumpError("The two settings records contain different serials; no edit made.")
    return tuple(records)


def patch_dump(
    data: bytes,
    *,
    disable_pin: bool = False,
    new_serial: str | None = None,
    region: str | None = None,
) -> bytes:
    records = inspect_dump(data)
    if new_serial is not None:
        validate_serial(new_serial)
    if region is not None:
        new_serial = serial_with_region(new_serial if new_serial is not None else records[0].serial, region)
    candidate = bytearray(data)
    allowed = set()
    for record in records:
        base = record.base
        if disable_pin:
            struct.pack_into("<H", candidate, base + FLAGS_OFFSET,
                             record.flags & ~CODELOCK_MASK)
            allowed.update(range(base + FLAGS_OFFSET, base + FLAGS_OFFSET + 2))
        if new_serial is not None:
            start = base + SERIAL_OFFSET
            candidate[start:start + SERIAL_LENGTH] = new_serial.encode("ascii")
            allowed.update(range(start, start + SERIAL_LENGTH))
        if disable_pin or new_serial is not None:
            start = base + CHECKSUM_OFFSET
            struct.pack_into("<I", candidate, start, record_checksum(candidate, base))
            allowed.update(range(start, start + 4))

    changed = {i for i, (before, after) in enumerate(zip(data, candidate))
               if before != after}
    if len(candidate) != len(data) or not changed.issubset(allowed):
        raise DumpError("Internal verification failed: an unexpected byte changed.")
    verified = inspect_dump(candidate)
    for before, after in zip(records, verified):
        expected_flags = before.flags & ~CODELOCK_MASK if disable_pin else before.flags
        if after.flags != expected_flags:
            raise DumpError("Internal verification failed: flags differ from the request.")
        if after.serial != (new_serial if new_serial is not None else before.serial):
            raise DumpError("Internal verification failed: serial differs from the request.")
    return bytes(candidate)


def parse_path(value: str) -> Path:

    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if not value:
        raise DumpError("A dump path is required.")
    return Path(value).expanduser().resolve()


def read_dump(path: Path) -> bytes:
    with path.open("rb") as source:

        data = source.read(DUMP_SIZE + 1)
    if len(data) > DUMP_SIZE:
        raise DumpError(f"Dump is larger than {DUMP_SIZE:,} bytes.")
    return data


def write_output(source: Path, destination: Path, data: bytes) -> None:
    if source.resolve() == destination.resolve():
        raise DumpError("Output must be a different file from the input dump.")

    with destination.open("xb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    if destination.read_bytes() != data:
        raise DumpError(f"Output verification failed. Do not use: {destination}")


def ask_yes_no(prompt: str) -> bool:
    while True:
        answer = input(prompt).strip().lower()
        if answer in ("", "n", "no"):
            return False
        if answer in ("y", "yes"):
            return True
        print("Enter y or n.")


def suggested_output(source: Path, disable_pin: bool, new_serial: str | None) -> Path:
    suffix = "_pin_disabled" if disable_pin else ""
    if new_serial is not None:
        suffix += f"_sn_{new_serial}"
    return source.with_name(source.stem + (suffix or "_edited") + ".bin")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Edit PIN/pattern CodeLock, scooter serial and region in a "
                    "recognized ZT3 VCU 1.5.5 raw dump. Input is never overwritten.",
        epilog="Without edit flags, prompts for the desired changes. "
               "--inspect performs validation without writing a file.",
    )
    parser.add_argument("dump", nargs="?", help="path to the 128 KiB raw .bin dump")
    parser.add_argument("--disable-pin", action="store_true",
                        help="disable numeric/pattern CodeLock in both settings records")
    parser.add_argument("--serial", metavar="SERIAL",
                        help="new 14-character uppercase ASCII letter/digit serial")
    parser.add_argument("--region", type=str.upper, choices=tuple(REGIONS),
                        help="serial region: D=20, E=25/32, U=40 km/h firmware cap")
    parser.add_argument("-o", "--output", help="new output path (must not exist)")
    parser.add_argument("--inspect", action="store_true",
                        help="validate and display current settings; do not write")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.inspect and (args.disable_pin or args.serial is not None or args.region is not None or args.output):
        parser.error("--inspect cannot be combined with edit/output options.")
    try:
        print(f"ZT3 Dump Editor {__version__}")
        source = parse_path(args.dump if args.dump is not None else input("Dump .bin path: "))
        data = read_dump(source)
        records = inspect_dump(data)
        print(f"Firmware: {PROFILE_NAME}")
        print(f"Current scooter serial: {records[0].serial}")
        print(f"Region: {describe_region(records[0].serial)}")
        for record in records:
            state = "enabled" if record.flags & CODELOCK_MASK else "disabled"
            print(f"  Settings 0x{record.base:05X}: CodeLock {state}, "
                  f"flags 0x{record.flags:04X}, checksum OK")
        print(f"Input SHA-256: {sha256(data)}")
        if args.inspect:
            return 0

        disable_pin = args.disable_pin
        new_serial = args.serial
        region = args.region
        interactive = not disable_pin and new_serial is None and region is None
        if interactive:
            disable_pin = ask_yes_no("Disable PIN/pattern CodeLock? [y/N]: ")
            while True:
                answer = input("New scooter serial (Enter to keep current): ").strip()
                try:
                    new_serial = validate_serial(answer) if answer else None
                    break
                except DumpError as exc:
                    print(exc)
            print("Regions: D=Germany 20 km/h, E=Europe 25/32 km/h, U=USA 40 km/h")
            while True:
                answer = input("New region [D/E/U, Enter to keep serial region]: ").strip().upper()
                if not answer or answer in REGIONS:
                    region = answer or None
                    break
                print("Enter D, E, U or leave blank.")
        result = patch_dump(data, disable_pin=disable_pin, new_serial=new_serial, region=region)
        if result == data:
            print("No changes needed. No file written.")
            return 0

        final_serial = inspect_dump(result)[0].serial
        if final_serial != records[0].serial:
            print(f"New scooter serial: {final_serial}")
            print(f"New region: {describe_region(final_serial)}")
        default = suggested_output(source, disable_pin,
                                   final_serial if new_serial is not None or region is not None else None)
        if args.output is not None:
            destination = parse_path(args.output)
        elif interactive:
            answer = input(f"Output path [Enter for {default}]: ").strip()
            destination = parse_path(answer) if answer else default
        else:
            destination = default

        write_output(source, destination, result)
        count = sum(before != after for before, after in zip(data, result))
        print(f"Saved: {destination}")
        print(f"Changed bytes: {count}; both settings checksums verified.")
        print(f"Output SHA-256: {sha256(result)}")
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        return 130
    except (DumpError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
