"""Minimal seekable file-like object backed by HTTP Range requests, for use
with the stdlib zipfile module against a remote ZIP without downloading it
in full. Confirmed via raw requests testing that
download.marktstammdatenregister.de supports byte-range GETs (Accept-Ranges:
bytes, 206 responses with correct Content-Range) — remotezip 0.12.3 failed
against this specific server for unknown reasons, so this replaces it with a
small dependency-free implementation.

Also holds the two helpers shared across scripts: finding the current
Gesamtdatenexport URL, and fetching one ZIP member's bytes via a single
range request (rather than zipfile's default chunked reads, which would be
very chatty over HTTP for large members)."""
import re
import struct
import zlib
import zipfile

import requests

INDEX_URL = "https://www.marktstammdatenregister.de/MaStR/Datendownload"


class HTTPRangeFile:
    def __init__(self, url, session=None):
        self.url = url
        self.session = session or requests.Session()
        head = self.session.head(url, timeout=30, allow_redirects=True)
        head.raise_for_status()
        self.size = int(head.headers["Content-Length"])
        self.pos = 0

    def seek(self, offset, whence=0):
        if whence == 0:
            self.pos = offset
        elif whence == 1:
            self.pos += offset
        elif whence == 2:
            self.pos = self.size + offset
        return self.pos

    def tell(self):
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            end = self.size - 1
        else:
            end = min(self.pos + n, self.size) - 1
        if end < self.pos:
            return b""
        r = self.session.get(
            self.url, headers={"Range": f"bytes={self.pos}-{end}"}, timeout=60
        )
        r.raise_for_status()
        data = r.content
        self.pos += len(data)
        return data

    def seekable(self):
        return True


def find_latest_zip_url():
    html = requests.get(INDEX_URL, timeout=30).text
    m = re.search(
        r"https://download\.marktstammdatenregister\.de/Gesamtdatenexport_\d+_[\d.]+\.zip",
        html,
    )
    if not m:
        raise RuntimeError("Could not find Gesamtdatenexport URL on download page")
    return m.group(0)


def fetch_member_bytes(rf, zf, name):
    info = zf.getinfo(name)
    rf.seek(info.header_offset)
    local_header = rf.read(30)
    if local_header[:4] != b"PK\x03\x04":
        raise RuntimeError("Bad local file header")
    fname_len, extra_len = struct.unpack("<HH", local_header[26:30])
    data_start = info.header_offset + 30 + fname_len + extra_len
    rf.seek(data_start)
    compressed = rf.read(info.compress_size)
    if info.compress_type == zipfile.ZIP_STORED:
        return compressed
    return zlib.decompress(compressed, -15)
