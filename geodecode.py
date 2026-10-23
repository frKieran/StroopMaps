"""Stage 2: the loaded area's visual geometry from a savestate's RAM (geo graph + F3D display lists + textures).

decode(state) walks gCurrentArea->unk04 (the area's GraphNodeRoot, decomp/src/engine/graph_node.h), applies the
translation / rotation / scale nodes, takes the first child of each level-of-detail group's highest-detail range
and the selected case of each switch, skips the background, object lists and functional nodes it doesn't know, and
runs every display list through a small F3D interpreter (decomp/include/PR/gbi.h, the F3D_OLD encoding US uses).
Each drawn triangle becomes (positions, colours, uvs) in a batch keyed by its material (texture, combiner use,
render layer). Textures are decoded to RGBA arrays (RGBA16/32, IA4/8/16, I4/8, CI4/8 with the TLUT).

Microcodes (2026-10-22): Gfx(ram, scene, ucode='F3D' | 'F3DEX' | 'F3DEX2'). F3DEX adds G_TRI2 / G_QUAD, 64-entry
vertex buffers, the doubled vertex indices, G_BRANCH_Z, G_CULLDL and G_MODIFYVTX; F3DEX2 renumbers the opcodes
(G_VTX 0x01, G_TRI1 0x05, G_TRI2 0x06, G_QUAD 0x07, G_DL 0xDE, G_ENDDL 0xDF, G_GEOMETRYMODE 0xD9, G_MTX 0xDA,
G_MOVEWORD 0xDB, G_MOVEMEM 0xDC, G_TEXTURE 0xD7, ...) and moves the geometry-mode bits (translated here to the F3D
bit positions, so the rest of the decoder has one encoding). detect_ucode(ram_or_bytes) finds a game's microcode
from the RSP microcode text in RAM or a ROM, else by reading its display lists. The opcode tables, the vertex-load
and triangle forms and the detection by display-list shape follow hugou74130's STROOP Next
(`next/src/StroopNext.Core/Graphics/F3DInterpreter.cs`, branch feature/stroop-next of the STROOP repo, which has no
licence); this is a re-implementation in Python, not a copy of its files.

Other layouts: a savestate of a rebuilt (decomp-built) hack has gCurrentArea and the segment table elsewhere than
US. Ram(path) checks the US addresses against Mario's own pointers and, if they disagree, finds both by scan
(detect_layout).

The object rule: keep_object() decides which RAM objects the map draws, by mode (OBJECT_MODES; the default draws
collision objects and everything that doesn't move); never Mario, inactive or invisible objects.
"""
import math, os, re, struct, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sm64io as m64                              # noqa: E402 - Savestate, RDRAM_OFFSET

SEGMENT_TABLE = 0x8033B400
CURRENT_AREA = 0x8032DDCC
MAX_GRAPH_DEPTH = 64                              # the deepest real graph is far shallower (the decomp nests ~10)

T_FUNC = 0x100
T_ROOT, T_ORTHO, T_PERSP, T_MASTER, T_START, T_LOD, T_SWITCH, T_CAMERA = 1, 2, 0x103, 4, 0xA, 0xB, 0x10C, 0x114
T_TRANSROT, T_TRANS, T_ROT, T_OBJECT, T_ANIM, T_BILL, T_DL, T_SCALE = 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C
T_SHADOW, T_OBJPARENT, T_GENERATED, T_BACKGROUND, T_HELD, T_CULL = 0x28, 0x29, 0x12A, 0x12C, 0x12E, 0x2F

G_LIGHTING = 0x00020000
G_TEXTURE_GEN = 0x00040000
GEOM_INIT = 0x00022204       # init_rsp (game_init.c): G_SHADE | G_SHADING_SMOOTH | G_CULL_BACK | G_LIGHTING
FMT = {0: 'RGBA', 1: 'YUV', 2: 'CI', 3: 'IA', 4: 'I'}
SIZ_BITS = {0: 4, 1: 8, 2: 16, 3: 32}


LAYOUT_US = {'segtable': SEGMENT_TABLE, 'area': CURRENT_AREA, 'source': 'us'}


def _rdram_words(st):
    """The savestate's RDRAM as big-endian N64 words (a numpy uint32 array of the values)."""
    n = (len(st.data) - m64.RDRAM_OFFSET) // 4
    return np.frombuffer(st.data, dtype='<u4', offset=m64.RDRAM_OFFSET, count=n)


def detect_layout(st):
    """{'segtable', 'area', 'source'}: where gSegmentTable and gCurrentArea are in this savestate. US and every
    US-layout hack (Island, SS1R, ...) keep the decomp's US addresses, checked against Mario's own pointer to his
    area; a rebuilt hack (Hidden Stars, ...) has them elsewhere, so both are found by scan: gCurrentArea is the word
    that equals Mario's area pointer outside Mario's struct, and gSegmentTable is the 32-word block whose entry 0 is 0
    and whose entry 1 and most later ones are 8-aligned RDRAM offsets, nearly all distinct (the best such block wins;
    run on the states of the 8 Island areas and vanilla BoB it is only used when the US addresses fail the check)."""
    try:
        m = st.mario_state
        area = st.u32(m + 0x90)
    except Exception:                                       # noqa: BLE001 - no Mario: assume the US layout
        return dict(LAYOUT_US)
    if area and st.u32(CURRENT_AREA) == area:
        return dict(LAYOUT_US)
    w = _rdram_words(st)
    hits = [0x80000000 + 4 * int(i) for i in np.nonzero(w == area)[0]]
    hits = [a for a in hits if not m <= a < m + 0x110]
    if not hits:
        return dict(LAYOUT_US, source='us (no gCurrentArea found)')
    big = (w > 0x10000) & (w < 0x800000) & ((w & 7) == 0)
    ok = (w == 0) | big
    bad = np.concatenate([[0], np.cumsum(~ok)])
    nbig = np.concatenate([[0], np.cumsum(big)])
    n = len(w) - 33
    mask = (w[:n] == 0) & (w[1:n + 1] != 0) & (bad[32:n + 32] - bad[:n] == 0) & (nbig[32:n + 32] - nbig[:n] >= 7)
    cands = []
    for i in np.nonzero(mask)[0]:
        k = int(nbig[i + 32] - nbig[i])
        d = len(set(w[i:i + 32][big[i:i + 32]].tolist()))
        if d >= k - 3:                                      # a segment can be mapped twice (The Preservation Project)
            cands.append((-d, int(i)))
    cands = [0x80000000 + 4 * i for _, i in sorted(cands)]
    return {'segtable': cands[0] if cands else SEGMENT_TABLE, 'area': hits[0], 'source': 'scan',
            'segtable_candidates': cands}


class Ram:
    """A savestate's RDRAM with the interface the walker needs (va, u32, s16, f32, raw). `layout` (default:
    detect_layout) holds gSegmentTable and gCurrentArea; `mario_object` is gMarioObject (Mario's MarioState.marioObj)."""

    def __init__(self, path, layout=None):
        self.st = m64.Savestate(path)
        self.layout = layout or detect_layout(self.st)
        self.seg = [self.st.u32(self.layout['segtable'] + 4 * i) for i in range(32)]
        try:
            self.mario_object = self.st.u32(self.st.mario_state + 0x88)
        except Exception:                                   # noqa: BLE001
            self.mario_object = self.st.u32(0x80361158)

    def va(self, a):
        """A segmented or KSEG0 address as a KSEG0 one."""
        if a >= 0x80000000:
            return a
        s = (a >> 24) & 0x1F
        return 0x80000000 | ((self.seg[s] + (a & 0xFFFFFF)) & 0x7FFFFF)

    def u32(self, a): return self.st.u32(a)
    def s16(self, a): return self.st.s16(a)
    def f32(self, a): return self.st.f32(a)
    def raw(self, a, n): return self.st.bytes(a, n)


# ------------------------------------------------------------------ matrices (row vectors, as the game's Mtxf)
def mat_trs(t, r=None, s=1.0):
    """mtxf_rotate_zxy_and_translate (math_util.c): rotation Z, then X, then Y; then the translation."""
    m = np.eye(4)
    if r is not None and any(r):
        sx, cx = math.sin(r[0] * math.pi / 0x8000), math.cos(r[0] * math.pi / 0x8000)
        sy, cy = math.sin(r[1] * math.pi / 0x8000), math.cos(r[1] * math.pi / 0x8000)
        sz, cz = math.sin(r[2] * math.pi / 0x8000), math.cos(r[2] * math.pi / 0x8000)
        rz = np.array([[cz, sz, 0], [-sz, cz, 0], [0, 0, 1]])
        rx = np.array([[1, 0, 0], [0, cx, sx], [0, -sx, cx]])
        ry = np.array([[cy, 0, -sy], [0, 1, 0], [sy, 0, cy]])
        m[:3, :3] = rz @ rx @ ry
    m[:3, :3] *= s
    m[3, :3] = t
    return m


# ------------------------------------------------------------------ textures
def decode_texture(ram, addr, fmt, siz, w, h, tlut=None):
    """RGBA uint8 (h, w, 4) of a texture stored row-major in RAM."""
    bits = SIZ_BITS[siz]
    n = (w * h * bits + 7) // 8
    d = np.frombuffer(ram.raw(ram.va(addr), n), dtype=np.uint8)
    f = FMT.get(fmt, 'RGBA')
    if bits == 4:
        v = np.stack([d >> 4, d & 15], 1).reshape(-1)[:w * h]
    elif bits == 8:
        v = d[:w * h]
    elif bits == 16:
        v = d[:2 * w * h].view('>u2').astype(np.uint32)
    else:
        v = d[:4 * w * h].view('>u4')
    out = np.zeros((w * h, 4), dtype=np.uint8)
    if f == 'RGBA' and bits == 16:
        out[:, 0] = ((v >> 11) & 31) * 255 // 31
        out[:, 1] = ((v >> 6) & 31) * 255 // 31
        out[:, 2] = ((v >> 1) & 31) * 255 // 31
        out[:, 3] = (v & 1) * 255
    elif f == 'RGBA' and bits == 32:
        for i in range(4):
            out[:, i] = (v >> (24 - 8 * i)) & 255
    elif f == 'IA' and bits == 4:
        i = (v >> 1) * 255 // 7
        out[:, 0] = out[:, 1] = out[:, 2] = i
        out[:, 3] = (v & 1) * 255
    elif f == 'IA' and bits == 8:
        out[:, 0] = out[:, 1] = out[:, 2] = (v >> 4) * 17
        out[:, 3] = (v & 15) * 17
    elif f == 'IA' and bits == 16:
        out[:, 0] = out[:, 1] = out[:, 2] = v >> 8
        out[:, 3] = v & 255
    elif f == 'I':
        i = v * 17 if bits == 4 else v
        out[:, 0] = out[:, 1] = out[:, 2] = out[:, 3] = i
    elif f == 'CI' and tlut is not None:
        out = tlut[np.minimum(v, len(tlut) - 1)]
    else:
        out[:] = (255, 0, 255, 255)
    return out.reshape(h, w, 4)


def decode_tlut(ram, addr, count):
    """`count` RGBA5551 palette entries at `addr` as RGBA8 rows (a CI texture's TLUT)."""
    v = np.frombuffer(ram.raw(ram.va(addr), 2 * count), dtype='>u2').astype(np.uint32)
    out = np.zeros((count, 4), dtype=np.uint8)
    out[:, 0] = ((v >> 11) & 31) * 255 // 31
    out[:, 1] = ((v >> 6) & 31) * 255 // 31
    out[:, 2] = ((v >> 1) & 31) * 255 // 31
    out[:, 3] = (v & 1) * 255
    return out


# ------------------------------------------------------------------ display-list interpreter
UCODES = ('F3D', 'F3DEX', 'F3DEX2')

# F3DEX2 geometry-mode bits (gbi.h, F3DEX_GBI_2) -> the F3D bit positions this decoder keeps its mode in
EX2_CULL_FRONT, EX2_CULL_BACK, EX2_SHADING_SMOOTH = 0x200, 0x400, 0x200000
F3D_CULL_FRONT, F3D_CULL_BACK, F3D_SHADING_SMOOTH = 0x1000, 0x2000, 0x200
SHARED_GEO = 0x1 | 0x4 | 0x10000 | 0x20000 | 0x40000 | 0x80000 | 0x100000   # zbuffer shade fog lighting texgen(+linear) lod

# RDP commands (the same numbers in every microcode) and RSP ones a map has nothing to do with
IGNORED_RDP = frozenset({0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xEB, 0xEC, 0xED, 0xEE, 0xEF, 0xF1, 0xF6, 0xF7,
                         0xF8, 0xF9, 0xFE, 0xFF})
IGNORED_EX = IGNORED_RDP | {0x00, 0xE0, 0xB3, 0xDD, 0x08}   # G_NOOP, G_SPNOOP, G_RDPHALF_2 (F3DEX), G_LOAD_UCODE, G_LINE3D

# opcode tables: names -> number, per microcode (gbi.h)
OPS_EX = dict(ENDDL=0xB8, DL=0x06, VTX=0x04, TRI1=0xBF, TRI2=0xB1, QUAD=0xB5, BRANCHZ=0xB0, CULLDL=0xBE,
              MODIFYVTX=0xB2, RDPHALF1=0xB4, TEXTURE=0xBB, GEOM=-1, SETGEO=0xB7, CLEARGEO=0xB6, MTX=0x01,
              POPMTX=0xBD, MOVEWORD=0xBC, MOVEMEM=0x03, OTHER_L=0xB9, OTHER_H=0xBA)
OPS_EX2 = dict(ENDDL=0xDF, DL=0xDE, VTX=0x01, TRI1=0x05, TRI2=0x06, QUAD=0x07, BRANCHZ=0x04, CULLDL=0x03,
               MODIFYVTX=0x02, RDPHALF1=0xE1, TEXTURE=0xD7, GEOM=0xD9, SETGEO=-1, CLEARGEO=-1, MTX=0xDA,
               POPMTX=0xD8, MOVEWORD=0xDB, MOVEMEM=0xDC, OTHER_L=0xE2, OTHER_H=0xE3)


def geo_ex2_to_f3d(bits):
    """F3DEX2's geometry-mode bits in F3D's layout, so one renderer state serves every microcode."""
    r = bits & SHARED_GEO
    if bits & EX2_CULL_FRONT: r |= F3D_CULL_FRONT
    if bits & EX2_CULL_BACK: r |= F3D_CULL_BACK
    if bits & EX2_SHADING_SMOOTH: r |= F3D_SHADING_SMOOTH
    return r


def read_mtx(ram, addr):
    """A game Mtx (s15.16 in two halves: 16 s16 integer parts, then 16 u16 fractions, row-major) as the 4x4 float
    matrix for row vectors that the walker uses."""
    b = ram.raw(ram.va(addr), 64)
    hi = np.frombuffer(b[:32], dtype='>i2').astype(np.float64)
    lo = np.frombuffer(b[32:], dtype='>u2').astype(np.float64)
    return (hi + lo / 65536.0).reshape(4, 4)


class Gfx:
    """The display-list interpreter. ucode 'F3D' is the encoding vanilla SM64 and ROM Manager hacks use (F3D_OLD);
    'F3DEX' and 'F3DEX2' are the other two microcodes romhacks use (module docstring)."""

    def __init__(self, ram, scene, ucode='F3D'):
        if ucode not in UCODES:
            raise ValueError(f'unknown microcode {ucode!r} (known: {", ".join(UCODES)})')
        self.ucode = ucode
        self.ram, self.scene = ram, scene
        self.vtx = [None] * (32 if ucode == 'F3D' else 64)
        self.numlights = None             # G_MOVEWORD NUMLIGHT (F3DEX / F3DEX2); F3D counts the lights it was given
        self.rdphalf1 = 0                 # G_RDPHALF_1's word: the target of G_BRANCH_Z
        self.mv_stack = []                # the modelview matrix stack (G_MTX push / G_POPMTX), F3DEX / F3DEX2 only
        self.notes = {}                   # handled commands worth knowing about (G_MTX in a level's display lists)
        self.geom = GEOM_INIT
        self.timg = None                  # (addr, fmt, siz, width)
        self.tiles = {i: {} for i in range(8)}
        self.tmem = {}                    # tmem word address -> (addr, fmt, siz, width) loaded there
        self.tlut = None                  # (addr, count)
        self.tex_on, self.tex_tile, self.tex_s, self.tex_t = 0, 0, 1.0, 1.0
        self.combine = (0, 0)
        self.prim = self.env = (255, 255, 255, 255)
        self.lights = {}
        self.othermode_l = 0
        self.unknown = {}
        self.culled = 0
        self.light_model = None           # (L world, ambient, diffuse): one light for every level instead of the ROM's
        self.white = False                # lit vertices drawn white (for fitting a reference image's lighting)

    def run(self, dl, mtx, layer, depth=0):
        """Interpret the display list at `dl` under matrix `mtx`, adding its triangles to the scene."""
        ram = self.ram
        a = ram.va(dl)
        ex = self.ucode != 'F3D'
        if depth == 0:
            self.mv_stack = []
        for _ in range(100000):
            w0, w1 = ram.u32(a), ram.u32(a + 4)
            a += 8
            op = w0 >> 24
            if ex:
                done, mtx = self._step_ex(op, w0, w1, mtx, layer, depth)
                if done:
                    return
                continue
            if op == 0x04:                                   # G_VTX (F3D): n-1 in bits 20..23, v0 in 16..19
                n, v0 = ((w0 >> 20) & 15) + 1, (w0 >> 16) & 15
                self.load_vtx(w1, n, v0, mtx)
            elif op == 0xBF:                                 # G_TRI1
                i = [((w1 >> s) & 255) // 10 for s in (16, 8, 0)]
                self.tri(i, layer)
            elif op == 0xB1:                                 # G_TRI2 (F3DEX): not F3D
                self.unknown['TRI2'] = self.unknown.get('TRI2', 0) + 1
            elif op == 0x06:                                 # G_DL
                if depth < 16:
                    self.run(w1, mtx, layer, depth + 1)
                if (w0 >> 16) & 255 == 1:                    # G_DL_NOPUSH: a branch
                    return
            elif op == 0xB8:                                 # G_ENDDL
                return
            elif op == 0xB7:
                self.geom |= w1
            elif op == 0xB6:
                self.geom &= ~w1
            elif op == 0xBB:                                 # G_TEXTURE
                self.tex_on, self.tex_tile = w0 & 255, (w0 >> 8) & 7
                self.tex_s, self.tex_t = ((w1 >> 16) & 0xFFFF) / 65536.0, (w1 & 0xFFFF) / 65536.0
            elif op == 0x03:                                 # G_MOVEMEM: lights (F3D: G_MV_L0 = 0x86, +2 per light)
                idx = (w0 >> 16) & 255
                if 0x86 <= idx <= 0x94:
                    self.read_light((idx - 0x86) // 2, w1)
            elif op == 0xB9:
                self.othermode_l = w1
            elif self.rdp(op, w0, w1):
                pass
            elif op in (0x00, 0xE6, 0xE7, 0xE8, 0xE9, 0xBA, 0xBC, 0xBE, 0xBD, 0x01, 0xF7, 0xF8, 0xF9, 0xB4, 0xB3,
                        0xB2, 0xED, 0xEF, 0xEE, 0xFF, 0xFE, 0xF6, 0xE4, 0xE5):
                pass
            else:
                self.unknown[hex(op)] = self.unknown.get(hex(op), 0) + 1
        self.unknown['runaway'] = self.unknown.get('runaway', 0) + 1

    def read_light(self, k, addr):
        """Light k (0 = the first diffuse light; the ambient light follows the last diffuse one) from a Light struct."""
        la = self.ram.va(addr)
        col = self.ram.raw(la, 3)
        dx, dy, dz = struct.unpack('>bbb', self.ram.raw(la + 8, 3))
        self.lights[k] = (tuple(col), (dx, dy, dz))

    def rdp(self, op, w0, w1):
        """The RDP commands every microcode shares (textures, tiles, combiner, colours); True when handled."""
        if op == 0xFD:                                   # G_SETTIMG
            self.timg = (w1, (w0 >> 21) & 7, (w0 >> 19) & 3, (w0 & 0xFFF) + 1)
        elif op == 0xF5:                                 # G_SETTILE
            t = (w1 >> 24) & 7
            self.tiles[t] = dict(fmt=(w0 >> 21) & 7, siz=(w0 >> 19) & 3, line=(w0 >> 9) & 511, tmem=w0 & 511,
                                 pal=(w1 >> 20) & 15, cmt=(w1 >> 18) & 3, maskt=(w1 >> 14) & 15,
                                 shiftt=(w1 >> 10) & 15, cms=(w1 >> 8) & 3, masks=(w1 >> 4) & 15,
                                 shifts=w1 & 15, **{k: self.tiles[t].get(k, 0) for k in ('uls', 'ult', 'lrs', 'lrt')})
        elif op in (0xF3, 0xF4):                         # G_LOADBLOCK / G_LOADTILE
            t = (w1 >> 24) & 7
            if self.timg is not None:
                self.tmem[self.tiles[t].get('tmem', 0)] = self.timg
        elif op == 0xF0:                                 # G_LOADTLUT
            if self.timg is not None:
                t = (w1 >> 24) & 7
                self.tlut = (self.timg[0], ((w1 >> 14) & 0x3FF) + 1, self.tiles[t].get('tmem', 256))
        elif op == 0xF2:                                 # G_SETTILESIZE
            t = (w1 >> 24) & 7
            self.tiles[t].update(uls=(w0 >> 12) & 0xFFF, ult=w0 & 0xFFF, lrs=(w1 >> 12) & 0xFFF, lrt=w1 & 0xFFF)
        elif op == 0xFC:
            self.combine = (w0 & 0xFFFFFF, w1)
        elif op == 0xFA:
            self.prim = tuple((w1 >> s) & 255 for s in (24, 16, 8, 0))
        elif op == 0xFB:
            self.env = tuple((w1 >> s) & 255 for s in (24, 16, 8, 0))
        else:
            return False
        return True

    # ---- F3DEX and F3DEX2
    def _step_ex(self, op, w0, w1, mtx, layer, depth):
        """One command of an F3DEX / F3DEX2 display list: (True when the list ends, the current matrix)."""
        ex2 = self.ucode == 'F3DEX2'
        O = OPS_EX2 if ex2 else OPS_EX
        if ex2 and op >= 0xE4:                               # the RDP commands, same numbers as in F3D
            if not self.rdp(op, w0, w1) and op not in IGNORED_RDP:
                self.unknown[hex(op)] = self.unknown.get(hex(op), 0) + 1
            return False, mtx
        if op == O['ENDDL']:
            return True, mtx
        if op == O['DL']:
            if depth < 16:
                self.run(w1, mtx, layer, depth + 1)
            return (w0 >> 16) & 255 == 1, mtx                # G_DL_NOPUSH: a branch
        if op == O['VTX']:
            if ex2:                                          # n in 12..19, (v0 + n) * 2 in 1..7
                n = (w0 >> 12) & 0xFF
                self.load_vtx(w1, n, ((w0 >> 1) & 0x7F) - n, mtx)
            else:                                            # v0 * 2 in 16..23, n in 10..15
                self.load_vtx(w1, (w0 >> 10) & 0x3F, ((w0 >> 16) & 0xFF) // 2, mtx)
        elif op == O['TRI1']:
            self.tri2x(w0 if ex2 else w1, layer)
        elif op in (O['TRI2'], O['QUAD']):                   # a quad is two triangles in the same encoding
            self.tri2x(w0, layer)
            self.tri2x(w1, layer)
        elif op == O['BRANCHZ']:                             # taken: a map has no camera depth, so the near LOD
            if depth < 16 and self.rdphalf1:
                self.run(self.rdphalf1, mtx, layer, depth + 1)
            return True, mtx
        elif op == O['RDPHALF1']:
            self.rdphalf1 = w1
        elif op == O['CULLDL']:                              # frustum test: a map draws everything
            pass
        elif op == O['MODIFYVTX']:
            where, idx = (w0 >> 16) & 0xFF, (w0 & 0xFFFF) // 2
            v = self.vtx[idx] if 0 <= idx < len(self.vtx) else None
            if v is not None and where == 0x10:              # G_MWO_POINT_RGBA
                self.vtx[idx] = (v[0], tuple((w1 >> s) & 255 for s in (24, 16, 8, 0)), v[2])
            elif v is not None and where == 0x14:            # G_MWO_POINT_ST
                s16 = lambda x: x - 0x10000 if x & 0x8000 else x           # noqa: E731
                self.vtx[idx] = (v[0], v[1], (s16(w1 >> 16), s16(w1 & 0xFFFF)))
        elif ex2 and op == O['GEOM']:                        # keep-mask in w0 (the complement of the cleared bits)
            self.geom = (self.geom & ~geo_ex2_to_f3d(~w0 & 0xFFFFFF)) | geo_ex2_to_f3d(w1)
        elif op == O['SETGEO']:
            self.geom |= w1
        elif op == O['CLEARGEO']:
            self.geom &= ~w1
        elif op == O['TEXTURE']:
            self.tex_on = (w0 >> 1) & 0x7F if ex2 else w0 & 0xFF
            self.tex_tile = (w0 >> 8) & 7
            self.tex_s, self.tex_t = ((w1 >> 16) & 0xFFFF) / 65536.0, (w1 & 0xFFFF) / 65536.0
        elif op == O['MTX']:
            flags = (w0 >> 16) & 0xFF
            push = not flags & 1 if ex2 else bool(flags & 1)  # F3DEX2 stores the NOPUSH sense (gSPMatrix xors G_MTX_PUSH)
            if not flags & 4:                                # modelview only: the projection is the view, not the map
                m = read_mtx(self.ram, w1)
                if push:
                    self.mv_stack.append(mtx)
                mtx = m if flags & 2 else m @ mtx            # G_MTX_LOAD / G_MTX_MUL (row vectors: the new one first)
                self.note('mtx_load' if flags & 2 else 'mtx_mul')
        elif op == O['POPMTX']:
            for _ in range(max(1, w1 // 64) if ex2 else 1):
                if self.mv_stack:
                    mtx = self.mv_stack.pop()
        elif op == O['MOVEMEM']:
            if ex2:
                if (w0 & 0xFF) == 0x0A:                      # G_MV_LIGHT: the offset says which (0 and 24 are LookAt)
                    off = ((w0 >> 8) & 0xFF) * 8
                    if off % 24 == 0 and off >= 48:
                        self.read_light(off // 24 - 2, w1)
            elif (w0 & 0xFF) == 0x86:                        # F3DEX gDma2p form: index 0x86, byte offset / 8 in 8..15
                off = ((w0 >> 8) & 0xFF) * 8
                if off % 24 == 0 and off >= 48:
                    self.read_light(off // 24 - 2, w1)
            elif 0x86 <= (w0 >> 16) & 0xFF <= 0x94:         # the F3D form, as some F3DEX builds keep it
                self.read_light((((w0 >> 16) & 0xFF) - 0x86) // 2, w1)
        elif op == O['MOVEWORD']:
            idx = (w0 >> 16) & 0xFF if ex2 else w0 & 0xFF
            off = w0 & 0xFFFF if ex2 else (w0 >> 8) & 0xFFFF
            if idx == 0x02:                                  # G_MW_NUMLIGHT: n * 24 (F3D's NUML(n) form also read)
                n = w1 // 24 if w1 < 0x80000000 else ((w1 - 0x80000000) >> 5) - 1
                self.numlights = max(1, min(7, n))
            elif idx == 0x0A and ex2 and off % 24 == 0:      # G_MW_LIGHTCOL: the n-th light's colour
                k = off // 24
                d = self.lights.get(k, ((0, 0, 0), (0x28, 0x28, 0x28)))[1]
                self.lights[k] = (((w1 >> 24) & 255, (w1 >> 16) & 255, (w1 >> 8) & 255), d)
        elif op == O['OTHER_L']:
            if ex2:
                ln = (w0 & 0xFF) + 1
                sh = 32 - ((w0 >> 8) & 0xFF) - ln
            else:
                sh, ln = (w0 >> 8) & 0xFF, w0 & 0xFF
            mask = ((1 << ln) - 1) << sh
            self.othermode_l = (self.othermode_l & ~mask) | (w1 & mask)
        elif op == O['OTHER_H']:
            pass
        elif self.rdp(op, w0, w1):
            pass
        elif op not in IGNORED_EX:
            self.unknown[hex(op)] = self.unknown.get(hex(op), 0) + 1
        return False, mtx

    def note(self, key):
        """Count a command that is handled but worth knowing about (a map's display lists seldom have them)."""
        self.notes[key] = self.notes.get(key, 0) + 1

    def tri2x(self, w, layer):
        """One triangle whose vertex numbers are in the low 24 bits of w, doubled (F3DEX, F3DEX2)."""
        self.tri([((w >> 16) & 0xFF) // 2, ((w >> 8) & 0xFF) // 2, (w & 0xFF) // 2], layer)

    def load_vtx(self, addr, n, v0, mtx):
        """G_VTX: load `n` vertices from `addr` into the buffer at `v0`, transformed by `mtx` and lit."""
        ram = self.ram
        b = ram.raw(ram.va(addr), 16 * n)
        lit = bool(self.geom & G_LIGHTING)
        if lit:
            nl = self.numlights or max(len(self.lights) - 1, 1)   # F3DEX / F3DEX2 say how many; F3D is inferred
            amb = np.array(self.lights.get(nl, ((0, 0, 0), None))[0], dtype=np.float64)
            # SM64's light directions are in view space (the lighting turns with the camera). STROOP's map images
            # were lit as by a camera looking down while facing +Z (yaw 0): view x = world -x, view y (screen up)
            # = world +z, view z (towards the eye) = world +y. (The image itself is still north-up.)
            dif = [(np.array(c, dtype=np.float64), np.array((-d[0], d[2], d[1]), dtype=np.float64) / 127.0)
                   for k, (c, d) in self.lights.items() if k < nl]
        for k in range(n):
            x, y, z, _, s, t = struct.unpack('>hhhHhh', b[16 * k:16 * k + 12])
            r, g, bb, al = b[16 * k + 12:16 * k + 16]
            p = np.array([x, y, z, 1.0]) @ mtx
            if lit and self.white:
                col = (255, 255, 255, al)
            elif lit and self.light_model:
                L, la, lk = self.light_model
                nrm = np.array(struct.unpack('>bbb', bytes([r, g, bb])), dtype=np.float64) @ mtx[:3, :3]
                ln = np.linalg.norm(nrm)
                nrm = nrm / ln if ln else nrm
                # the fitted shape, tinted by the ROM's own light colours (black lights stay black: pipe voids)
                tint = np.clip(amb + sum(dc for dc, _ in dif), 0, 255)
                v = tint * min(1.0, la + lk * max(0.0, float(nrm @ L)))
                col = tuple(v) + (al,)
            elif lit:
                nrm = np.array(struct.unpack('>bbb', bytes([r, g, bb])), dtype=np.float64) @ mtx[:3, :3]
                ln = np.linalg.norm(nrm)
                nrm = nrm / ln if ln else nrm
                c = amb.copy()
                for dc, dd in dif:
                    dl = np.linalg.norm(dd)
                    c += dc * max(0.0, float(nrm @ (dd / dl))) if dl else 0
                col = tuple(np.clip(c, 0, 255)) + (al,)
            else:
                col = (r, g, bb, al)
            self.vtx[v0 + k] = (p[:3], col, (s, t))

    def material(self):
        """The key of the current texture and combiner state (one scene batch per material)."""
        tile = self.tiles[self.tex_tile] if self.tex_on else None
        tex = None
        if tile and tile.get('fmt') is not None and 'lrs' in tile:
            src = self.tmem.get(tile.get('tmem', 0))
            if src is not None:
                w = ((tile['lrs'] - tile['uls']) >> 2) + 1
                h = ((tile['lrt'] - tile['ult']) >> 2) + 1
                if tile.get('masks'):
                    w = min(w, 1 << tile['masks']) if tile['cms'] & 2 == 0 else w
                if tile.get('maskt'):
                    h = min(h, 1 << tile['maskt']) if tile['cmt'] & 2 == 0 else h
                tl = None
                if tile['fmt'] == 2 and self.tlut:
                    tl = (self.tlut[0], 16 if tile['siz'] == 0 else 256, tile['pal'] if tile['siz'] == 0 else 0)
                tex = (src[0], tile['fmt'], tile['siz'], w, h, tl, tile['cms'], tile['cmt'])
        c0, c1 = self.combine
        # combiner use: does the colour cycle read TEXEL0 (1) / SHADE (4) / PRIMITIVE (3) / ENVIRONMENT (5)?
        a0, c0_, b0 = (c0 >> 20) & 15, (c0 >> 15) & 31, (c1 >> 28) & 15
        d0 = (c1 >> 15) & 7
        used = {a0, b0, d0, c0_ if c0_ < 16 else None}
        mode = ('tex' if 1 in used or 1 == c0_ else '') + ('shade' if 4 in used or c0_ == 4 else '')
        if not mode:
            mode = 'prim' if 3 in used else 'env' if 5 in used else 'shade'
        if 'tex' not in mode:
            tex = None
        return tex, mode, self.prim if 3 in used else self.env if 5 in used and 'shade' not in mode else None

    def tri(self, idx, layer):
        """Add one triangle of buffered vertices `idx` to the batch of the current material."""
        vs = [self.vtx[i] for i in idx]
        if any(v is None for v in vs):
            return
        # seen from straight above, a face is a front face iff its normal points up (counter-clockwise from above,
        # the game's winding); drop what the game's culling would (G_CULL_FRONT 0x1000, G_CULL_BACK 0x2000)
        a, b, c = (v[0] for v in vs)
        ny = (b[2] - a[2]) * (c[0] - a[0]) - (b[0] - a[0]) * (c[2] - a[2])
        if (self.geom & 0x2000 and ny <= 0) or (self.geom & 0x1000 and ny >= 0):
            self.culled += 1
            return
        tex, mode, const = self.material()
        key = (layer, tex, mode, const)
        b = self.scene.setdefault(key, {'pos': [], 'col': [], 'st': [], 'tile': None})
        if tex is not None and b['tile'] is None:
            t = self.tiles[self.tex_tile]
            b['tile'] = (t['uls'] / 4.0, t['ult'] / 4.0, self.tex_s, self.tex_t, t.get('shifts', 0), t.get('shiftt', 0))
        for p, c, st in vs:
            b['pos'].append(p)
            b['col'].append(c)
            b['st'].append(st)


# ------------------------------------------------------------------ the object rule
# Collision objects load a collision model (posts, cannon lids, platforms, doors that block); grabbable ones can be
# picked up (cork boxes). Fields are the US struct Object (decomp/include/types.h, object_fields.h).
INTERACT_GRABBABLE = 0x00000002
# interaction types of things that hurt or bounce Mario, i.e. enemies, even when they also load a collision model
# (Whomp, Thwomp, Bowser: INTERACT_DAMAGE)
INTERACT_HOSTILE = (0x00000008 | 0x00000080 | 0x00008000 | 0x00020000 | 0x00040000 | 0x00100000 | 0x00200000
                    | 0x10000000 | 0x20000000)    # DAMAGE KOOPA BOUNCE_TOP BULLY FLAME BOUNCE_TOP2 MR_BLIZZARD SNUFIT SHOCK
OBJ_FLAG_MOVE_XZ_USING_FVEL, OBJ_FLAG_MOVE_Y_WITH_TERMINAL_VEL, OBJ_FLAG_HOLDABLE = 0x2, 0x4, 0x400
OBJECT_DROPS = ('inactive', 'invisible', 'other area', 'no model', 'mario', 'enemy', 'animated', 'moving', 'held',
                'no collision', 'collectible')   # the reasons object_reason() gives for leaving an object out
OBJECT_KEEPS = ('collision', 'grabbable', 'static', 'any')
INTERACT_COLLECTIBLE = 0x00000010 | 0x00000020 | 0x00001000 | 0x00010000   # coin, cap, star or key, water ring
# What objects a map draws (a choice, default 'static'). Every mode leaves out Mario, inactive,
# invisible and other areas' objects; True means DEFAULT_OBJECT_MODE and False 'none' wherever `objects` is taken.
OBJECT_MODES = {
    'static': 'Collision objects + everything that doesn\'t move (default)',
    'scenery': 'Like the default, without collectibles (coins, stars, keys, caps, rings)',
    'grabbable': 'Collision objects + grabbable objects at rest',
    'collision': 'Collision objects only',
    'all': 'Every drawn object, enemies and moving ones too (rest pose)',
    'none': 'No objects',
}
DEFAULT_OBJECT_MODE = 'static'


def billboard_matrix(t, mtx):
    """A billboard node's matrix seen from straight above: mtxf_billboard turns sprites (trees, coins) to face the
    camera, so for a camera looking down with screen-up north they lie flat, facing up: local x -> +X, local y -> -Z,
    local z -> +Y, scaled as the parent, at the node's translation `t` through `mtx`."""
    pos = np.array([t[0], t[1], t[2], 1.0]) @ mtx
    s = float(np.linalg.norm(mtx[0, :3]))
    m = np.eye(4)
    m[0, :3], m[1, :3], m[2, :3], m[3, :3] = (s, 0, 0), (0, 0, -s), (0, s, 0), pos[:3]
    return m


def object_mode(objects):
    """`objects` as taken by decode / visual / render (True, False, None or a mode name) -> an OBJECT_MODES key."""
    if objects is True:
        return DEFAULT_OBJECT_MODE
    if not objects:
        return 'none'
    if objects not in OBJECT_MODES:
        raise ValueError(f'unknown object mode {objects!r} (choose from {", ".join(OBJECT_MODES)})')
    return objects


def object_rule(collision, interact_type=0, flags=0, animated=False, speed=0.0, held=False, mode='static'):
    """The object rule on plain values: 'collision', 'grabbable' (drawn) or the reason it is not. collision: it loads a
    collision model (oCollisionData / its behaviour's LOAD_COLLISION_DATA); interact_type: oInteractType; flags:
    oFlags; animated: it plays an animation; speed: horizontal speed oForwardVel / oVelX / oVelZ, or |oVelY| if
    larger; held: oHeldState is not HELD_FREE. ROM-side callers (romload, from the behaviour script) give
    collision from LOAD_COLLISION_DATA and interact_type / flags from the behaviour's SET_INT / OR_INT."""
    mode = object_mode(mode)
    if mode == 'none':
        return 'no collision'
    if collision:
        return 'enemy' if interact_type & INTERACT_HOSTILE and mode != 'all' else 'collision'
    if mode == 'collision':
        return 'no collision'
    if mode == 'all':
        return 'any'
    # Without collision ('static', the default since 2026-10-22; 'grabbable' was the rule before): any object the
    # game draws, unless it moves. Creatures (goombas, koopas, bob-ombs) are animated; objects built to move have the
    # move flags.
    if animated:                                    # creatures; boxes, trees, coins and signs aren't animated
        return 'animated'
    if flags & (OBJ_FLAG_MOVE_XZ_USING_FVEL | OBJ_FLAG_MOVE_Y_WITH_TERMINAL_VEL):
        return 'moving'                             # built to move, even if it stands still at this moment
    if held or speed > 1.0:
        return 'held' if held else 'moving'         # carried, thrown, sliding or flying now
    if interact_type & INTERACT_GRABBABLE or flags & OBJ_FLAG_HOLDABLE:
        return 'grabbable'
    if mode == 'grabbable':
        return 'no collision'
    if mode == 'scenery' and interact_type & INTERACT_COLLECTIBLE:
        return 'collectible'
    return 'static'


def object_reason(ram, n, mario_obj=None, area_index=None, mode='static'):
    """The object rule on the RAM object (or its GraphNodeObject) at n: 'collision' / 'grabbable' when the map draws
    it, else why not (OBJECT_DROPS)."""
    flags = ram.s16(n + 2) & 0xFFFF
    if n == mario_obj:
        return 'mario'
    if not ram.s16(n + 0x74) & 1:                           # activeFlags & ACTIVE_FLAG_ACTIVE
        return 'inactive'
    if flags & 0x10:                                        # GRAPH_RENDER_INVISIBLE
        return 'invisible'
    if area_index is not None and ram.raw(n + 0x19, 1)[0] != area_index:
        return 'other area'
    if not ram.u32(n + 0x14):                               # no model (sharedChild)
        return 'no model'
    f32 = ram.f32
    speed = max(abs(f32(n + 0xB8)), abs(f32(n + 0xAC)), abs(f32(n + 0xB4)), abs(f32(n + 0xB0)))
    return object_rule(collision=bool(ram.u32(n + 0x218)), interact_type=ram.u32(n + 0x130), flags=ram.u32(n + 0x8C),
                       animated=bool(ram.u32(n + 0x3C)), speed=speed, held=bool(ram.u32(n + 0x124)), mode=mode)


def keep_object(ram, n, mario_obj=None, area_index=None, mode='static'):
    """True when the map draws the RAM object at n (module docstring; object_rule has the details). Pass mario_obj
    (Ram.mario_object) and the area index (the area struct's first byte) to leave Mario and other areas' objects out."""
    return object_reason(ram, n, mario_obj, area_index, mode) not in OBJECT_DROPS


# ------------------------------------------------------------------ microcode detection
_TEXT_F3D = re.compile(rb'RSP SW Version: *2\.0')
_TEXT_GFX = re.compile(rb'RSP Gfx ucode ([A-Za-z0-9]+)(?:\.[A-Za-z0-9]+)? +(?:fifo|xbus) +(\d+)\.(\w+)')


def ucodes_in_text(data):
    """The set of microcode families whose version text `data` (bytes in N64 byte order) contains: the F3D text of the
    vanilla game ("RSP SW Version: 2.0D, 04-01-96") and the "RSP Gfx ucode <name> fifo|xbus <major>.<minor>" text of
    the later ones. F3DEX, F3DLX, F3DLP and L3DEX 1.x are F3DEX; version 2 and up (F3DEX2, F3DLX2, F3DZEX, the Zelda
    ones) are F3DEX2; the 2D microcodes (S2DEX) are ignored."""
    out = set()
    if _TEXT_F3D.search(data):
        out.add('F3D')
    for m in _TEXT_GFX.finditer(data):
        name, major = m.group(1).decode('ascii'), int(m.group(2))
        if not name.startswith(('F3D', 'L3D')):
            continue
        out.add('F3DEX2' if major >= 2 or name.endswith('2') or name.startswith('F3DZ') else 'F3DEX')
    return out


def _as_z64(data):
    """A ROM image in N64 (.z64) byte order, from .z64 / .v64 / .n64 bytes; anything else as it is."""
    h = bytes(data[:4])
    if h == b'\x37\x80\x40\x12':                             # .v64: byte-swapped pairs
        a = bytearray(data)
        a[0::2], a[1::2] = bytes(data[1::2]), bytes(data[0::2])
        return bytes(a)
    if h == b'\x40\x12\x37\x80':                             # .n64: little-endian words
        return np.frombuffer(bytes(data), dtype='<u4').byteswap().tobytes()
    return bytes(data)


def ucode_of_dls(ram, dls, max_commands=512):
    """The microcode the display lists `dls` (KSEG0 or segmented addresses) are written for, by their shape (STROOP
    Next's Detect): F3DEX2 ends a list with G_ENDDL 0xDF; F3D and F3DEX end with 0xB8 and an F3DEX list shows itself by
    a G_VTX with the doubled v0 and a length field equal to 16 n - 1, or a G_TRI2. The microcode with most lists wins
    (F3D on a tie)."""
    f3d = ex = ex2 = 0
    for dl in dls:
        if not dl:
            continue
        try:
            a = ram.va(dl)
            is_ex = False
            for _ in range(max_commands):
                w0 = ram.u32(a)
                a += 8
                op = w0 >> 24
                if w0 == 0xDF000000:
                    ex2 += 1
                    break
                if w0 == 0xB8000000:
                    ex += is_ex
                    f3d += not is_ex
                    break
                if op == 0x04 and (w0 & 0xF) == 0xF and ((w0 >> 10) & 0x3F) * 16 - 1 == (w0 & 0x3FF):
                    is_ex = True
                elif op == 0xB1:
                    is_ex = True
        except Exception:                                   # noqa: BLE001 - an unreadable list votes for nothing
            continue
    if ex2 > f3d and ex2 > ex:
        return 'F3DEX2'
    return 'F3DEX' if ex > f3d else 'F3D'


def graph_dls(ram, root=None):
    """Display-list addresses of the loaded area's geo graph (the lists the walker would run), for ucode_of_dls."""
    class _Rec(Gfx):
        def run(self, dl, mtx, layer, depth=0):
            self.dls.append(dl)
    if root is None:
        root = ram.u32(ram.u32(ram.layout['area']) + 0x04)
    w = Walker(ram, False)
    w.scheme = detect_scheme(ram, root)
    w.gfx = _Rec(ram, w.scene)
    w.gfx.dls = []
    w.walk(root, np.eye(4))
    return w.gfx.dls


def detect_ucode(src, dls=None):
    """'F3D' | 'F3DEX' | 'F3DEX2': the microcode a game's display lists are written for.
    `src` is raw memory or ROM bytes (any ROM byte order), a path to a .z64 / .n64 / .v64 / .st, a Ram (a savestate's
    RDRAM), or any object with a `rom_bytes` attribute (bytes of a ROM) or a `ucode` attribute (its answer).
    First the RSP microcode text (ucodes_in_text) in the memory or ROM; when that names exactly one family, that is the
    answer. When it names none or several (a ROM that carries more than one microcode), a Ram is decided by the display
    lists of its loaded area (or `dls`, a list of addresses), and bytes by the newest family named (F3D when none)."""
    data, ram = None, None
    if isinstance(src, (bytes, bytearray, memoryview)):
        data = _as_z64(src)
    elif isinstance(src, str):
        if src.lower().endswith('.st'):
            src = Ram(src)
        else:
            with open(src, 'rb') as f:
                data = _as_z64(f.read())
    if data is None:
        if getattr(src, 'ucode', None) in UCODES:
            return src.ucode
        rb = getattr(src, 'rom_bytes', None)
        if rb is not None:
            data = _as_z64(rb)
        elif getattr(src, 'st', None) is not None:          # a savestate: RDRAM in N64 byte order
            ram = src
            data = _rdram_words(src.st).byteswap().tobytes()
    found = ucodes_in_text(data) if data is not None else set()
    if len(found) == 1:
        return next(iter(found))
    if ram is not None and (dls is not None or hasattr(ram, 'layout')):
        try:
            return ucode_of_dls(ram, dls if dls is not None else graph_dls(ram))
        except Exception:                                   # noqa: BLE001 - no usable area: fall through
            pass
    for u in ('F3DEX2', 'F3DEX', 'F3D'):
        if u in found:
            return u
    return 'F3D'


# ------------------------------------------------------------------ graph node numbering
class GraphScheme:
    """How a build numbers its graph node types (and layers). Vanilla US and every ROM Manager hack use the decomp's
    values (T_ROOT = 1, ...). A decomp-built "hacker" family (The Hidden Stars here; found by hugou74130 for STROOP Next,
    GameScene.cs GraphScheme) numbers the types 0..21 in a different order, with the root as 20, and permutes the
    render layers: detect_scheme() recognises it by the root's type and maps both back to the vanilla values, so the
    walker has one set of constants."""
    TYPES = [T_ORTHO, T_PERSP, T_MASTER, T_LOD, T_SWITCH, T_CAMERA, T_TRANSROT, T_TRANS, T_ROT, T_OBJECT, T_ANIM,
             T_BILL, T_DL, T_SCALE, T_SHADOW, T_OBJPARENT, T_GENERATED, T_BACKGROUND, T_HELD, T_CULL, T_ROOT, T_START]
    LAYERS8 = [0, 1, 3, 2, 4, 6, 5, 7]
    LAYERS13 = [0, 1, 3, 2, 4, 4, 1, 4, 1, 4, 6, 5, 7]       # the hack's silhouette build has 13 layers
    ROOT, MASTER = 20, 2

    def __init__(self, hacker=False, layers=None):
        self.hacker, self.layers = hacker, layers

    def type(self, raw):
        """The decomp's node type for a graph node's raw type word (a rebuilt hack may number them differently)."""
        if not self.hacker:
            return raw
        return self.TYPES[raw] if 0 <= raw < len(self.TYPES) else -1

    def layer(self, raw):
        """The decomp's render layer for a node's raw layer number."""
        if self.layers is None:
            return raw
        return self.layers[raw] if raw < len(self.layers) else 1


def detect_scheme(ram, root):
    """The GraphScheme of the graph at `root`: the decomp's numbering, or the one a rebuilt hack uses."""
    if ram.s16(root) & 0xFFFF != GraphScheme.ROOT:
        return GraphScheme()
    layers = GraphScheme.LAYERS8
    first = c = ram.u32(root + 0x10)
    for _ in range(64):
        if not 0x80000000 <= c < 0x80800000:
            break
        if ram.s16(c) & 0xFFFF == GraphScheme.MASTER:
            if ram.u32(c + 0x10) - c == 0x14 + 8 * len(GraphScheme.LAYERS13):
                layers = GraphScheme.LAYERS13
            break
        c = ram.u32(c + 0x08)
        if c == first:
            break
    return GraphScheme(True, layers)


# ------------------------------------------------------------------ geo graph
class Walker:
    """Walks an area's geo graph in RAM (geo_process_* in rendering_graph_node.c), drawing its display lists."""
    def __init__(self, ram, objects=False, ucode='F3D'):
        self.object_mode = object_mode(objects)
        self.ram, self.objects = ram, self.object_mode != 'none'
        self.scene = {}
        self.gfx = Gfx(ram, self.scene, ucode)
        self.counts = {}
        self.skipped = {}
        self.depth_capped = 0                           # subtrees not walked: deeper than MAX_GRAPH_DEPTH
        self.nobj = 0
        self.kept = {}                                        # why each drawn object was kept (keep_object's reasons)
        self.dropped = {}                                     # why each undrawn one was not
        mo = getattr(ram, 'mario_object', None)                # gMarioObject
        self.mario_obj = ram.u32(0x80361158) if mo is None else mo
        self.area_index = 0
        self.scheme = GraphScheme()

    def children(self, first):
        """The sibling ring that starts at `first` (the node and its nexts, until it comes round)."""
        out, n = [], first
        for _ in range(4096):
            if not n:
                break
            out.append(n)
            n = self.ram.u32(n + 0x08)
            if n == first:
                break
        return out

    def walk(self, node, mtx, depth=0):
        """Draw `node`'s siblings and their subtrees under `mtx`."""
        ram = self.ram
        if depth > MAX_GRAPH_DEPTH:                     # a cycle in a damaged graph: stop instead of recursing forever
            self.depth_capped += 1
            return
        for n in self.children(node):
            typ, flags = self.scheme.type(ram.s16(n) & 0xFFFF), ram.s16(n + 2) & 0xFFFF
            self.counts[typ] = self.counts.get(typ, 0) + 1
            # GRAPH_RENDER_ACTIVE; an object's is cleared beyond its drawing distance, which a map ignores
            if not flags & 1 and typ != T_OBJECT:
                continue
            child = ram.u32(n + 0x10)
            layer = self.scheme.layer(flags >> 8)
            m = mtx
            dl = None
            if typ in (T_TRANSROT,):
                t = [ram.s16(n + 0x18 + 2 * i) for i in range(3)]
                r = [ram.s16(n + 0x1E + 2 * i) for i in range(3)]
                m, dl = mat_trs(t, r) @ mtx, ram.u32(n + 0x14)
            elif typ in (T_TRANS, T_ANIM, T_BILL):
                t = [ram.s16(n + 0x18 + 2 * i) for i in range(3)]
                m, dl = mat_trs(t) @ mtx, ram.u32(n + 0x14)
            elif typ == T_ROT:
                r = [ram.s16(n + 0x18 + 2 * i) for i in range(3)]
                m, dl = mat_trs((0, 0, 0), r) @ mtx, ram.u32(n + 0x14)
            elif typ == T_SCALE:
                m, dl = mat_trs((0, 0, 0), None, ram.f32(n + 0x18)) @ mtx, ram.u32(n + 0x14)
            elif typ == T_DL:
                dl = ram.u32(n + 0x14)
            elif typ == T_LOD:
                lo = ram.s16(n + 0x14)
                if lo > 0:                                    # only the range that starts at distance <= 0 (closest)
                    continue
            elif typ == T_SWITCH:
                kids = self.children(child)
                sel = ram.s16(n + 0x1E)
                if 0 <= sel < len(kids):
                    self.walk_one(kids[sel], m, depth + 1)
                continue
            elif typ == T_OBJPARENT and self.objects:
                shared = ram.u32(n + 0x14)
                if shared:
                    self.walk_one(shared, mtx, depth + 1)
                continue
            elif typ == T_OBJECT and self.objects:
                om = self.object_matrix(n)
                if om is not None:
                    self.nobj += 1
                    shared = ram.u32(n + 0x14)
                    if shared:
                        self.walk_one(shared, om, depth + 1)
                    if child:
                        self.walk(child, om, depth + 1)
                continue
            elif typ == T_BILL and self.objects:
                # sprites (trees, coins): mtxf_billboard turns them to face the camera, so seen from straight above
                # (screen-up north) they lie flat, facing up: local x -> +X, local y -> -Z, local z -> +Y
                m = billboard_matrix([ram.s16(n + 0x18 + 2 * i) for i in range(3)], mtx)
                dl = ram.u32(n + 0x14)
            elif typ in (T_BACKGROUND, T_OBJPARENT, T_HELD, T_SHADOW, T_OBJECT, T_GENERATED) and not self.objects:
                self.skipped[typ] = self.skipped.get(typ, 0) + 1
                continue
            if dl:
                self.gfx.geom = GEOM_INIT
                self.gfx.run(dl, m, layer)
            if child:
                self.walk(child, m, depth + 1)

    def object_matrix(self, n):
        """geo_process_object (rendering_graph_node.c): the object's own matrix, or None when keep_object leaves the
        object out of the map."""
        ram = self.ram
        why = object_reason(ram, n, self.mario_obj, self.area_index, self.object_mode)
        if why in OBJECT_DROPS:
            self.dropped[why] = self.dropped.get(why, 0) + 1
            return None
        self.kept[why] = self.kept.get(why, 0) + 1
        thr = ram.u32(n + 0x50)
        if thr:
            m = np.array(struct.unpack('>16f', ram.raw(thr, 64)), dtype=np.float64).reshape(4, 4)
        else:
            ang = [ram.s16(n + 0x1A + 2 * i) for i in range(3)]
            m = mat_trs([ram.f32(n + 0x20 + 4 * i) for i in range(3)], ang)
        sc = np.diag([ram.f32(n + 0x2C + 4 * i) for i in range(3)] + [1.0])
        return sc @ m

    def walk_one(self, n, mtx, depth):
        """One node without its siblings."""
        # temporarily treat as a list of one: walk children() would follow siblings, so wrap
        saved = self.children
        self.children = lambda first, _n=n, _s=saved: [_n] if first == _n else _s(first)
        try:
            self.walk(n, mtx, depth)
        finally:
            self.children = saved


# STROOP's map images' lighting, fitted on BoB.png (fit_light.py, 2026-10-18): shade = a + k max(0, n.L)
STROOP_LIGHT = (np.array([0.069, 0.609, -0.79]) / np.linalg.norm([0.069, 0.609, -0.79]), 0.66, 0.26)


def decode(state, objects=False, light=None, ucode=None):
    """(ram, walker) for the savestate `state`: its loaded area's geo graph run into walker.scene. `ucode`: 'F3D' |
    'F3DEX' | 'F3DEX2', default detect_ucode(ram). objects: True / False or an OBJECT_MODES key (what keep_object
    keeps)."""
    ram = Ram(state)
    area = ram.u32(ram.layout['area'])
    if not area:
        raise SystemExit(f'{state}: no current area')
    root = ram.u32(area + 0x04)
    ucode = ucode or detect_ucode(ram)
    w = Walker(ram, objects, ucode)
    w.ucode = ucode
    w.scheme = detect_scheme(ram, root)
    w.area_index = ram.raw(area, 1)[0]
    w.gfx.light_model = light
    w.walk(root, np.eye(4))
    return ram, w


def textures(ram, scene):
    """{tex key: RGBA array} for every textured batch."""
    out = {}
    for (layer, tex, mode, const) in scene:
        if tex is None or tex in out:
            continue
        addr, fmt, siz, w, h, tl, cms, cmt = tex
        pal = None
        if tl:
            pal = decode_tlut(ram, tl[0], 256)
            if tl[1] == 16:
                pal = pal[16 * tl[2]:16 * tl[2] + 16] if len(pal) >= 16 * tl[2] + 16 else pal[:16]
        try:
            out[tex] = decode_texture(ram, addr, fmt, siz, w, h, pal)
        except Exception:
            out[tex] = np.full((max(h, 1), max(w, 1), 4), (255, 0, 255, 255), dtype=np.uint8)
    return out


if __name__ == '__main__':
    ram, w = decode(sys.argv[1])
    print('node types', {hex(k): v for k, v in sorted(w.counts.items())})
    print('ucode', w.ucode, 'layout', ram.layout['source'], 'skipped', {hex(k): v for k, v in w.skipped.items()},
          'unknown ops', w.gfx.unknown, 'notes', w.gfx.notes)
    tot = 0
    for k, b in w.scene.items():
        n = len(b['pos']) // 3
        tot += n
    allp = np.array([p for b in w.scene.values() for p in b['pos']])
    print('batches', len(w.scene), 'tris', tot, 'layers', sorted({k[0] for k in w.scene}),
          'modes', sorted({k[2] for k in w.scene}))
    print('bounds', allp.min(0).round(), allp.max(0).round())
