"""Tests of geodecode.py on synthetic data: the F3D / F3DEX / F3DEX2 interpreters on made-up display lists, microcode
detection, the graph node numbering of rebuilt (decomp-built) hacks, and the object rule and its modes.

    python -m unittest discover -s tests -p "test_geodecode*.py"
"""
import os, struct, sys, unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import geodecode as g                                  # noqa: E402


# ---------------------------------------------------------------- a small fake RDRAM
class FakeRam:
    """The geodecode.Ram interface over a dict of byte blocks (KSEG0 addresses)."""

    def __init__(self):
        self.blocks = {}
        self.seg = [0] * 32
        self.layout = dict(g.LAYOUT_US)

    def put(self, addr, data):
        self.blocks[addr] = bytes(data)

    def va(self, a):
        return a if a >= 0x80000000 else 0x80000000 | (self.seg[(a >> 24) & 0x1F] + (a & 0xFFFFFF))

    def raw(self, a, n):
        for base, d in self.blocks.items():
            if base <= a < base + len(d):
                out = d[a - base:a - base + n]
                return out + bytes(n - len(out))
        return bytes(n)

    def u32(self, a): return struct.unpack('>I', self.raw(a, 4))[0]
    def s16(self, a): return struct.unpack('>h', self.raw(a, 2))[0]
    def f32(self, a): return struct.unpack('>f', self.raw(a, 4))[0]


def vtx(x, y, z, s=0, t=0, rgba=(255, 255, 255, 255)):
    return struct.pack('>hhhHhh', x, y, z, 0, s, t) + bytes(rgba)


def words(*pairs):
    return b''.join(struct.pack('>II', a, b) for a, b in pairs)


DL, VB, MT, LT = 0x80100000, 0x80110000, 0x80120000, 0x80130000


def tri_tris(scene):
    """[[(x, y, z) x 3]] of every triangle in a scene, in batch order."""
    out = []
    for b in scene.values():
        p = np.array(b['pos']).reshape(-1, 3, 3).round(3)
        out += [[tuple(v) for v in t] for t in p]
    return out


# the same shape in the three encodings: a 3x3 fan of 4 vertices per load, with 5 triangles, a quad and a pair
FRONT = [(0, 0, 0), (0, 0, 100), (100, 0, 0), (100, 0, 100), (200, 0, 0), (200, 0, 100)]


def f3d_list():
    ram = FakeRam()
    ram.put(VB, b''.join(vtx(*p) for p in FRONT))
    dl = [(0x04000000 | (5 << 20) | 96, VB),                                         # G_VTX n=6 v0=0
          (0xBF000000, (0 * 10 << 16) | (1 * 10 << 8) | 2 * 10),                      # G_TRI1
          (0xBF000000, (2 * 10 << 16) | (1 * 10 << 8) | 3 * 10),
          (0xBF000000, (2 * 10 << 16) | (3 * 10 << 8) | 4 * 10),
          (0xB8000000, 0)]
    ram.put(DL, words(*dl))
    return ram


def tri2x(a, b, c):
    return ((a * 2) << 16) | ((b * 2) << 8) | (c * 2)


def f3dex_list():
    ram = FakeRam()
    ram.put(VB, b''.join(vtx(*p) for p in FRONT))
    dl = [(0x04000000 | ((0 * 2) << 16) | (6 << 10) | (16 * 6 - 1), VB),             # G_VTX v0 = 0, n = 6
          (0xBF000000, tri2x(0, 1, 2)),                                              # G_TRI1
          (0xB1000000 | tri2x(2, 1, 3), tri2x(2, 3, 4)),                              # G_TRI2
          (0xB8000000, 0)]
    ram.put(DL, words(*dl))
    return ram


def f3dex2_list():
    ram = FakeRam()
    ram.put(VB, b''.join(vtx(*p) for p in FRONT))
    dl = [(0x01000000 | (6 << 12) | ((0 + 6) << 1), VB),                              # G_VTX n = 6, end = v0 + n
          (0x05000000 | tri2x(0, 1, 2), 0),                                          # G_TRI1
          (0x06000000 | tri2x(2, 1, 3), tri2x(2, 3, 4)),                              # G_TRI2
          (0xDF000000, 0)]
    ram.put(DL, words(*dl))
    return ram


class Interpreters(unittest.TestCase):
    EXPECT = [[(0, 0, 0), (0, 0, 100), (100, 0, 0)], [(100, 0, 0), (0, 0, 100), (100, 0, 100)],
              [(100, 0, 0), (100, 0, 100), (200, 0, 0)]]

    def run_dl(self, ram, ucode):
        scene = {}
        gfx = g.Gfx(ram, scene, ucode)
        gfx.run(DL, np.eye(4), 1)
        return gfx, tri_tris(scene)

    def test_same_mesh_in_three_encodings(self):
        for ram, ucode in ((f3d_list(), 'F3D'), (f3dex_list(), 'F3DEX'), (f3dex2_list(), 'F3DEX2')):
            gfx, tris = self.run_dl(ram, ucode)
            self.assertEqual(gfx.unknown, {}, ucode)
            self.assertEqual(tris, self.EXPECT, ucode)

    def test_default_is_f3d(self):
        ram = f3d_list()
        scene = {}
        gfx = g.Gfx(ram, scene)
        self.assertEqual(gfx.ucode, 'F3D')
        gfx.run(DL, np.eye(4), 1)
        self.assertEqual(len(tri_tris(scene)), 3)

    def test_unknown_ucode_rejected(self):
        with self.assertRaises(ValueError):
            g.Gfx(FakeRam(), {}, 'F3DZEX')

    def test_f3d_still_counts_tri2_as_unknown(self):
        ram = f3dex_list()                       # run as F3D: G_TRI2 is not an F3D command
        gfx, _ = self.run_dl(ram, 'F3D')
        self.assertEqual(gfx.unknown.get('TRI2'), 1)

    def test_quad(self):
        for ex2 in (False, True):
            ram = FakeRam()
            ram.put(VB, b''.join(vtx(*p) for p in [(0, 0, 0), (0, 0, 100), (100, 0, 100), (100, 0, 0)]))
            if ex2:
                dl = [(0x01000000 | (4 << 12) | (4 << 1), VB), (0x07000000 | tri2x(0, 1, 2), tri2x(0, 2, 3)),
                      (0xDF000000, 0)]
            else:
                dl = [(0x04000000 | (4 << 10) | 63, VB), (0xB5000000 | tri2x(0, 1, 2), tri2x(0, 2, 3)),
                      (0xB8000000, 0)]
            ram.put(DL, words(*dl))
            gfx, tris = self.run_dl(ram, 'F3DEX2' if ex2 else 'F3DEX')
            self.assertEqual(len(tris), 2)
            self.assertEqual(gfx.unknown, {})

    def test_vertex_buffer_has_64_entries_and_v0_offset(self):
        ram = FakeRam()
        pts = [(0, 0, 0), (0, 0, 100), (100, 0, 0)]
        ram.put(VB, b''.join(vtx(*p) for p in pts))
        # F3DEX2: load three vertices at 61..63, then draw them
        dl2 = [(0x01000000 | (3 << 12) | (64 << 1), VB), (0x05000000 | tri2x(61, 62, 63), 0), (0xDF000000, 0)]
        ram.put(DL, words(*dl2))
        gfx, tris = self.run_dl(ram, 'F3DEX2')
        self.assertEqual(tris, [pts])
        # F3DEX: v0 = 30, n = 3
        dl1 = [(0x04000000 | ((30 * 2) << 16) | (3 << 10) | 47, VB), (0xBF000000, tri2x(30, 31, 32)),
               (0xB8000000, 0)]
        ram.put(DL, words(*dl1))
        gfx, tris = self.run_dl(ram, 'F3DEX')
        self.assertEqual(tris, [pts])

    def test_f3dex2_geometry_mode(self):
        ram = FakeRam()
        pts = [(0, 0, 0), (0, 0, 100), (100, 0, 0)]
        ram.put(VB, b''.join(vtx(*p) for p in pts))
        back = [(0x01000000 | (3 << 12) | (3 << 1), VB), (0x05000000 | tri2x(0, 2, 1), 0)]    # facing down
        # G_CULL_BACK (0x400) is on at the start: the downward face is culled
        ram.put(DL, words(*back, (0xDF000000, 0)))
        gfx, tris = self.run_dl(ram, 'F3DEX2')
        self.assertEqual((tris, gfx.culled), ([], 1))
        # clear G_CULL_BACK: gsSPGeometryMode(G_CULL_BACK, 0) = (~0x400 & 0xFFFFFF, 0)
        ram.put(DL, words((0xD9000000 | (~0x400 & 0xFFFFFF), 0), *back, (0xDF000000, 0)))
        gfx, tris = self.run_dl(ram, 'F3DEX2')
        self.assertEqual((len(tris), gfx.culled), (1, 0))
        # set G_CULL_FRONT (0x200) as well as clearing back: the upward face is culled, the downward one is drawn
        up = [(0x05000000 | tri2x(0, 1, 2), 0)]
        ram.put(DL, words((0xD9000000 | (~0x400 & 0xFFFFFF), 0x200), *back[:1], *up, (0xDF000000, 0)))
        gfx, tris = self.run_dl(ram, 'F3DEX2')
        self.assertEqual((len(tris), gfx.culled), (0, 1))

    def test_geometry_bits_translate(self):
        self.assertEqual(g.geo_ex2_to_f3d(0x200 | 0x400 | 0x200000 | 0x20000 | 0x4), 0x1000 | 0x2000 | 0x200 | 0x20000 | 0x4)

    def test_dl_push_and_branch(self):
        ram = FakeRam()
        pts = [(0, 0, 0), (0, 0, 100), (100, 0, 0)]
        ram.put(VB, b''.join(vtx(*p) for p in pts))
        sub = 0x80140000
        ram.put(sub, words((0x05000000 | tri2x(0, 1, 2), 0), (0xDF000000, 0)))
        ram.put(DL, words((0x01000000 | (3 << 12) | (3 << 1), VB), (0xDE000000, sub),          # call: continues after
                          (0x05000000 | tri2x(0, 1, 2), 0), (0xDE010000, sub),                 # branch: ends this list
                          (0x05000000 | tri2x(0, 1, 2), 0), (0xDF000000, 0)))
        gfx, tris = self.run_dl(ram, 'F3DEX2')
        self.assertEqual(len(tris), 3)

    def test_branch_z_takes_the_near_list(self):
        ram = FakeRam()
        pts = [(0, 0, 0), (0, 0, 100), (100, 0, 0)]
        ram.put(VB, b''.join(vtx(*p) for p in pts))
        near = 0x80140000
        ram.put(near, words((0x05000000 | tri2x(0, 1, 2), 0), (0xDF000000, 0)))
        ram.put(DL, words((0x01000000 | (3 << 12) | (3 << 1), VB), (0xE1000000, near),         # G_RDPHALF_1
                          (0x04000000, 0x1234), (0x05000000 | tri2x(0, 1, 2), 0), (0xDF000000, 0)))
        gfx, tris = self.run_dl(ram, 'F3DEX2')
        self.assertEqual(len(tris), 1)                                                          # the list ended at the branch

    def test_matrix_stack(self):
        ram = FakeRam()
        pts = [(0, 0, 0), (0, 0, 100), (100, 0, 0)]
        ram.put(VB, b''.join(vtx(*p) for p in pts))
        ints = [0] * 16
        fracs = [0] * 16
        for i in range(3):
            ints[5 * i] = 1
        ints[12], ints[13], fracs[13] = 300, 7, 0x8000                                         # translate (300, 7.5, 0)
        ints[15] = 1
        ram.put(MT, struct.pack('>16h', *ints) + struct.pack('>16H', *fracs))
        m = g.read_mtx(ram, MT)
        self.assertAlmostEqual(m[3][0], 300.0)
        self.assertAlmostEqual(m[3][1], 7.5)
        load = lambda: (0x01000000 | (3 << 12) | (3 << 1), VB)                                   # noqa: E731
        tri = (0x05000000 | tri2x(0, 1, 2), 0)
        # F3DEX2 G_MTX flags are stored xor G_MTX_PUSH: push + mul = (0 ^ 1) | 0 -> field value 0x00 (push) / 0x01 (no push)
        ram.put(DL, words((0xDA000000 | (0x00 << 16) | 64, MT), load(), tri, (0xD8000040, 0x40), load(), tri, (0xDF000000, 0)))
        gfx, tris = self.run_dl(ram, 'F3DEX2')
        self.assertEqual(tris[0][0], (300.0, 7.5, 0.0))
        self.assertEqual(tris[1][0], (0.0, 0.0, 0.0))                                           # popped
        # F3DEX: the push flag is bit 0 as is: flags 0x01 = push, mul, modelview
        ram.put(DL, words((0x01000000 | (0x01 << 16) | 64, MT), (0x04000000 | (3 << 10) | 47, VB),
                          (0xBF000000, tri2x(0, 1, 2)), (0xBD000000, 0), (0x04000000 | (3 << 10) | 47, VB),
                          (0xBF000000, tri2x(0, 1, 2)), (0xB8000000, 0)))
        gfx, tris = self.run_dl(ram, 'F3DEX')
        self.assertEqual(tris[0][0], (300.0, 7.5, 0.0))
        self.assertEqual(tris[1][0], (0.0, 0.0, 0.0))
        # a projection matrix (flag 4) is ignored
        ram.put(DL, words((0xDA000000 | (0x04 << 16) | 64, MT), load(), tri, (0xDF000000, 0)))
        gfx, tris = self.run_dl(ram, 'F3DEX2')
        self.assertEqual(tris[0][0], (0.0, 0.0, 0.0))

    def test_lights_f3dex2(self):
        # one diffuse light (colour 100,0,0, direction straight "up" in the map's convention) + ambient 50,50,50
        ram = FakeRam()
        ram.put(VB, b''.join(vtx(*p, rgba=(0, 127, 0, 255)) for p in [(0, 0, 0), (0, 0, 100), (100, 0, 0)]))
        ram.put(LT, bytes([100, 0, 0, 0, 100, 0, 0, 0, 0, 0, 127, 0, 0, 0, 0, 0]) + bytes([50, 50, 50, 0, 50, 50, 50, 0]))
        light = lambda n: 0xDC000000 | (2 << 19) | (((n * 24 + 24) // 8) << 8) | 0x0A         # noqa: E731
        ram.put(DL, words((0xDB000000 | (2 << 16), 24),                                          # G_MW_NUMLIGHT, 1 light
                          (light(1), LT), (light(2) , LT + 16),
                          (0xD9000000 | (~0 & 0xFFFFFF), 0x20000),                                # G_LIGHTING on
                          (0x01000000 | (3 << 12) | (3 << 1), VB), (0x05000000 | tri2x(0, 1, 2), 0), (0xDF000000, 0)))
        scene = {}
        gfx = g.Gfx(ram, scene, 'F3DEX2')
        gfx.run(DL, np.eye(4), 1)
        self.assertEqual(gfx.numlights, 1)
        self.assertEqual(sorted(gfx.lights), [0, 1])
        col = next(iter(scene.values()))['col'][0]
        self.assertEqual(tuple(round(float(c)) for c in col[:3]), (150, 50, 50))

    def test_modifyvtx_and_tile_ops(self):
        ram = FakeRam()
        ram.put(VB, b''.join(vtx(*p) for p in [(0, 0, 0), (0, 0, 100), (100, 0, 0)]))
        ram.put(DL, words((0x01000000 | (3 << 12) | (3 << 1), VB), (0x02000000 | (0x10 << 16) | 0, 0x11223344),
                          (0xD7000000 | (1 << 1), 0xFFFF0000 | 0x8000), (0x05000000 | tri2x(0, 1, 2), 0), (0xDF000000, 0)))
        scene = {}
        gfx = g.Gfx(ram, scene, 'F3DEX2')
        gfx.run(DL, np.eye(4), 1)
        self.assertEqual(gfx.unknown, {})
        self.assertEqual(next(iter(scene.values()))['col'][0], (0x11, 0x22, 0x33, 0x44))
        self.assertEqual(gfx.tex_on, 1)

    def test_unknown_opcode_is_counted(self):
        ram = FakeRam()
        ram.put(DL, words((0xAA000000, 0), (0xDF000000, 0)))
        gfx, _ = self.run_dl(ram, 'F3DEX2')
        self.assertEqual(gfx.unknown, {'0xaa': 1})


class Detection(unittest.TestCase):
    def test_text_families(self):
        f3d = b'\0' * 100 + b'RSP SW Version: 2.0D, 04-01-96\0' + b'\0' * 50
        ex = b'RSP Gfx ucode F3DEX  fifo 1.23 Yoshitaka Yasumoto 1998 Nintendo.'
        lx = b'RSP Gfx ucode F3DLX  fifo 1.21 Yoshitaka Yasumoto 1998 Nintendo.'
        ex2 = b'RSP Gfx ucode F3DEX  fifo 2.08 Yoshitaka Yasumoto 1998 Nintendo.'
        zex = b'RSP Gfx ucode F3DZEX.NoN  fifo 2.08I Yoshitaka Yasumoto/Kawasedo'
        lx2 = b'RSP Gfx ucode F3DLX2.Rej fifo 2.08 Yoshitaka Yasumoto'
        s2dex = b'RSP Gfx ucode S2DEX  fifo 1.06 Yoshitaka Yasumoto'
        self.assertEqual(g.detect_ucode(f3d), 'F3D')
        self.assertEqual(g.detect_ucode(ex), 'F3DEX')
        self.assertEqual(g.detect_ucode(lx), 'F3DEX')
        self.assertEqual(g.detect_ucode(ex2), 'F3DEX2')
        self.assertEqual(g.detect_ucode(zex), 'F3DEX2')
        self.assertEqual(g.detect_ucode(lx2), 'F3DEX2')
        self.assertEqual(g.detect_ucode(s2dex), 'F3D')                       # 2D microcode: no 3D family named
        self.assertEqual(g.detect_ucode(b'nothing here'), 'F3D')
        self.assertEqual(g.detect_ucode(f3d + ex2), 'F3DEX2')                 # several in a ROM: the newest
        self.assertEqual(g.detect_ucode(bytearray(zex)), 'F3DEX2')

    def test_rom_byte_orders(self):
        body = b'\x80\x37\x12\x40' + b'\0' * 60 + b'RSP Gfx ucode F3DZEX.NoN  fifo 2.08I Yoshitaka' + b'\0\0'
        body += b'\0' * ((-len(body)) % 4)
        z64 = body
        v64 = bytearray(z64)
        v64[0::2], v64[1::2] = z64[1::2], z64[0::2]
        n64 = np.frombuffer(z64, dtype='>u4').astype('<u4').tobytes()
        for rom in (z64, bytes(v64), n64):
            self.assertEqual(g.detect_ucode(rom), 'F3DEX2')

    def test_rom_file_path(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.z64', delete=False) as f:
            f.write(b'\x80\x37\x12\x40' + b'RSP SW Version: 2.0H, 02-12-97' + bytes(8))
        try:
            self.assertEqual(g.detect_ucode(f.name), 'F3D')
        finally:
            os.remove(f.name)

    def test_object_attributes(self):
        class WithRom:
            rom_bytes = b'RSP Gfx ucode F3DEX  fifo 1.21 Yoshitaka'

        class WithAnswer:
            ucode = 'F3DEX2'
        self.assertEqual(g.detect_ucode(WithRom()), 'F3DEX')
        self.assertEqual(g.detect_ucode(WithAnswer()), 'F3DEX2')

    def test_by_display_lists(self):
        for ram, want in ((f3d_list(), 'F3D'), (f3dex_list(), 'F3DEX'), (f3dex2_list(), 'F3DEX2')):
            self.assertEqual(g.ucode_of_dls(ram, [DL]), want)
        # F3D with a G_VTX whose low bits happen to read 0xF is still F3D unless the length field agrees
        ram = f3d_list()
        self.assertEqual(g.ucode_of_dls(ram, [DL, 0, DL]), 'F3D')


class Rule(unittest.TestCase):
    def test_object_rule(self):
        r = g.object_rule
        self.assertEqual(r(True), 'collision')
        self.assertEqual(r(True, interact_type=0x200), 'collision')                  # breakable wall
        self.assertEqual(r(True, interact_type=0x8), 'enemy')                        # Whomp, Thwomp: INTERACT_DAMAGE
        self.assertEqual(r(False, interact_type=0x2), 'grabbable')                   # a cork box at rest
        self.assertEqual(r(False, flags=0x400), 'grabbable')                         # OBJ_FLAG_HOLDABLE
        self.assertEqual(r(False, interact_type=0x2, animated=True), 'animated')     # bob-omb
        self.assertEqual(r(False, interact_type=0x2, flags=0x2), 'moving')           # built to move
        self.assertEqual(r(False, interact_type=0x2, speed=12.0), 'moving')          # thrown / sliding
        self.assertEqual(r(False, interact_type=0x2, held=True), 'held')
        self.assertEqual(r(False, interact_type=0x10), 'static')                     # a coin (2026-10-22 rule)
        self.assertEqual(r(False), 'static')                                         # a tree, a sign
        self.assertEqual(r(False, interact_type=0x8, animated=True, flags=0x2), 'animated')   # a goomba
        self.assertEqual(r(False, speed=3.0), 'moving')                              # flying / drifting now

    def test_object_modes(self):
        r = g.object_rule
        coin, goomba, box, tree = dict(interact_type=0x10), dict(interact_type=0x8, animated=True, flags=0x2),             dict(interact_type=0x2), dict()
        keep = lambda kw, m: r(False, mode=m, **kw) in g.OBJECT_KEEPS
        self.assertTrue(all(keep(x, 'static') for x in (coin, box, tree)) and not keep(goomba, 'static'))
        self.assertTrue(keep(box, 'scenery') and keep(tree, 'scenery') and not keep(coin, 'scenery'))
        self.assertTrue(keep(box, 'grabbable') and not keep(tree, 'grabbable') and not keep(coin, 'grabbable'))
        self.assertFalse(any(keep(x, 'collision') for x in (coin, box, tree, goomba)))
        self.assertTrue(all(keep(x, 'all') for x in (coin, box, tree, goomba)))
        self.assertFalse(any(keep(x, 'none') for x in (coin, box, tree, goomba)))
        self.assertEqual(r(True, interact_type=0x8, mode='all'), 'collision')               # Whomp, in 'all'
        self.assertEqual(r(True, mode='none'), 'no collision')
        self.assertEqual((g.object_mode(True), g.object_mode(False), g.object_mode(None)), ('static', 'none', 'none'))
        with self.assertRaises(ValueError):
            g.object_mode('enemies')

    def make_object(self, ram, base, **kw):
        d = dict(flags=0, active=1, area=1, shared=0x80200000, collision=0, interact=0, ofl=0, anim=0, held=0,
                 fvel=0.0)
        d.update(kw)
        buf = bytearray(0x260)
        struct.pack_into('>hh', buf, 0, 0x18, d['flags'])
        struct.pack_into('>I', buf, 0x14, d['shared'])
        buf[0x19] = d['area']
        struct.pack_into('>h', buf, 0x74, d['active'])
        struct.pack_into('>I', buf, 0x218, d['collision'])
        struct.pack_into('>I', buf, 0x130, d['interact'])
        struct.pack_into('>I', buf, 0x8C, d['ofl'])
        struct.pack_into('>I', buf, 0x3C, d['anim'])
        struct.pack_into('>I', buf, 0x124, d['held'])
        struct.pack_into('>f', buf, 0xB8, d['fvel'])
        ram.put(base, bytes(buf))

    def test_keep_object_on_ram(self):
        ram = FakeRam()
        o = 0x80300000
        cases = [({'collision': 0x80400000}, True), ({'interact': 2}, True), ({}, True),     # {}: a static object
                 ({'collision': 1, 'active': 0}, False), ({'collision': 1, 'flags': 0x10}, False),
                 ({'collision': 1, 'area': 2}, False), ({'collision': 1, 'shared': 0}, False),
                 ({'interact': 2, 'fvel': 20.0}, False), ({'collision': 1, 'interact': 8}, False),
                 ({'interact': 2, 'anim': 0x80400000}, False)]
        for i, (kw, want) in enumerate(cases):
            self.make_object(ram, o, **kw)
            self.assertEqual(g.keep_object(ram, o, mario_obj=0x80999999, area_index=1), want, (i, kw))
        self.make_object(ram, o, collision=1)
        self.assertFalse(g.keep_object(ram, o, mario_obj=o, area_index=1))                  # Mario himself
        self.assertEqual(g.object_reason(ram, o, mario_obj=o), 'mario')


class Scheme(unittest.TestCase):
    def test_types_and_layers(self):
        s = g.GraphScheme(True, g.GraphScheme.LAYERS8)
        self.assertEqual([s.type(i) for i in (0, 1, 2, 5, 12, 15, 16, 17, 20, 21)],
                         [g.T_ORTHO, g.T_PERSP, g.T_MASTER, g.T_CAMERA, g.T_DL, g.T_OBJPARENT, g.T_GENERATED,
                          g.T_BACKGROUND, g.T_ROOT, g.T_START])
        self.assertEqual(s.type(99), -1)
        self.assertEqual([s.layer(i) for i in range(8)], [0, 1, 3, 2, 4, 6, 5, 7])
        v = g.GraphScheme()
        self.assertEqual((v.type(0x1B), v.layer(5)), (0x1B, 5))

    def test_detect(self):
        ram = FakeRam()
        root, master = 0x80500000, 0x80500100
        ram.put(root, struct.pack('>hh', 20, 1) + bytes(12) + struct.pack('>I', master))          # type 20, child at +0x10
        ram.put(master, struct.pack('>hh', 2, 1) + struct.pack('>III', 0, master, 0) + struct.pack('>I', master + 0x54))
        s = g.detect_scheme(ram, root)
        self.assertTrue(s.hacker)
        self.assertEqual(s.layers, g.GraphScheme.LAYERS8)
        ram.put(root, struct.pack('>hh', 1, 1) + bytes(12) + struct.pack('>I', master))
        self.assertFalse(g.detect_scheme(ram, root).hacker)

    def test_cyclic_graph_stops(self):
        # a damaged graph: an active group node whose child is itself would recurse forever
        ram = FakeRam()
        node = 0x80500000
        ram.put(node, struct.pack('>hh', g.T_START, 1) + struct.pack('>IIII', node, 0, 0, node))
        w = g.Walker(ram)
        w.walk(node, np.eye(4))
        self.assertEqual(w.depth_capped, 1)
        self.assertEqual(w.counts[g.T_START], g.MAX_GRAPH_DEPTH + 1)

if __name__ == '__main__':
    unittest.main()
