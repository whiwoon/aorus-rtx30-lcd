#!/usr/bin/env python3
"""aorus-lcd: GIGABYTE RTX 3090 AORUS XTREME LCD (Edge View) control over the GPU's own I2C bus.

Standard library only (Python 3.12). Install as /usr/local/sbin/aorus-lcd on the machine (or VM) that owns the GPU.
Protocol: legacy 0x61 LCD controller on the GPU's NVIDIA i2c adapter 1, firmware F2.3.
Every command is one 256-byte I2C write [opcode, CB 55 AC 38, args..., 0...]; a query is
that write followed by an exactly-4-byte read in a single I2C_RDWR transaction.

Pure functions (packets, header, container, RLE, response parsing) are separated from I/O
so they can be tested without hardware. See README.md for sources.
"""
import argparse
import contextlib
import ctypes
import errno
import fcntl
import glob
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from dataclasses import dataclass

# ---------------------------------------------------------------- constants
I2C_ADDR = 0x61                                  # LCD controller; never any other address
BUS_NAME_PREFIX = 'NVIDIA i2c adapter 1 at '
SYSFS_GLOB = '/sys/class/i2c-dev/i2c-*/name'
LOCK_PATH = '/run/lock/aorus-lcd.lock'
LOCK_TIMEOUT_S = 180.0
MAGIC = b'\xCB\x55\xAC\x38'
PACKET = 256
QUERY_LEN = 4

OP_FIRMWARE, OP_UPLOAD_STATE, OP_STATE, OP_OVERLAY_GET = 0xD6, 0xD7, 0xDE, 0xDF
OP_OVERLAY_SET, OP_SENSOR, OP_MODE, OP_POWER = 0xE1, 0xE3, 0xE5, 0xE7
OP_UPLOAD_HEADER, OP_UPLOAD_MARK, OP_LOOP, OP_SAVE = 0xF1, 0xF2, 0xF3, 0xAA

MODE_NAMES = ('faith1', 'faith2', 'faith3', 'image', 'text', 'gif', 'chibi', 'carousel')
MODE_IMAGE, MODE_TEXT, MODE_GIF, MODE_CAROUSEL = 3, 4, 5, 7
CAROUSEL_RAW = 9                                 # firmware's internal value for mode 7
BOUNCE_MODE = 0                                  # faith1: passed through to force a redraw
WIDGETS = ('temp', 'clock', 'usage', 'fan', 'vram-clock', 'vram', 'fps', 'power')

W, H = 320, 170
FRAME_PIXELS = W * H
FRAME_BYTES = FRAME_PIXELS * 2                   # 108800
FMT_RAW, FMT_RLE = 1, 3
IMAGE_SIZE = 2 + 10 + FRAME_BYTES                # 108812
GIF_MAX_BYTES = 4 * 1024 * 1024
GIF_MAX_FRAMES = 255
RLE_WINDOW = 0x7FFF

# Upload targets for non-50-series cards (GCC 26.09.10.01 ucVga.dll GvLcdApi.SendImage).
UPLOAD_KINDS = {
    #        flash address  storage  mode
    'image': (0x01F25800, 1, MODE_IMAGE),
    'text':  (0x01F00000, 1, MODE_TEXT),
    'gif':   (0x00000000, 2, MODE_GIF),
}
PAGE_RETRIES = 8
PAGE_RETRY_S = 0.25
PAGE_GAP_S = 0.001                               # prototype pacing (GCC: ~0.75 us)
RETRY_HINT = '업로드가 중간에 실패했다. 같은 명령으로 다시 업로드하면 된다.'


class LcdError(Exception):
    code = 1


class ProbeError(LcdError):
    code = 2


class UploadError(LcdError):
    code = 3


# ---------------------------------------------------------------- pure: packets
def _byte(v, name='value'):
    v = int(v)
    if not 0 <= v <= 0xFF:
        raise LcdError(f'{name} {v} is outside 0..255')
    return v


def packet(op, *args):
    """256-byte command: [op, CB 55 AC 38, args..., zero padding]."""
    body = bytes([_byte(op, 'opcode')]) + MAGIC + bytes(_byte(a) for a in args)
    if len(body) > PACKET:
        raise LcdError('packet too long')
    return body + bytes(PACKET - len(body))


def mode_arg(mode):
    """E5 argument for user mode 0..7 (mode 7 = internal 9 -> 10)."""
    if not 0 <= mode <= 7:
        raise LcdError(f'mode {mode} is outside 0..7')
    return (CAROUSEL_RAW if mode == MODE_CAROUSEL else mode) + 1


def parse_mode(text):
    t = str(text).strip().lower()
    if t.isdigit() and 0 <= int(t) <= 7:
        return int(t)
    if t in MODE_NAMES:
        return MODE_NAMES.index(t)
    raise LcdError(f'unknown mode {text!r}; use 0-7 or {", ".join(MODE_NAMES)}')


def page_count(size):
    return size // 256 + 1                       # GCC: +1 even when size % 256 == 0


def erase_mode(size):
    return 2 if size >= 20480 else 1             # 2 = 64 KiB blocks, 1 = 4 KiB sectors


def erase_wait_s(size):
    if erase_mode(size) == 2:
        return -(-size // 65536) * 2.0
    return -(-size // 4096) * 0.4


def upload_header(addr, size, storage, frames=0, delay_ms=0):
    """19-byte F1 header (GvLcdApi.SendImage)."""
    return (bytes([OP_UPLOAD_HEADER]) + MAGIC + addr.to_bytes(4, 'big') + bytes([storage])
            + page_count(size).to_bytes(4, 'big') + frames.to_bytes(2, 'big')
            + bytes([max(0, min(255, delay_ms)), erase_mode(size), 0]))


def header_packet(addr, size, storage, frames=0, delay_ms=0):
    h = upload_header(addr, size, storage, frames, delay_ms)
    return h + bytes(PACKET - len(h))


def pages(payload):
    n = page_count(len(payload))
    data = payload + bytes(n * 256 - len(payload))
    return [data[i * 256:(i + 1) * 256] for i in range(n)]


def overlay_args(widgets, interval):
    flags = [1 if w in widgets else 0 for w in WIDGETS]
    return flags + [_byte(interval, 'interval')]


def loop_args(modes, interval):
    if not 1 <= len(modes) <= 7:
        raise LcdError('loop needs 1..7 modes')
    for m in modes:
        if not 0 <= m <= 6:
            raise LcdError('loop accepts modes 0..6 only')
    return [_byte(interval, 'interval')] + [m + 1 for m in modes]


def sensor_args(temp=0, clock=0, usage=0, fan=0, vram_clock=0, vram=0, fps=0, power=0):
    """E3 (experimental): [temp u8][clock u16][usage u8][fan u16][vram clock u16][vram % u8][fps u16][power u16], BE."""
    u8 = lambda v: max(0, min(255, int(v)))
    u16 = lambda v: list(max(0, min(65535, int(v))).to_bytes(2, 'big'))
    return ([u8(temp)] + u16(clock) + [u8(usage)] + u16(fan) + u16(vram_clock) + [u8(vram)]
            + u16(fps) + u16(power))


# ---------------------------------------------------------------- pure: responses
def parse_firmware(r):
    if len(r) != 4 or r[0] != OP_FIRMWARE:
        raise ProbeError(f'unexpected D6 response {r.hex(" ")}')
    return f'F{r[1] >> 4}.{r[1] & 0xF}'


def parse_state(r):
    if len(r) != 4 or r[0] != OP_STATE:
        raise LcdError(f'unexpected DE response {r.hex(" ")}')
    raw = r[1] - 1
    mode = MODE_CAROUSEL if raw == CAROUSEL_RAW else raw
    name = MODE_NAMES[mode] if 0 <= mode < len(MODE_NAMES) else 'unknown'
    on = {1: True, 2: False}.get(r[2])
    return {'mode': mode, 'mode_name': name, 'on': on, 'raw': r.hex(' ')}


def parse_overlay(r):
    if len(r) != 4 or r[0] != OP_OVERLAY_GET:
        raise LcdError(f'unexpected DF response {r.hex(" ")}')
    return {'widgets': [w for i, w in enumerate(WIDGETS) if r[1] >> i & 1],
            'interval': r[2], 'raw': r.hex(' ')}


def parse_upload_state(r):
    if len(r) != 4 or r[0] != OP_UPLOAD_STATE:
        raise LcdError(f'unexpected D7 response {r.hex(" ")}')
    return {'value': r[1], 'idle': r[1] == 0, 'raw': r.hex(' ')}


# ---------------------------------------------------------------- pure: container / RLE
@dataclass(frozen=True)
class Frame:
    start: int
    end: int                                     # exclusive
    w: int
    h: int
    fmt: int


def build_container(blobs, fmt):
    """u16 count | per frame [u32 inclusive end offset, u16 w, u16 h, u16 fmt] | data (LE)."""
    out = bytearray(len(blobs).to_bytes(2, 'little'))
    end = 2 + 10 * len(blobs)
    for b in blobs:
        end += len(b)
        out += struct.pack('<IHHH', end - 1, W, H, fmt)
    return bytes(out) + b''.join(blobs)


def build_image_container(px):
    if len(px) != FRAME_BYTES:
        raise LcdError(f'image needs {FRAME_BYTES} bytes of RGB565, got {len(px)}')
    return build_container([px], FMT_RAW)


def build_gif_container(frames):
    return build_container([rle_encode(f) for f in frames], FMT_RLE)


def parse_container(blob):
    if len(blob) < 12:
        raise LcdError('container too short')
    count = int.from_bytes(blob[:2], 'little')
    start = 2 + 10 * count
    if count == 0 or start > len(blob):
        raise LcdError(f'bad frame count {count}')
    frames = []
    for k in range(count):
        end, w, h, fmt = struct.unpack_from('<IHHH', blob, 2 + 10 * k)
        if end < start or end >= len(blob):
            raise LcdError(f'frame {k} end offset {end} out of range')
        frames.append(Frame(start, end + 1, w, h, fmt))
        start = end + 1
    if start != len(blob):
        raise LcdError(f'container has {len(blob) - start} trailing bytes')
    return frames


def rle_encode(px):
    """Encode LE-RGB565 pixels with the token grammar of GCC Compress_RLE.

    Token head u16 LE: bit 15 = run, low 15 bits = pixel count.
      run: head|0x8000 + one pixel (only for >= 3 equal pixels); literal: head + pixels.
    Scan in windows of <= 32767 pixels; the literal stretches to the first 3-pixel repeat
    in the window, a run never crosses the window end, and a window with no repeat or
    with fewer than 4 pixels is one literal. Re-implemented from the description in
    c-drew/gigabyte-aorus-lcd-linux (protocol.py rle_encode_frame)."""
    n = len(px) // 2
    if n < 3 or len(px) % 2:
        raise LcdError('frame too small for RLE')
    v = struct.unpack(f'<{n}H', px)
    out = bytearray()
    i = 0
    while i < n:
        end = i + min(RLE_WINDOW, n - i)
        lit, run = end - i, 0
        if end - i >= 4:
            for j in range(i, end - 2):
                p = v[j]
                if v[j + 1] == p and v[j + 2] == p:
                    k = j + 3
                    while k < end and v[k] == p:
                        k += 1
                    lit, run = j - i, k - j
                    break
        if lit:
            out += lit.to_bytes(2, 'little') + px[2 * i:2 * (i + lit)]
        if run:
            s = 2 * (i + lit)
            out += (run | 0x8000).to_bytes(2, 'little') + px[s:s + 2]
        i += lit + run
    return bytes(out)


def rle_decode(blob, pixels=FRAME_PIXELS):
    out = bytearray()
    i = 0
    while i < len(blob):
        if i + 2 > len(blob):
            raise LcdError('truncated RLE token')
        head = int.from_bytes(blob[i:i + 2], 'little')
        count = head & 0x7FFF
        if count == 0:
            raise LcdError('zero-length RLE token')
        if head & 0x8000:
            if i + 4 > len(blob):
                raise LcdError('truncated RLE run')
            out += blob[i + 2:i + 4] * count
            i += 4
        else:
            if i + 2 + 2 * count > len(blob):
                raise LcdError('truncated RLE literal')
            out += blob[i + 2:i + 2 + 2 * count]
            i += 2 + 2 * count
        if len(out) > 2 * pixels:
            raise LcdError('RLE frame decodes to too many pixels')
    if len(out) != 2 * pixels:
        raise LcdError(f'RLE frame decodes to {len(out) // 2} pixels, expected {pixels}')
    return bytes(out)


def validate_upload(kind, blob):
    """Return frame count for the header; raise LcdError for anything unexpected."""
    if kind not in UPLOAD_KINDS:
        raise LcdError(f'unknown upload kind {kind!r}')
    if kind in ('image', 'text'):
        if len(blob) != IMAGE_SIZE:
            raise LcdError(f'{kind} container must be {IMAGE_SIZE} bytes, got {len(blob)}')
        frames = parse_container(blob)
        f = frames[0] if len(frames) == 1 else None
        if f is None or (f.w, f.h, f.fmt) != (W, H, FMT_RAW) or f.end - f.start != FRAME_BYTES:
            raise LcdError(f'{kind} container must be one {W}x{H} raw RGB565 frame')
        return 0                                 # GCC sends frame count 0 for stills (measured)
    if len(blob) > GIF_MAX_BYTES:
        raise LcdError(f'gif container {len(blob)} bytes exceeds {GIF_MAX_BYTES}')
    frames = parse_container(blob)
    if not 1 <= len(frames) <= GIF_MAX_FRAMES:
        raise LcdError(f'gif needs 1..{GIF_MAX_FRAMES} frames, got {len(frames)}')
    for k, f in enumerate(frames):
        if (f.w, f.h, f.fmt) != (W, H, FMT_RLE):
            raise LcdError(f'gif frame {k} must be {W}x{H} RLE (format 3)')
        rle_decode(blob[f.start:f.end])
    return len(frames)


# ---------------------------------------------------------------- I/O: bus discovery and transport
def find_bus(pattern=SYSFS_GLOB):
    """Return /dev/i2c-N for the single adapter named 'NVIDIA i2c adapter 1 at ...'."""
    hits = []
    for name_file in sorted(glob.glob(pattern)):
        try:
            with open(name_file) as fh:
                name = fh.read().strip()
        except OSError:
            continue
        if name.startswith(BUS_NAME_PREFIX):
            hits.append(os.path.basename(os.path.dirname(name_file)))
    if len(hits) != 1:
        raise ProbeError(f'expected exactly one "{BUS_NAME_PREFIX}..." adapter, found {len(hits)} '
                         f'({", ".join(hits) or "none"}); is i2c-dev loaded and is this the GPU VM?')
    return '/dev/' + hits[0]


I2C_RDWR, I2C_M_RD = 0x0707, 0x0001


class _Msg(ctypes.Structure):
    _fields_ = [('addr', ctypes.c_uint16), ('flags', ctypes.c_uint16),
                ('len', ctypes.c_uint16), ('buf', ctypes.POINTER(ctypes.c_uint8))]


class _RdwrData(ctypes.Structure):
    _fields_ = [('msgs', ctypes.POINTER(_Msg)), ('nmsgs', ctypes.c_uint32)]


class I2CTransport:
    """Raw I2C_RDWR to address 0x61 only. The address is not a parameter on purpose."""

    def __init__(self, path, ioctl=fcntl.ioctl):
        self.path, self._ioctl, self.fd = path, ioctl, None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR)
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _xfer(self, data, read_len=0):
        if len(data) != PACKET:
            raise LcdError('internal: packet must be 256 bytes')
        wbuf = (ctypes.c_uint8 * PACKET).from_buffer_copy(data)
        msgs = [_Msg(I2C_ADDR, 0, PACKET, wbuf)]
        rbuf = None
        if read_len:
            rbuf = (ctypes.c_uint8 * read_len)()
            msgs.append(_Msg(I2C_ADDR, I2C_M_RD, read_len, rbuf))
        arr = (_Msg * len(msgs))(*msgs)
        self._ioctl(self.fd, I2C_RDWR, _RdwrData(arr, len(msgs)))
        return bytes(rbuf) if rbuf is not None else None

    def write(self, data):
        self._xfer(data)

    def query(self, data):
        return self._xfer(data, QUERY_LEN)


@contextlib.contextmanager
def device_lock(path=LOCK_PATH, timeout=LOCK_TIMEOUT_S, sleep=time.sleep):
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES) or time.monotonic() > deadline:
                    raise LcdError(f'could not lock {path}: another aorus-lcd is running') from e
                sleep(0.2)
        yield
    finally:
        os.close(fd)


# ---------------------------------------------------------------- device logic (transport-agnostic)
class Device:
    """All writes go through _write, which refuses until a D6 probe succeeded."""

    def __init__(self, transport, sleep=time.sleep, log=None):
        self.t, self.sleep = transport, sleep
        self.log = log or (lambda msg: print(msg, file=sys.stderr))
        self.firmware = None

    def probe(self):
        r = self.t.query(packet(OP_FIRMWARE))
        if not r or r[0] != OP_FIRMWARE:
            raise ProbeError(f'LCD probe failed (D6 -> {r.hex(" ") if r else "nothing"}); no write sent')
        self.firmware = parse_firmware(r)
        return self.firmware

    def _write(self, data):
        if self.firmware is None:
            raise ProbeError('internal: write before successful D6 probe')
        self.t.write(data)

    def command(self, op, *args):
        self._write(packet(op, *args))

    def state(self):
        return parse_state(self.t.query(packet(OP_STATE)))

    def overlay(self):
        return parse_overlay(self.t.query(packet(OP_OVERLAY_GET)))

    def upload_state(self):
        return parse_upload_state(self.t.query(packet(OP_UPLOAD_STATE)))

    def status(self):
        fw = self.firmware or self.probe()
        return {'firmware': fw, **self.state(), 'overlay': self.overlay(),
                'upload_state': self.upload_state()}

    def set_power(self, on):
        self.command(OP_POWER, 1 if on else 2)

    def set_mode(self, mode):
        self.command(OP_MODE, mode_arg(mode))

    def set_overlay(self, widgets, interval):
        self.command(OP_OVERLAY_SET, *overlay_args(widgets, interval))

    def set_loop(self, modes, interval):
        self.command(OP_LOOP, *loop_args(modes, interval))

    def save(self):
        self.command(OP_SAVE)

    def feed(self, values):
        self.command(OP_SENSOR, *sensor_args(**values))

    def _write_page(self, data):
        for attempt in range(PAGE_RETRIES + 1):
            try:
                self._write(data)
                return attempt
            except OSError as e:
                if attempt == PAGE_RETRIES:
                    raise UploadError(f'page write failed after {PAGE_RETRIES} retries ({e}). {RETRY_HINT}') from e
                self.sleep(PAGE_RETRY_S)

    def upload(self, kind, blob, delay_ms=0, show=True, save=False):
        frames = validate_upload(kind, blob)
        addr, storage, target = UPLOAD_KINDS[kind]
        if kind != 'gif':
            delay_ms = 0
        before = self.state()
        if kind == 'gif' and show:
            self.set_mode(MODE_GIF)                     # GIF is selected before upload
            self.sleep(0.5)
        size = len(blob)
        plist = pages(blob)
        self.log(f'upload {kind}: {size} bytes, {len(plist)} pages, erase ~{erase_wait_s(size):.1f}s')
        try:
            self.sleep(2.0)
            self.command(OP_UPLOAD_MARK, 1)
            self.sleep(0.5)
            self._write(header_packet(addr, size, storage, frames, delay_ms))
            self.sleep(erase_wait_s(size))
            self.sleep(1.0)
            retries = 0
            half = len(plist) // 2
            for i, page in enumerate(plist):
                retries += self._write_page(page)
                if PAGE_GAP_S:
                    self.sleep(PAGE_GAP_S)
                if i + 1 == half:
                    self.log(f'  {i + 1}/{len(plist)} pages')
            self.sleep(0.5)
            self.command(OP_UPLOAD_MARK, 2)
            self.sleep(0.5)
        except UploadError:
            raise
        except OSError as e:
            raise UploadError(f'I2C write failed during upload ({e}). {RETRY_HINT}') from e
        self.log(f'  {len(plist)}/{len(plist)} pages sent, retries={retries}')
        if show:
            if before['on'] is False:
                self.set_power(True)
                self.sleep(0.5)
            if kind == 'gif' or before['mode'] == target:
                self.set_mode(BOUNCE_MODE)              # pass through another mode to force redraw
                self.sleep(1.0)
            self.set_mode(target)
            self.sleep(0.5)
        if save:
            self.save()
        return retries



# ---------------------------------------------------------------- feed (experimental)
NVSMI_FIELDS = 'temperature.gpu,clocks.gr,utilization.gpu,fan.speed,clocks.mem,memory.used,memory.total,power.draw'


def parse_nvsmi(line):
    def num(s):
        try:
            return float(s.strip())
        except ValueError:
            return 0.0
    p = [num(x) for x in line.split(',')]
    if len(p) != 8:
        raise LcdError(f'unexpected nvidia-smi output {line!r}')
    temp, clock, usage, fan, mclock, used, total, power = p
    return {'temp': round(temp), 'clock': round(clock), 'usage': round(usage), 'fan': round(fan),
            'vram_clock': round(mclock), 'vram': round(100 * used / total) if total else 0,
            'fps': 0, 'power': round(power)}


def read_sensors(run=subprocess.run):
    exe = shutil.which('nvidia-smi') or '/usr/bin/nvidia-smi'
    try:
        r = run([exe, f'--query-gpu={NVSMI_FIELDS}', '--format=csv,noheader,nounits'],
                capture_output=True, text=True, timeout=10, check=True)
        return parse_nvsmi(r.stdout.strip().splitlines()[0])
    except (OSError, IndexError, subprocess.SubprocessError) as e:
        raise LcdError(f'nvidia-smi failed: {e}') from e


# ---------------------------------------------------------------- selftest (no I2C)
def selftest():
    errors = []

    def check(name, cond):
        print(('ok   ' if cond else 'FAIL ') + name)
        if not cond:
            errors.append(name)
    check('F1 header', upload_header(0x01F25800, IMAGE_SIZE, 1).hex(' ')
          == 'f1 cb 55 ac 38 01 f2 58 00 01 00 00 01 aa 00 00 00 02 00')
    img = build_image_container(bytes(FRAME_BYTES))
    check('image container head', img[:12].hex(' ') == '01 00 0b a9 01 00 40 01 aa 00 01 00')
    check('image validates', validate_upload('image', img) == 0)
    px = bytes(range(256)) * (FRAME_BYTES // 256) + bytes(FRAME_BYTES % 256)
    check('RLE round trip', rle_decode(rle_encode(px)) == px)
    check('packet layout', packet(OP_MODE, 4)[:7].hex(' ') == 'e5 cb 55 ac 38 04 00')
    try:
        bus = find_bus()
        print(f'ok   bus {bus} (address 0x{I2C_ADDR:02x}; not opened by selftest)')
    except ProbeError as e:
        print(f'FAIL bus: {e}')
        errors.append('bus')
    return 0 if not errors else 1


# ---------------------------------------------------------------- CLI
def _print_status(st, as_json=False):
    if as_json:
        print(json.dumps(st, ensure_ascii=False))
        return
    ov = st['overlay']
    print(f"firmware {st['firmware']}")
    print(f"mode     {st['mode']} ({st['mode_name']})  raw {st['raw']}")
    print(f"power    {'on' if st['on'] else 'off' if st['on'] is False else 'unknown'}")
    print(f"overlay  {' '.join(ov['widgets']) or 'none'}  interval {ov['interval']}s  raw {ov['raw']}")
    print(f"upload   {'idle' if st['upload_state']['idle'] else 'busy'}  raw {st['upload_state']['raw']}")


def _readback(dev):
    st, ov = dev.state(), dev.overlay()
    print(f"readback DE {st['raw']} -> mode {st['mode']} ({st['mode_name']}), "
          f"{'on' if st['on'] else 'off' if st['on'] is False else 'unknown'}; "
          f"DF {ov['raw']} -> {' '.join(ov['widgets']) or 'none'} / {ov['interval']}s")


def _read_blob(src):
    limit = GIF_MAX_BYTES + 1
    if src == '-':
        data = sys.stdin.buffer.read(limit)
    else:
        with open(src, 'rb') as fh:
            data = fh.read(limit)
    if len(data) >= limit:
        raise LcdError(f'input exceeds {GIF_MAX_BYTES} bytes')
    return data


def build_parser():
    p = argparse.ArgumentParser(prog='aorus-lcd', description='RTX 3090 AORUS XTREME LCD control (i2c 0x61).')
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('status')
    s.add_argument('--json', action='store_true')
    sub.add_parser('on')
    sub.add_parser('off')
    s = sub.add_parser('mode')
    s.add_argument('mode')
    s = sub.add_parser('overlay')
    s.add_argument('widgets', nargs='+', help=f'{" ".join(WIDGETS)} | none')
    s.add_argument('--interval', type=int)
    s = sub.add_parser('loop')
    s.add_argument('modes', nargs='+')
    s.add_argument('--interval', type=int, default=5)
    sub.add_parser('save')
    s = sub.add_parser('upload')
    s.add_argument('--kind', required=True, choices=sorted(UPLOAD_KINDS))
    s.add_argument('--delay-ms', type=int, default=100)
    s.add_argument('--no-show', action='store_true')
    s.add_argument('--save', action='store_true')
    s.add_argument('file')
    s = sub.add_parser('feed', help='(experimental) send nvidia-smi values with E3')
    s.add_argument('--once', action='store_true')
    s.add_argument('--period', type=float, default=5.0, help=argparse.SUPPRESS)
    sub.add_parser('selftest')
    return p


@contextlib.contextmanager
def open_device(lock_path=LOCK_PATH):
    bus = find_bus()
    with device_lock(lock_path), I2CTransport(bus) as t:
        dev = Device(t)
        dev.probe()
        yield dev


def run(args, opener=open_device):
    if args.cmd == 'selftest':
        return selftest()
    if args.cmd == 'overlay':
        names = [w.lower() for w in args.widgets]
        if names != ['none'] and any(w not in WIDGETS for w in names):
            raise LcdError(f'widgets must be from {", ".join(WIDGETS)} or none')
        args.widgets = [] if names == ['none'] else names
    if args.cmd == 'mode':
        args.mode = parse_mode(args.mode)
    if args.cmd == 'loop':
        args.modes = [parse_mode(m) for m in args.modes]
        loop_args(args.modes, args.interval)
    if args.cmd == 'upload':
        blob = _read_blob(args.file)
        validate_upload(args.kind, blob)         # fail before taking the lock / touching the bus
        if not 1 <= args.delay_ms <= 255 and args.kind == 'gif':
            raise LcdError('--delay-ms must be 1..255 (firmware field is one byte)')
    if args.cmd == 'feed':
        return _feed(args, opener)

    with opener() as dev:
        if args.cmd == 'status':
            _print_status(dev.status(), args.json)
            return 0
        if args.cmd in ('on', 'off'):
            dev.set_power(args.cmd == 'on')
        elif args.cmd == 'mode':
            dev.set_mode(args.mode)
        elif args.cmd == 'overlay':
            interval = args.interval if args.interval is not None else (dev.overlay()['interval'] or 5)
            dev.set_overlay(args.widgets, interval)
        elif args.cmd == 'loop':
            dev.set_loop(args.modes, args.interval)
            dev.sleep(0.3)
            dev.set_mode(MODE_CAROUSEL)
        elif args.cmd == 'save':
            dev.save()
        elif args.cmd == 'upload':
            dev.upload(args.kind, blob, args.delay_ms, show=not args.no_show, save=args.save)
            print(f"upload done; D7 {dev.upload_state()['raw']}")
        dev.sleep(0.3)
        _readback(dev)
    return 0


def _feed(args, opener):
    last = None
    while True:
        values = read_sensors()
        if values != last:
            with opener() as dev:
                dev.feed(values)
            print('feed ' + ' '.join(f'{k}={v}' for k, v in values.items()), flush=True)
            last = values
        if args.once:
            return 0
        time.sleep(args.period)


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except LcdError as e:
        print(f'aorus-lcd: {e}', file=sys.stderr)
        return e.code
    except OSError as e:
        print(f'aorus-lcd: I/O error: {e}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
