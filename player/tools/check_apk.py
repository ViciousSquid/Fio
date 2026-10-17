"""Check every APK native library, including p4a's compressed Python bundle.

ELF PT_LOAD segments must support 16 KiB pages. Uncompressed libraries must
also be ZIP-aligned to 16 KiB. This checks packaging, not device behavior.
"""
from __future__ import annotations

import argparse
import struct
import tarfile
import zipfile

PAGE_SIZE = 16384


def check_elf(header: bytes, name: str, *, abi='arm64-v8a') -> None:
    if header[:6] != b"\x7fELF\x02\x01" or len(header) < 64:
        raise ValueError(f"{name}: expected a little-endian ELF64 library")
    machine = {'arm64-v8a': 183, 'x86_64': 62}[abi]
    if struct.unpack_from("<HH", header, 16) != (3, machine):
        raise ValueError(f"{name}: expected a {abi} shared library")
    offset = struct.unpack_from("<Q", header, 32)[0]
    size, count = struct.unpack_from("<HH", header, 54)
    if size != 56 or count == 0 or offset < 64 or offset + size * count > len(header):
        raise ValueError(f"{name}: invalid or oversized ELF program headers")
    loads = 0
    for index in range(count):
        kind, flags, file_offset, address, physical, file_size, memory_size, alignment = struct.unpack_from(
            "<IIQQQQQQ", header, offset + index * size)
        if kind == 1:  # PT_LOAD
            loads += 1
            if alignment < PAGE_SIZE or (address - file_offset) % PAGE_SIZE:
                raise ValueError(f"{name}: PT_LOAD segment is not 16 KiB aligned")
    if not loads:
        raise ValueError(f"{name}: no loadable ELF segments")


def check_apk(path, *, abi='arm64-v8a') -> int:
    checked = 0
    bundled = 0
    with zipfile.ZipFile(path) as apk, open(path, "rb") as raw:
        for entry in apk.infolist():
            if not entry.filename.startswith("lib/") or not entry.filename.endswith(".so"):
                continue
            if not entry.filename.startswith(f"lib/{abi}/"):
                raise ValueError(f"Unexpected ABI: {entry.filename}")
            with apk.open(entry) as library:
                if entry.filename.endswith("/libpybundle.so"):
                    # p4a deliberately calls its gzip/tar Python bundle .so.
                    # Inspect its extension modules without extracting files.
                    with tarfile.open(fileobj=library, mode="r|*") as bundle:
                        for member in bundle:
                            if member.isfile() and member.name.endswith(".so"):
                                with bundle.extractfile(member) as extension:
                                    check_elf(extension.read(1024 * 1024), member.name, abi=abi)
                                checked += 1
                                bundled += 1
                    continue
                check_elf(library.read(1024 * 1024), entry.filename, abi=abi)
            if entry.compress_type == zipfile.ZIP_STORED:
                raw.seek(entry.header_offset)
                local_header = raw.read(30)
                name_size, extra_size = struct.unpack_from("<HH", local_header, 26)
                data_offset = entry.header_offset + 30 + name_size + extra_size
                if data_offset % PAGE_SIZE:
                    raise ValueError(f"{entry.filename}: uncompressed library is not ZIP-aligned to 16 KiB")
            checked += 1
    if not checked or not bundled:
        raise ValueError("APK is missing native libraries or p4a Python extension modules")
    return checked


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apk", nargs="+")
    parser.add_argument('--abi', choices=('arm64-v8a', 'x86_64'), default='arm64-v8a')
    args = parser.parse_args(argv)
    for path in args.apk:
        count = check_apk(path, abi=args.abi)
        print(f"PASS: {path}: {count} {args.abi} libraries support 16 KiB page alignment")


if __name__ == "__main__":
    main()
