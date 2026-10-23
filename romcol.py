"""Collision from ROM data: an area's TERRAIN (surface_load.c) and collision objects' collision models.

Part of the ROM reader (romload.py). Ports `load_area_terrain`, `load_static_surfaces`, `read_surface_data`,
`transform_object_vertices` and `load_object_surfaces` from the decomp (decomp/src/engine/surface_load.c).

Triangles come out in the `stroopmap.Area.tris` format: `(kind, surface_type, verts 3x3 float game units, normal)`
with kind 'floor' | 'wall' | 'ceil'. Under ROM Manager's extended boundaries (`shift` 2) the game keeps each vertex as
`v >> shift`, and STROOP (with "Use Extended Level Boundaries") draws it times 4, so the vertex returned is
`(v >> shift) << shift`, as `stroopmap.Area` has it. The normal is computed on the shifted-down vertices, as
`read_surface_data` does with the stored ones.

`ram` is anything with `raw(kseg0_address, n)` and `va(address)` (romload.RomRam).
"""
import math
import struct

import numpy as np

TERRAIN_LOAD_VERTICES, TERRAIN_LOAD_CONTINUE, TERRAIN_LOAD_END = 0x40, 0x41, 0x42
TERRAIN_LOAD_OBJECTS, TERRAIN_LOAD_ENVIRONMENT = 0x43, 0x44
FORCE_TYPES = {0x0004, 0x000E, 0x0024, 0x0025, 0x0027, 0x002C, 0x002D}     # surface_has_force()

# SpecialPreset type by preset id (decomp/include/special_presets.inc.c): how many s16 follow x, y, z
# (0: none, 1: y rotation, 2: y rotation + param, 3: three unknowns, 4: y rotation, default param)
_SPECIAL_TYPE = {0: 1, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: 0, 8: 1, 9: 0, 10: 0, 11: 0, 12: 0, 13: 0, 14: 1, 15: 0,
                 16: 0, 17: 0, 18: 0, 19: 0, 20: 0, 21: 0, 22: 0, 23: 0, 24: 0, 25: 0, 26: 0, 27: 0, 28: 0, 29: 0,
                 30: 3, 31: 0, 32: 0, 33: 0, 34: 0, 35: 1, 36: 1, 37: 0, 38: 0, 39: 0, 40: 0, 101: 1, 102: 1, 103: 1,
                 104: 1, 105: 1, 106: 1, 107: 1, 108: 1, 109: 1, 110: 1, 111: 1, 112: 1, 113: 1, 114: 1, 115: 1,
                 116: 1, 117: 1, 118: 1, 119: 1, 120: 1, 121: 0, 122: 0, 123: 0, 124: 0, 125: 0, 126: 1, 127: 1,
                 128: 1, 129: 1, 130: 1, 131: 2, 132: 2, 133: 2, 134: 2, 135: 2, 136: 2, 137: 1, 138: 4, 139: 4,
                 140: 4, 141: 4}
_SPECIAL_EXTRA = {0: 0, 1: 1, 2: 2, 3: 3, 4: 1}


class TerrainInfo:
    """What the terrain data holds besides the static triangles."""

    def __init__(self):
        self.special = []          # (preset id, x, y, z) of the special objects (doors, trees, ...), not placed
        self.water_boxes = 0
        self.unknown_special = False
        self.stopped = ''


def s16(ram, a):
    """The signed 16-bit word at `a`."""
    return struct.unpack('>h', ram.raw(a, 2))[0]


def normal_of(a, b, c):
    """read_surface_data's normal (v2 - v1) x (v3 - v2) on integer vertices, or None when degenerate."""
    nx = (b[1] - a[1]) * (c[2] - b[2]) - (b[2] - a[2]) * (c[1] - b[1])
    ny = (b[2] - a[2]) * (c[0] - b[0]) - (b[0] - a[0]) * (c[2] - b[2])
    nz = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
    mag = math.sqrt(nx * nx + ny * ny + nz * nz)
    if mag < 0.0001:
        return None
    return nx / mag, ny / mag, nz / mag


def kind_of(n):
    """A surface's kind from its normal, as surface_load.c sorts them."""
    return 'floor' if n[1] > 0.01 else 'ceil' if n[1] < -0.01 else 'wall'


def make_tri(ttype, v1, v2, v3, shift):
    """One Area.tris entry from the three vertices as the data holds them (None when the game skips it)."""
    n = normal_of(*([q >> shift for q in v] for v in (v1, v2, v3)))
    if n is None:
        return None
    v = np.array([[(c >> shift) << shift for c in p] for p in (v1, v2, v3)], dtype=np.float64)
    return (kind_of(n), ttype, v, n)


def parse_terrain(ram, va, shift, max_words=4_000_000):
    """The static triangles of an area's terrain data at `va` (a KSEG0 address): [Area.tris entries], TerrainInfo.
    Follows load_area_terrain: vertex lists, surface groups (type < 0x40 or >= 0x65), CONTINUE (keep reading), END,
    special objects (counted, skipped) and water boxes (counted)."""
    info = TerrainInfo()
    tris = []
    a = va
    verts = []
    end = va + 2 * max_words
    while a < end:
        cmd = s16(ram, a) & 0xFFFF
        a += 2
        if cmd == TERRAIN_LOAD_VERTICES:
            n = s16(ram, a)
            a += 2
            if n < 0 or n > 20000:
                info.stopped = f'bad vertex count {n}'
                break
            raw = ram.raw(a, 6 * n)
            verts = [struct.unpack('>hhh', raw[6 * i:6 * i + 6]) for i in range(n)]
            a += 6 * n
        elif cmd < 0x40 or cmd >= 0x65:
            n = s16(ram, a)
            a += 2
            step = 8 if cmd in FORCE_TYPES else 6
            if n < 0 or n > 20000:
                info.stopped = f'bad triangle count {n}'
                break
            raw = ram.raw(a, step * n)
            for i in range(n):
                ia, ib, ic = struct.unpack('>hhh', raw[step * i:step * i + 6])
                try:
                    t = make_tri(cmd, verts[ia], verts[ib], verts[ic], shift)
                except IndexError:
                    info.stopped = f'vertex index out of range in a type {cmd:#x} group'
                    t = None
                    break
                if t is not None:
                    tris.append(t)
            a += step * n
            if info.stopped:
                break
        elif cmd == TERRAIN_LOAD_CONTINUE:
            continue
        elif cmd == TERRAIN_LOAD_END:
            break
        elif cmd == TERRAIN_LOAD_OBJECTS:
            n = s16(ram, a)
            a += 2
            for _ in range(n):
                pid = s16(ram, a) & 0xFF
                x, y, z = struct.unpack('>hhh', ram.raw(a + 2, 6))
                a += 8
                t = _SPECIAL_TYPE.get(pid)
                if t is None:
                    info.unknown_special = True      # not in the vanilla table: the extra words are unknown
                    t = 0
                a += 2 * _SPECIAL_EXTRA[t]
                info.special.append((pid, x, y, z))
        elif cmd == TERRAIN_LOAD_ENVIRONMENT:
            n = s16(ram, a)
            a += 2 + 12 * n
            info.water_boxes += n
        else:
            info.stopped = f'unknown terrain command {cmd:#x}'
            break
    return tris, info


# ------------------------------------------------------------------ objects
_SIN = None


def _tables():
    global _SIN
    if _SIN is None:
        _SIN = np.sin(np.arange(4096 + 1024) * (2.0 * np.pi / 4096)).astype(np.float32)
    return _SIN


def sins(a):
    """sins() of the decomp: the sine table entry for angle `a`."""
    return float(_tables()[(a & 0xFFFF) >> 4])


def coss(a):
    """coss() of the decomp: the cosine table entry for angle `a`."""
    return float(_tables()[((a & 0xFFFF) >> 4) + 0x400])


def object_matrix(pos, ang, scale):
    """The object's transform as the game builds it (mtxf_rotate_zxy_and_translate, then obj_apply_scale_to_matrix),
    row-vector convention, float32: rows 0-2 times the scale, row 3 the position."""
    sx, cx = sins(ang[0]), coss(ang[0])
    sy, cy = sins(ang[1]), coss(ang[1])
    sz, cz = sins(ang[2]), coss(ang[2])
    f = np.float32
    m = np.zeros((4, 4), dtype=np.float32)
    m[0][0] = f(cy) * f(cz) + f(sx) * f(sy) * f(sz)
    m[1][0] = -f(cy) * f(sz) + f(sx) * f(sy) * f(cz)
    m[2][0] = f(cx) * f(sy)
    m[3][0] = pos[0]
    m[0][1] = f(cx) * f(sz)
    m[1][1] = f(cx) * f(cz)
    m[2][1] = -f(sx)
    m[3][1] = pos[1]
    m[0][2] = -f(sy) * f(cz) + f(sx) * f(cy) * f(sz)
    m[1][2] = f(sy) * f(sz) + f(sx) * f(cy) * f(cz)
    m[2][2] = f(cx) * f(cy)
    m[3][2] = pos[2]
    m[3][3] = 1.0
    for i in range(3):
        m[i, :3] *= f(scale[i])
    return m


def object_tris(ram, va, pos, ang, scale, shift):
    """The triangles of one collision object: its collision model (the data a LOAD_COLLISION_DATA command names, at
    `va`) with the vertices turned and moved to the object (load_object_collision_model), truncated to s16 as the
    game stores them. Returns [Area.tris entries]."""
    a = va + 2                                       # skip TERRAIN_LOAD_VERTICES
    n = s16(ram, a)
    a += 2
    if n < 0 or n > 600:
        return []
    m = object_matrix(pos, ang, scale)
    raw = ram.raw(a, 6 * n)
    src = np.array([struct.unpack('>hhh', raw[6 * i:6 * i + 6]) for i in range(n)], dtype=np.float32).reshape(-1, 3)
    out = (src[:, 0:1] * m[0, :3] + src[:, 1:2] * m[1, :3] + src[:, 2:3] * m[2, :3] + m[3, :3])
    verts = [tuple(int(np.clip(np.trunc(c), -32768, 32767)) for c in row) for row in out]
    a += 6 * n
    tris = []
    for _ in range(64):
        cmd = s16(ram, a) & 0xFFFF
        if cmd == TERRAIN_LOAD_CONTINUE or cmd == TERRAIN_LOAD_END:
            break
        k = s16(ram, a + 2)
        a += 4
        step = 8 if cmd in FORCE_TYPES else 6
        if k < 0 or k > 2000:
            break
        rawt = ram.raw(a, step * k)
        for i in range(k):
            ia, ib, ic = struct.unpack('>hhh', rawt[step * i:step * i + 6])
            if max(ia, ib, ic) >= n:
                continue
            t = make_tri(cmd, verts[ia], verts[ib], verts[ic], shift)
            if t is not None:
                tris.append(t)
        a += step * k
    return tris
