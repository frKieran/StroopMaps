"""Unit tests of the ROM reader on synthetic data (no ROM file, no savestate).

    python -m unittest discover -s tests -p "test_rom_unit*.py"
"""
import os, struct, sys, unittest


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import geolayout, romcol, romload      # noqa: E402


def mio0_encode_literal(data):
    """A valid MIO0 block that stores every byte as a literal (no back references)."""
    n = len(data)
    flags = bytearray()
    for i in range(0, n, 32):
        k = min(32, n - i)
        w = ((1 << k) - 1) << (32 - k)
        flags += struct.pack('>I', w)
    comp = b''
    head = 16
    co = head + len(flags)
    uo = co + len(comp)
    return b'MIO0' + struct.pack('>III', n, co, uo) + bytes(flags) + comp + data


class FakeRam:
    def __init__(self, blocks):
        self.blocks = blocks                     # {base: bytes}
        self.seg = [None] * 32

    def va(self, a):
        return a if a >= 0x80000000 else self.seg[(a >> 24) & 0x1F] + (a & 0xFFFFFF)

    def raw(self, a, n):
        for base, d in self.blocks.items():
            if base <= a < base + len(d):
                out = d[a - base:a - base + n]
                return out + bytes(n - len(out))
        raise LookupError(hex(a))

    def u32(self, a): return struct.unpack('>I', self.raw(a, 4))[0]
    def s16(self, a): return struct.unpack('>h', self.raw(a, 2))[0]


class Mio0Test(unittest.TestCase):
    def test_literals(self):
        data = bytes(range(200)) * 3
        self.assertEqual(romload.mio0_decode(mio0_encode_literal(data)), data)

    def test_back_reference(self):
        # 4 literal bytes "abcd" then a copy of length 8 from distance 4 -> "abcdabcdabcd"
        flags = struct.pack('>I', 0xF0000000)
        comp = struct.pack('>H', ((8 - 3) << 12) | (4 - 1))
        blk = b'MIO0' + struct.pack('>III', 12, 16 + 4, 16 + 4 + 2) + flags + comp + b'abcd'
        self.assertEqual(romload.mio0_decode(blk), b'abcdabcdabcd')


class TerrainTest(unittest.TestCase):
    def make(self):
        v = [(0, 0, 0), (0, 0, 100), (100, 0, 0), (100, 100, 0)]
        d = struct.pack('>hh', 0x40, len(v)) + b''.join(struct.pack('>hhh', *p) for p in v)
        d += struct.pack('>hh', 0x00, 2) + struct.pack('>hhh', 0, 1, 2) + struct.pack('>hhh', 2, 1, 0)   # type 0: floor, ceil
        d += struct.pack('>hh', 0x04, 1) + struct.pack('>hhhh', 0, 2, 3, 7)                                   # force type 4
        d += struct.pack('>h', 0x41) + struct.pack('>hh', 0x43, 1) + struct.pack('>hhhh', 1, 5, 6, 7)       # a special
        d += struct.pack('>hh', 0x05, 1) + struct.pack('>hhh', 0, 1, 2) + struct.pack('>h', 0x42)           # after CONTINUE
        return FakeRam({0x80100000: d})

    def test_parse(self):
        tris, info = romcol.parse_terrain(self.make(), 0x80100000, 0)
        kinds = [(t[0], t[1]) for t in tris]
        self.assertEqual(kinds[0], ('floor', 0))
        self.assertEqual(kinds[1], ('ceil', 0))
        self.assertEqual(len(tris), 4)                          # the group after CONTINUE and the special is read
        self.assertEqual(len(info.special), 1)
        self.assertEqual(info.special[0], (1, 5, 6, 7))

    def test_elb_shift(self):
        v = [(0, 0, 0), (0, 0, 407), (407, 0, 0)]
        d = struct.pack('>hh', 0x40, 3) + b''.join(struct.pack('>hhh', *p) for p in v)
        d += struct.pack('>hh', 0, 1) + struct.pack('>hhh', 0, 1, 2) + struct.pack('>h', 0x42)
        tris, _ = romcol.parse_terrain(FakeRam({0x80100000: d}), 0x80100000, 2)
        self.assertEqual(tris[0][2][1].tolist(), [0, 0, 404])      # (407 >> 2) << 2

    def test_object_rotation(self):
        # a flat 100x100 square with its corner at the origin, object yawed 90 degrees at (1000, 0, 2000)
        v = [(0, 0, 0), (0, 0, 100), (100, 0, 100), (100, 0, 0)]
        d = struct.pack('>hh', 0x40, 4) + b''.join(struct.pack('>hhh', *p) for p in v)
        d += struct.pack('>hh', 0, 2) + struct.pack('>hhh', 0, 1, 2) + struct.pack('>hhh', 0, 2, 3) + struct.pack('>h', 0x41)
        ram = FakeRam({0x80100000: d})
        tris = romcol.object_tris(ram, 0x80100000, (1000, 0, 2000), (0, 0x4000, 0), (1, 1, 1), 0)
        xs = sorted({int(p[0]) for t in tris for p in t[2]})
        zs = sorted({int(p[2]) for t in tris for p in t[2]})
        self.assertEqual((xs, zs), ([1000, 1100], [1900, 2000]))   # +Z rotates to +X under yaw 0x4000


class GeoTest(unittest.TestCase):
    def test_tree_and_walk(self):
        # START, OPEN, TRANSLATION(layer 1, 10 0 0, dl 0x80200000), DL(layer 0, 0x80200008), CLOSE, END
        g = (struct.pack('>BBH', 0x0B, 0, 0) + struct.pack('>I', 0x04000000)[:0] +
             struct.pack('>BBH', 0x04, 0, 0) +
             struct.pack('>BBhhh', 0x11, 0x81, 10, 0, 0) + struct.pack('>I', 0x80200000) +
             struct.pack('>BBH', 0x15, 0, 0) + struct.pack('>I', 0x80200008) +
             struct.pack('>BBH', 0x05, 0, 0) + struct.pack('>BBH', 0x01, 0, 0))
        ram = FakeRam({0x80100000: g})
        root, parser = geolayout.parse_geo(ram, 0x80100000)
        self.assertEqual(root.typ, geolayout.T_START)
        self.assertEqual([k.typ for k in root.kids], [geolayout.T_TRANS, geolayout.T_DL])
        self.assertEqual(root.kids[0].layer, 1)
        self.assertEqual(root.kids[0].t, (10, 0, 0))
        self.assertEqual(root.kids[1].dl, 0x80200008)
        self.assertEqual(parser.notes, [])

    def test_branch_and_return(self):
        # BRANCH_AND_LINK to a sub layout holding one DL node, then END
        main = struct.pack('>BBH', 0x0B, 0, 0) + struct.pack('>BBH', 0x04, 0, 0) + \
            struct.pack('>BBHI', 0x00, 0, 0, 0x80100040) + struct.pack('>BBH', 0x05, 0, 0) + struct.pack('>BBH', 0x01, 0, 0)
        main = main.ljust(0x40, b'\0') + struct.pack('>BBHI', 0x15, 0, 0, 0x80300000) + struct.pack('>BBH', 0x03, 0, 0)
        root, parser = geolayout.parse_geo(FakeRam({0x80100000: main}), 0x80100000)
        self.assertEqual([k.dl for k in root.kids], [0x80300000])

    def test_angle(self):
        self.assertEqual(geolayout._angle(90), 0x4000)
        self.assertEqual(geolayout._angle(-90), -0x4000)


class ScriptTest(unittest.TestCase):
    def test_names_and_signature_tables(self):
        self.assertEqual(len(romload.VANILLA_LEVEL_COURSE), 38)
        self.assertIn(9, romload.VANILLA_SIGNATURE)
        t = bytes([1, 2, 3, 0xFF])
        self.assertEqual(romload.decode_text(t + b'\xff', 0), '123')

    def test_zero_length_command_stops_a_skip(self):
        self.assertEqual(romload._cmd_len(b'\x10\x04', 0x15000000), 4)
        with self.assertRaises(romload.MemError):                 # SKIP / SKIP_IF would never advance past it
            romload._cmd_len(b'\x10\x00', 0x15000000)

    def test_clean_name(self):
        self.assertEqual(romload.clean_name(' 6 HAZY MAZE CAVE'), 'Hazy Maze Cave')
        self.assertEqual(romload.clean_name(' 1 BOB-OMB BATTLEFIELD'), 'Bob-omb Battlefield')


if __name__ == '__main__':
    unittest.main()
