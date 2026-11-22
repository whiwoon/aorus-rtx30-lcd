"""Offline tests for aorus_lcd.py and lcd. No VM, SSH, I2C or network access."""
import contextlib
import ctypes
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


A = load('aorus_lcd_t', ROOT / 'aorus_lcd.py')
L = load('lcd_t', ROOT / 'lcd')
HDR_IMAGE = 'f1 cb 55 ac 38 01 f2 58 00 01 00 00 01 aa 00 00 00 02 00'
HEAD_IMAGE = '01 00 0b a9 01 00 40 01 aa 00 01 00'


class FakeTransport:
    """Records every write; answers queries from a table keyed by opcode."""

    def __init__(self, mode_raw=4, on=1, fail_pages=0, probe=b'\xd6\x23\x01\x02'):
        self.writes, self.queries = [], []
        self.answers = {0xD6: probe, 0xDE: bytes([0xDE, mode_raw, on, 0]),
                        0xDF: b'\xdf\x25\x05\x00', 0xD7: b'\xd7\x00\x00\x00'}
        self.fail_pages = fail_pages

    def query(self, data):
        assert len(data) == 256
        self.queries.append(data[0])
        return self.answers[data[0]]

    def write(self, data):
        assert len(data) == 256
        is_cmd = data[1:5] == A.MAGIC and data[0] in (0xE1, 0xE3, 0xE5, 0xE7, 0xF1, 0xF2, 0xF3, 0xAA)
        if not is_cmd and self.fail_pages:
            self.fail_pages -= 1
            raise OSError(5, 'Input/output error')
        self.writes.append(bytes(data))
        if data[0] == 0xE5 and is_cmd:
            self.answers[0xDE] = bytes([0xDE, data[5], self.answers[0xDE][2], 0])


def device(t):
    d = A.Device(t, sleep=lambda s: None, log=lambda m: None)
    return d


def image_blob(fill=0):
    return A.build_image_container(bytes([fill]) * A.FRAME_BYTES)


class Packets(unittest.TestCase):
    def test_packet_layout(self):
        p = A.packet(0xE5, 4)
        self.assertEqual(len(p), 256)
        self.assertEqual(p[:6].hex(' '), 'e5 cb 55 ac 38 04')
        self.assertEqual(p[6:], bytes(250))

    def test_f1_header_image(self):
        self.assertEqual(A.upload_header(0x01F25800, 108812, 1).hex(' '), HDR_IMAGE)
        self.assertEqual(A.header_packet(0x01F25800, 108812, 1)[19:], bytes(237))

    def test_container_head(self):
        blob = image_blob()
        self.assertEqual(len(blob), 108812)
        self.assertEqual(blob[:12].hex(' '), HEAD_IMAGE)

    def test_page_count_and_erase(self):
        self.assertEqual(A.page_count(108812), 426)
        self.assertEqual(A.page_count(256), 2)
        self.assertEqual(A.page_count(512), 3)
        self.assertEqual(A.page_count(255), 1)
        self.assertEqual(A.erase_mode(20479), 1)
        self.assertEqual(A.erase_mode(20480), 2)
        self.assertAlmostEqual(A.erase_wait_s(20479), 5 * 0.4)
        self.assertAlmostEqual(A.erase_wait_s(20480), 2.0)
        self.assertAlmostEqual(A.erase_wait_s(4096), 0.4)
        self.assertAlmostEqual(A.erase_wait_s(4097), 0.8)
        self.assertAlmostEqual(A.erase_wait_s(65536), 2.0)
        self.assertAlmostEqual(A.erase_wait_s(65537), 4.0)
        self.assertAlmostEqual(A.erase_wait_s(108812), 4.0)
        pages = A.pages(bytes(512))
        self.assertEqual(len(pages), 3)
        self.assertEqual(pages[-1], bytes(256))

    def test_modes(self):
        self.assertEqual([A.mode_arg(m) for m in range(8)], [1, 2, 3, 4, 5, 6, 7, 10])
        self.assertEqual(A.parse_mode('carousel'), 7)
        self.assertEqual(A.parse_mode('IMAGE'), 3)
        self.assertEqual(A.parse_mode('5'), 5)
        for bad in ('8', 'x', '-1'):
            with self.assertRaises(A.LcdError):
                A.parse_mode(bad)

    def test_overlay_loop_sensor_args(self):
        self.assertEqual(A.overlay_args(['temp', 'usage', 'vram'], 5), [1, 0, 1, 0, 0, 1, 0, 0, 5])
        self.assertEqual(A.loop_args([0, 3, 6], 10), [10, 1, 4, 7])
        with self.assertRaises(A.LcdError):
            A.loop_args([7], 5)
        self.assertEqual(A.sensor_args(temp=45, clock=1695, usage=99, fan=30, vram_clock=9751,
                                       vram=96, fps=0, power=350),
                         [45, 0x06, 0x9F, 99, 0, 30, 0x26, 0x17, 96, 0, 0, 0x01, 0x5E])

    def test_responses(self):
        self.assertEqual(A.parse_firmware(bytes.fromhex('d6230102')), 'F2.3')
        st = A.parse_state(bytes.fromhex('de040100'))
        self.assertEqual((st['mode'], st['mode_name'], st['on']), (3, 'image', True))
        st = A.parse_state(bytes.fromhex('de0a0200'))
        self.assertEqual((st['mode'], st['mode_name'], st['on']), (7, 'carousel', False))
        ov = A.parse_overlay(bytes.fromhex('df250500'))
        self.assertEqual((ov['widgets'], ov['interval']), (['temp', 'usage', 'vram'], 5))
        self.assertTrue(A.parse_upload_state(bytes.fromhex('d7000000'))['idle'])
        with self.assertRaises(A.ProbeError):
            A.parse_firmware(bytes.fromhex('ffffffff'))


class Rle(unittest.TestCase):
    def roundtrip(self, px):
        enc = A.rle_encode(px)
        self.assertEqual(A.rle_decode(enc, len(px) // 2), px)
        return enc

    def test_random(self):
        rnd = random.Random(7)
        px = bytes(rnd.randrange(256) for _ in range(A.FRAME_BYTES))
        self.roundtrip(px)

    def test_solid(self):
        enc = self.roundtrip(b'\x1f\xf8' * A.FRAME_PIXELS)
        # 54400 = 32767 + 21633: one run per window, never crossing it
        self.assertEqual(enc.hex(), 'ffff1ff8' + (21633 | 0x8000).to_bytes(2, 'little').hex() + '1ff8')

    def test_stripes_and_short_repeats(self):
        row = b''.join((i // 8 % 3).to_bytes(2, 'little') for i in range(A.W))
        self.roundtrip(row * A.H)
        px = b'\x01\x00\x01\x00\x02\x00\x02\x00\x02\x00\x03\x00'
        self.assertEqual(A.rle_encode(px).hex(' '), '02 00 01 00 01 00 03 80 02 00 01 00 03 00')

    def test_window_boundary(self):
        rnd = random.Random(3)
        n = A.RLE_WINDOW + 5
        vals = [rnd.randrange(1, 65535) | 1 for _ in range(n)]
        for k in range(1, n):                      # no repeats at all
            if vals[k] == vals[k - 1]:
                vals[k] ^= 2
        vals[A.RLE_WINDOW - 2:A.RLE_WINDOW + 3] = [9] * 5   # repeat straddling window end
        px = b''.join(v.to_bytes(2, 'little') for v in vals)
        enc = self.roundtrip(px)
        # Window 1 has only a 2-pixel repeat at its end: one literal of the whole window.
        self.assertEqual(int.from_bytes(enc[:2], 'little'), A.RLE_WINDOW)
        # Window 2 starts with the other 3 equal pixels: a run that did not cross the boundary.
        k = 2 + 2 * A.RLE_WINDOW
        self.assertEqual(enc[k:k + 4].hex(' '), '03 80 09 00')

    def test_short_tail_window_literal(self):
        px = b'\x05\x00' * (A.RLE_WINDOW + 3)      # last window has 3 pixels (< 4) -> literal
        enc = self.roundtrip(px)
        self.assertEqual(enc[4:].hex(' '), '03 00 05 00 05 00 05 00')

    def test_decode_rejects_garbage(self):
        for bad in (b'\x01', b'\x05\x00\x01\x00', b'\x00\x00', b'\xff\xff\x00\x00\xff\xff\x00\x00'):
            with self.assertRaises(A.LcdError):
                A.rle_decode(bad, 10)


class Validation(unittest.TestCase):
    def test_image_ok(self):
        self.assertEqual(A.validate_upload('image', image_blob()), 0)
        self.assertEqual(A.validate_upload('text', image_blob()), 0)

    def test_image_rejects(self):
        good = image_blob()
        bad_dims = bytearray(good)
        bad_dims[6:8] = (321).to_bytes(2, 'little')
        bad_fmt = bytearray(good)
        bad_fmt[10:12] = (3).to_bytes(2, 'little')
        for blob in (good[:-1], good + b'\0', bytes(bad_dims), bytes(bad_fmt), b''):
            with self.assertRaises(A.LcdError):
                A.validate_upload('image', blob)
        with self.assertRaises(A.LcdError):
            A.validate_upload('gif', good)          # raw frame is not a GIF frame
        with self.assertRaises(A.LcdError):
            A.validate_upload('other', good)

    def test_gif(self):
        frames = [bytes([i]) * A.FRAME_BYTES for i in range(3)]
        blob = A.build_gif_container(frames)
        self.assertEqual(A.validate_upload('gif', blob), 3)
        f = A.parse_container(blob)
        self.assertEqual([x.fmt for x in f], [3, 3, 3])
        with self.assertRaises(A.LcdError):
            A.validate_upload('image', blob)
        with self.assertRaises(A.LcdError):
            A.validate_upload('gif', blob[:-1])
        with patch.object(A, 'GIF_MAX_BYTES', len(blob) - 1):
            with self.assertRaises(A.LcdError):
                A.validate_upload('gif', blob)
        many = A.build_container([b'\x00\x80\x00\x00'] * 256, A.FMT_RLE)
        with self.assertRaises(A.LcdError):
            A.validate_upload('gif', many)


class DeviceFlow(unittest.TestCase):
    def test_upload_sequence(self):
        t = FakeTransport(mode_raw=1)                # currently faith1
        d = device(t)
        d.probe()
        self.assertEqual(d.upload('image', image_blob()), 0)
        w = t.writes
        self.assertEqual(w[0][:6].hex(' '), 'f2 cb 55 ac 38 01')
        self.assertEqual(w[1][:19].hex(' '), HDR_IMAGE)
        self.assertEqual(len(w), 1 + 1 + 426 + 1 + 1)
        payload = b''.join(w[2:428])
        self.assertEqual(payload[:108812], image_blob())
        self.assertEqual(payload[108812:], bytes(426 * 256 - 108812))
        self.assertEqual(w[428][:6].hex(' '), 'f2 cb 55 ac 38 02')
        self.assertEqual(w[429][:6].hex(' '), 'e5 cb 55 ac 38 04')   # show image mode

    def test_same_mode_bounces_and_power_on(self):
        t = FakeTransport(mode_raw=4, on=2)          # already image mode, panel off
        d = device(t)
        d.probe()
        d.upload('image', image_blob(), save=True)
        tail = [x[:6].hex(' ') for x in t.writes[428:]]
        self.assertEqual(tail, ['f2 cb 55 ac 38 02', 'e7 cb 55 ac 38 01', 'e5 cb 55 ac 38 01',
                                'e5 cb 55 ac 38 04', 'aa cb 55 ac 38 00'])

    def test_no_show_no_save(self):
        t = FakeTransport()
        d = device(t)
        d.probe()
        d.upload('image', image_blob(), show=False)
        self.assertEqual(t.writes[-1][:6].hex(' '), 'f2 cb 55 ac 38 02')
        self.assertNotIn(0xAA, [x[0] for x in t.writes if x[1:5] == A.MAGIC])

    def test_gif_selects_mode_first(self):
        t = FakeTransport()
        d = device(t)
        d.probe()
        blob = A.build_gif_container([bytes(A.FRAME_BYTES)] * 2)
        d.upload('gif', blob, delay_ms=80)
        self.assertEqual(t.writes[0][:6].hex(' '), 'e5 cb 55 ac 38 06')
        h = A.upload_header(0, len(blob), 2, 2, 80)
        self.assertEqual(t.writes[2][:19], h)
        self.assertEqual(h[5:10].hex(' '), '00 00 00 00 02')
        self.assertEqual(h[14:17].hex(' '), '00 02 50')
        self.assertEqual(t.writes[-1][:6].hex(' '), 'e5 cb 55 ac 38 06')

    def test_retry_then_success(self):
        t = FakeTransport(fail_pages=3)
        sleeps = []
        d = A.Device(t, sleep=sleeps.append, log=lambda m: None)
        d.probe()
        self.assertEqual(d.upload('image', image_blob(), show=False), 3)
        self.assertEqual(sleeps.count(0.25), 3)
        self.assertEqual(len(t.writes), 429)

    def test_retry_exhausted(self):
        t = FakeTransport(fail_pages=9)
        d = device(t)
        d.probe()
        with self.assertRaises(A.UploadError) as cm:
            d.upload('image', image_blob())
        self.assertIn('다시 업로드', str(cm.exception))
        self.assertEqual(cm.exception.code, 3)
        self.assertNotIn('f2 cb 55 ac 38 02', [x[:6].hex(' ') for x in t.writes])

    def test_probe_failure_means_no_writes(self):
        for bad in (b'\xff\xff\xff\xff', b'\x00\x00\x00\x00'):
            t = FakeTransport(probe=bad)
            d = device(t)
            with self.assertRaises(A.ProbeError):
                d.probe()
            for call in (lambda: d.set_power(True), lambda: d.set_mode(3), d.save,
                         lambda: d.upload('image', image_blob())):
                with self.assertRaises(A.ProbeError):
                    call()
            self.assertEqual(t.writes, [])

    def test_invalid_container_no_writes(self):
        t = FakeTransport()
        d = device(t)
        d.probe()
        with self.assertRaises(A.LcdError):
            d.upload('image', image_blob()[:-2])
        self.assertEqual(t.writes, [])


class Transport(unittest.TestCase):
    def test_only_address_0x61_and_4_byte_reads(self):
        seen = []

        def ioctl(fd, req, data):
            self.assertEqual(req, A.I2C_RDWR)
            for i in range(data.nmsgs):
                m = data.msgs[i]
                seen.append((m.addr, m.flags, m.len))
                if m.flags & A.I2C_M_RD:
                    for k, b in enumerate(b'\xd6\x23\x01\x02'):
                        m.buf[k] = b
        t = A.I2CTransport('/dev/null', ioctl=ioctl)
        with t:
            d = A.Device(t, sleep=lambda s: None, log=lambda m: None)
            self.assertEqual(d.probe(), 'F2.3')
            d.set_mode(3)
        self.assertEqual(seen, [(0x61, 0, 256), (0x61, 1, 4), (0x61, 0, 256)])

    def test_source_has_no_other_address(self):
        src = (ROOT / 'aorus_lcd.py').read_text()
        self.assertEqual(src.count('_Msg(I2C_ADDR'), 2)
        self.assertNotIn('I2C_SLAVE', src)
        self.assertNotIn('addr=', src)

    def test_find_bus(self):
        with tempfile.TemporaryDirectory() as tmp:
            def adapter(n, name):
                Path(tmp, f'i2c-{n}').mkdir()
                Path(tmp, f'i2c-{n}', 'name').write_text(name + '\n')
            pattern = os.path.join(tmp, 'i2c-*', 'name')
            with self.assertRaises(A.ProbeError):
                A.find_bus(pattern)
            adapter(0, 'SMBus I801 adapter')
            adapter(1, 'NVIDIA i2c adapter 1 at 1:00.0')
            adapter(3, 'NVIDIA i2c adapter 3 at 1:00.0')
            self.assertEqual(A.find_bus(pattern), '/dev/i2c-1')
            adapter(9, 'NVIDIA i2c adapter 1 at 2:00.0')
            with self.assertRaises(A.ProbeError):
                A.find_bus(pattern)

    def test_lock_excludes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'lock')
            with A.device_lock(path):
                with self.assertRaises(A.LcdError):
                    with A.device_lock(path, timeout=0, sleep=lambda s: None):
                        pass
            with A.device_lock(path, timeout=0):
                pass


class Cli(unittest.TestCase):
    def run_cli(self, argv, t):
        @contextlib.contextmanager
        def opener():
            d = device(t)
            d.probe()
            yield d
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = A.run(A.build_parser().parse_args(argv), opener=opener)
        return rc, out.getvalue()

    def test_status_and_writes_with_readback(self):
        t = FakeTransport()
        rc, out = self.run_cli(['status'], t)
        self.assertEqual(rc, 0)
        self.assertIn('F2.3', out)
        self.assertIn('temp usage vram', out)
        self.assertEqual(t.writes, [])
        rc, out = self.run_cli(['mode', 'gif'], t)
        self.assertEqual(t.writes[-1][:6].hex(' '), 'e5 cb 55 ac 38 06')
        self.assertIn('readback DE de 06', out)
        self.run_cli(['overlay', 'none'], t)
        self.assertEqual(t.writes[-1][:14].hex(' '), 'e1 cb 55 ac 38 00 00 00 00 00 00 00 00 05')
        self.run_cli(['loop', 'faith1', 'image', '--interval', '7'], t)
        self.assertEqual([x[:8].hex(' ') for x in t.writes[-2:]],
                         ['f3 cb 55 ac 38 07 01 04', 'e5 cb 55 ac 38 0a 00 00'])
        self.assertNotIn(0xAA, [x[0] for x in t.writes])

    def test_upload_validates_before_opening(self):
        with tempfile.NamedTemporaryFile() as f:
            f.write(b'junk')
            f.flush()
            with self.assertRaises(A.LcdError):
                A.run(A.build_parser().parse_args(['upload', '--kind', 'image', f.name]),
                      opener=lambda: self.fail('device opened'))

    def test_nvsmi(self):
        v = A.parse_nvsmi('45, 1695, 99, 30, 9751, 23000, 24576, 350.12')
        self.assertEqual(v, {'temp': 45, 'clock': 1695, 'usage': 99, 'fan': 30, 'vram_clock': 9751,
                             'vram': 94, 'fps': 0, 'power': 350})
        self.assertEqual(A.parse_nvsmi('45, 1695, 99, [N/A], 9751, 1, 0, 350')['fan'], 0)


class Master(unittest.TestCase):
    def test_rgb565(self):
        img = L.Image.new('RGB', (L.W, L.H), (255, 0, 0))
        img.putpixel((1, 0), (0, 255, 0))
        img.putpixel((2, 0), (0, 0, 255))
        img.putpixel((3, 0), (255, 255, 255))
        px = L.to_rgb565(img)
        self.assertEqual(len(px), A.FRAME_BYTES)
        self.assertEqual(px[:8].hex(' '), '00 f8 e0 07 1f 00 ff ff')

    def test_text_hangul_and_fit(self):
        img = L.render_text('안녕하세요\\nLLM VM 온도 45°C')
        self.assertEqual(img.size, (320, 170))
        self.assertGreater(len(set(img.getdata())), 1)
        L.render_text('x' * 200)                  # very long text still fits (small size)
        L.render_text('작게', size=12, fg=(255, 0, 0), bg=(0, 0, 40))

    def test_image_fit(self):
        src = L.Image.new('RGBA', (1000, 200), (0, 0, 255, 128))
        for mode in ('cover', 'contain'):
            self.assertEqual(L.fit(src, mode, (0, 0, 0)).size, (320, 170))
        c = L.fit(L.Image.new('RGB', (100, 100), (255, 255, 255)), 'contain', (10, 20, 30))
        self.assertEqual(c.getpixel((0, 0)), (10, 20, 30))

    def test_dry_run_writes_preview_without_remote(self):
        def no_run(*a, **k):
            raise AssertionError('remote called')
        with tempfile.TemporaryDirectory() as tmp:
            png, binf = os.path.join(tmp, 'p.png'), os.path.join(tmp, 'c.bin')
            with contextlib.redirect_stderr(io.StringIO()):
                rc = L.main(['text', '테스트', '--dry-run', '--preview', png, '--container', binf], run=no_run)
            self.assertEqual(rc, 0)
            with L.Image.open(png) as im:
                self.assertEqual(im.size, (320, 170))
            blob = Path(binf).read_bytes()
            self.assertEqual(blob[:12].hex(' '), HEAD_IMAGE)
            self.assertEqual(A.validate_upload('image', blob), 0)
            gif = os.path.join(tmp, 'a.gif')
            fr = [L.Image.new('RGB', (64, 48), c) for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
            fr[0].save(gif, save_all=True, append_images=fr[1:], duration=50, loop=0)
            with contextlib.redirect_stderr(io.StringIO()):
                rc = L.main(['gif', gif, '--dry-run', '--container', binf, '--max-frames', '2'], run=no_run)
            self.assertEqual(rc, 0)
            self.assertEqual(A.validate_upload('gif', Path(binf).read_bytes()), 2)

    def test_remote_command_lines(self):
        calls = []

        def fake(cmd, input=None):
            calls.append((cmd, input))

            class R:
                returncode = 0
            return R()
        old, L.PREFIX = L.PREFIX, ['ssh', 'root@gpu-host']
        self.addCleanup(setattr, L, 'PREFIX', old)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(L.main(['text', '$(reboot)', '--save'], run=fake), 0)
        cmd, data = calls[-1]
        self.assertEqual(cmd[:2], ['ssh', 'root@gpu-host'])
        self.assertEqual(cmd[2:], ['aorus-lcd', 'upload', '--kind', 'image', '--save', '-'])
        self.assertEqual(len(data), 108812)
        self.assertNotIn('reboot', ' '.join(cmd))
        self.assertEqual(L.main(['mode', 'image'], run=fake), 0)
        self.assertEqual(calls[-1][0][2:], ['aorus-lcd', 'mode', 'image'])
        L.PREFIX = []
        self.assertEqual(L.main(['mode', 'image'], run=fake), 0)
        self.assertEqual(calls[-1][0], ['aorus-lcd', 'mode', 'image'])
        L.PREFIX = ['ssh', 'root@gpu-host']
        self.assertEqual(L.main(['overlay', 'temp', 'power', '--interval', '5'], run=fake), 0)
        n = len(calls)
        with contextlib.redirect_stderr(io.StringIO()):
            for bad in (['mode', 'image;reboot'], ['status', '$(id)'], ['overlay', 'a b'], ['loop', '--x=1']):
                self.assertEqual(L.main(bad, run=fake), 1)
        self.assertEqual(len(calls), n)


if __name__ == '__main__':
    unittest.main()
