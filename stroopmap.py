"""Top-down collision maps in STROOP's Map tab format, from a savestate taken after the level has loaded.

Reads the area's static collision with sm64io.load_state_collision (ROM Manager's 4x extended boundaries detected
from the floor under Mario), renders floors shaded by height and slope, walls as dark edges
(ceilings optional), and writes one PNG per floor band plus the <Map> entries for STROOP's Config/MapAssociations.xml.

Geometry is the collision the game holds: each vertex as the loader keeps it, (v >> shift) << shift, which is what
STROOP draws with "Use Extended Level Boundaries" ticked (it multiplies the RAM vertices by 4).

Image convention (STROOP MapMapObject.GetDimensions: a quad centred on the rectangle, the texture across it):
north-up (-Z at the top), pixel (0, 0)'s outer corner at world (x1, z1), every pixel `scale` units square, and
x1/x2/z1/z2 the outer pixel edges.

    python stroopmap.py STATE.st --slug castle --name "Island Castle" --level 6 --area 1 --out DIR
"""
import argparse, math, os, sys
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sm64io                                      # noqa: E402 - savestates and their collision

DEATH_PLANE = 0x0A
MAX_PX = 8192                                     # longest side; past it the scale doubles (STROOP's limit is 16384;
                                                  # a full 4x-ELB level at 4 u/px would be 16k px and GBs to render)
SS_BUDGET = 128_000_000                           # supersampled pixels before render() drops to ss=1
WARP_DEST = 0x8033B248                            # sWarpDest (US/Island): STROOP keys maps on its level and area bytes

# height colour ramp (low -> high): deep blue, teal, green, yellow, orange, pale
RAMP = np.array([(38, 60, 140), (32, 128, 160), (60, 168, 96), (200, 200, 70), (225, 130, 50), (245, 225, 210)],
                dtype=np.float64)
WALL_RGB = (20, 20, 24)
DEATH_RGB = (110, 20, 20)
CEIL_RGBA = (60, 60, 200, 90)


# ------------------------------------------------------------------ loading
class Area:
    """The area's static triangles as the game holds them, plus what STROOP keys the map on."""

    def __init__(self, path):
        lv = sm64io.load_state_collision(path)
        st = sm64io.Savestate(path)
        import geodecode                              # gCurrentArea's address: US, or found by scan (rebuilt hacks)
        layout = geodecode.detect_layout(st)
        ram_area = st.u32(layout['area'])
        if not ram_area or st.u32(st.mario_state + 0x90) != ram_area:
            raise SystemExit(f'{path}: Mario\'s area is not the loaded area (taken before the level loaded?)')
        if len(lv.tris) < 8:
            raise SystemExit(f'{path}: only {len(lv.tris)} triangles; take the state after the level has loaded')
        self.path, self.shift, self.notes = path, lv.elb, list(lv.notes or [])
        if layout.get('source') == 'us':
            self.level = st.bytes(WARP_DEST + 1, 1)[0]
            self.area = st.bytes(WARP_DEST + 2, 1)[0]
        else:
            # a rebuilt hack moved sWarpDest; STROOP reads every address at its US place, so it can't follow these
            # areas anyway: the area index from the area struct, the level unknown (0)
            self.level, self.area = 0, st.bytes(ram_area, 1)[0]
            self.notes.append('rebuilt RAM layout: the level number is unknown and STROOP (US addresses) can\'t '
                              'pick this map by itself')
        self.mario = lv.mario
        s = self.shift
        self.tris = []                                # (kind, type, verts (3x3 float), normal)
        for kind, ttype, verts, n, _ in lv.tris:
            v = np.array([[(c >> s) << s for c in p] for p in verts], dtype=np.float64)
            self.tris.append((kind, ttype, v, n))

    def of(self, kind, death=None):
        """The area's triangles of one kind (tris_of)."""
        return tris_of(self, kind, death)


def tris_of(area, kind, death=None):
    """The triangles of `kind` ('floor' | 'wall' | 'ceil') of any area source (death planes: True only, False
    without, None both)."""
    out = [t for t in area.tris if t[0] == kind]
    if death is not None:
        out = [t for t in out if (t[1] == DEATH_PLANE) == death]
    return out


# ------------------------------------------------------------------ bands
def find_bands(area, cell=32.0, min_sep=200.0, min_area=256 * 256, min_frac=0.01):
    """Split heights where floors stack: on a `cell`-unit grid, every pair of consecutive floor heights more than
    `min_sep` apart gives an interval (lower, upper]; the fewest heights that
    stab every interval (greedy by upper end) are the band starts. Each start is the lowest upper floor of its
    group, so a floor at height h is drawn in the band that holds y = h, and anything from the start up is clipped
    out of the band below. A split must separate at least max(`min_area`, `min_frac` of the area's floor) square
    units, so a ledge over a floor doesn't make a band. Returns [(split y, square units it separates)], ascending."""
    floors = tris_of(area, 'floor', death=False)
    if not floors:
        return []
    allv = np.concatenate([t[2] for t in floors])
    x0, z0 = allv[:, 0].min(), allv[:, 2].min()
    nx = int((allv[:, 0].max() - x0) // cell) + 1
    nz = int((allv[:, 2].max() - z0) // cell) + 1
    cells, heights = [], []
    for _, _, v, _ in floors:
        ix, iz, h = _raster(v, x0, z0, cell, nx, nz)
        cells.append(iz * nx + ix)
        heights.append(h)
    c = np.concatenate(cells)
    h = np.round(np.concatenate(heights))
    order = np.lexsort((h, c))
    c, h = c[order], h[order]
    min_area = max(min_area, min_frac * len(np.unique(c)) * cell * cell)
    same = c[1:] == c[:-1]
    gap = (h[1:] - h[:-1]) > min_sep
    pick = same & gap
    lo, hi = h[:-1][pick], h[1:][pick]
    if not len(lo):
        return []
    pairs, counts = np.unique(np.stack([lo, hi], 1), axis=0, return_counts=True)
    keep = [(float(a), float(b), float(n) * cell * cell) for (a, b), n in zip(pairs, counts)]
    splits = []
    for a, b, _ in sorted(keep, key=lambda t: t[1]):
        if any(a < s <= b for s, _ in splits):
            continue
        # a split at b serves every interval it stabs; skip it when that is less than min_area of floor in all
        served = sum(w for a2, b2, w in keep if a2 < b <= b2 and not any(a2 < s <= b2 for s, _ in splits))
        if served < min_area:
            continue
        splits.append((b, served))
    return sorted(splits)


# ------------------------------------------------------------------ rasterising
def _raster(v, x0, z0, step, nx, nz, top=None):
    """Grid samples (centres x0 + (i + 0.5) * step) inside the triangle's xz projection, with the triangle's plane
    height there. The inside test is inclusive, as the game's floor checks are. Returns (ix, iz, h)."""
    xs, zs = v[:, 0], v[:, 2]
    i0 = max(int(math.floor((xs.min() - x0) / step - 0.5)), 0)
    i1 = min(int(math.ceil((xs.max() - x0) / step - 0.5)), nx - 1)
    j0 = max(int(math.floor((zs.min() - z0) / step - 0.5)), 0)
    j1 = min(int(math.ceil((zs.max() - z0) / step - 0.5)), nz - 1)
    if i1 < i0 or j1 < j0:
        return (np.zeros(0, int),) * 2 + (np.zeros(0),)
    ii, jj = np.meshgrid(np.arange(i0, i1 + 1), np.arange(j0, j1 + 1))
    px = x0 + (ii + 0.5) * step
    pz = z0 + (jj + 0.5) * step
    (ax, ay, az), (bx, by, bz), (cx, cy, cz) = v
    e0 = (bx - ax) * (pz - az) - (bz - az) * (px - ax)
    e1 = (cx - bx) * (pz - bz) - (cz - bz) * (px - bx)
    e2 = (ax - cx) * (pz - cz) - (az - cz) * (px - cx)
    inside = ((e0 >= 0) & (e1 >= 0) & (e2 >= 0)) | ((e0 <= 0) & (e1 <= 0) & (e2 <= 0))
    n = np.cross(v[1] - v[0], v[2] - v[0])
    if abs(n[1]) < 1e-9:
        return (np.zeros(0, int),) * 2 + (np.zeros(0),)
    hgt = ay - (n[0] * (px - ax) + n[2] * (pz - az)) / n[1]
    if top is not None:
        inside &= hgt < top
    return ii[inside], jj[inside], hgt[inside]


class Frame:
    """The image rectangle, `margin` units around the geometry, with pixel centres on multiples of `scale` (outer
    edges at odd multiples of scale / 2). The default scale is the loader's vertex grid under extended boundaries
    (4 units for ROM Manager's 4x), so every vertex sits on a pixel centre and an axis-aligned wall is drawn centred
    in its pixel; vanilla levels get 8 units."""

    def __init__(self, area, scale, margin):
        scale = scale or ((1 << area.shift) if area.shift else 8.0)
        solid = [t for t in area.tris if t[1] != DEATH_PLANE]
        allv = np.concatenate([t[2] for t in solid])
        lo = allv.min(0) - margin
        hi = allv.max(0) + margin
        while True:
            self.x1 = math.floor(lo[0] / scale + 0.5) * scale - scale / 2
            self.z1 = math.floor(lo[2] / scale + 0.5) * scale - scale / 2
            self.w = int(math.ceil((hi[0] - self.x1) / scale))
            self.h = int(math.ceil((hi[2] - self.z1) / scale))
            if max(self.w, self.h) <= MAX_PX:
                break
            scale *= 2
        self.scale = scale
        self.x2 = self.x1 + self.w * scale
        self.z2 = self.z1 + self.h * scale


def height_range(area, frame):
    """The colour ramp's ends for every band of an area: the 0.5 and 99.5 percentiles of the floor heights seen
    from above (death planes left out), on a grid 4 pixels wide, so a floor has the same colour in every band."""
    step = frame.scale * 4
    W, H = -(-frame.w // 4), -(-frame.h // 4)
    zbuf = np.full((H, W), -np.inf)
    for _, _, v, _ in tris_of(area, 'floor', death=False):
        ix, iz, h = _raster(v, frame.x1, frame.z1, step, W, H)
        np.maximum.at(zbuf, (iz, ix), h)
    hs = zbuf[np.isfinite(zbuf)]
    if not len(hs):
        return 0.0, 1.0
    return float(np.percentile(hs, 0.5)), float(np.percentile(hs, 99.5))


def render(area, frame, top=None, hrange=(0.0, 1.0), ss=2, ceilings=False, death_planes=False):
    """One band's image: floors below `top` by height (on the ramp from hrange[0] to hrange[1]) and slope, walls as
    dark edges where no floor covers them, death planes (optional) dark red and half transparent; nothing is
    transparent. Returns (PIL RGBA image, (lowest, highest) floor height drawn)."""
    if frame.w * frame.h * ss * ss > SS_BUDGET:
        ss = 1                                    # a huge map: supersampling would need several GB
    step = frame.scale / ss
    W, H = frame.w * ss, frame.h * ss
    zbuf = np.full((H, W), -np.inf)
    tid = np.full((H, W), -1, dtype=np.int32)
    floors = tris_of(area, 'floor')
    for k, (_, ttype, v, _) in enumerate(floors):
        ix, iz, h = _raster(v, frame.x1, frame.z1, step, W, H, top)
        if not len(ix):
            continue
        better = h > zbuf[iz, ix]
        ix, iz, h = ix[better], iz[better], h[better]
        zbuf[iz, ix] = h
        tid[iz, ix] = k

    # float64 keeps the published maps bit-identical; float32 (rounding: at most 1 level) only past 4 GB
    rgba = np.zeros((H, W, 4), dtype=np.float64 if H * W * 32 <= 4 << 30 else np.float32)
    have = tid >= 0
    death = np.array([t[1] == DEATH_PLANE for t in floors] + [False])
    solid = have & ~death[tid]
    if solid.any():
        hs = zbuf[solid]
        lo, hi = hrange[0], max(hrange[1], hrange[0] + 1)
        t = np.clip((zbuf[solid] - lo) / (hi - lo), 0, 1) * (len(RAMP) - 1)
        k = np.minimum(t.astype(int), len(RAMP) - 2)
        f = (t - k)[:, None]
        base = RAMP[k] * (1 - f) + RAMP[k + 1] * f
        # slope: hill shade from the north-west (light from -X, -Z, up), plus a darkening with steepness
        normals = np.array([t[3] for t in floors] + [(0, 1, 0)])
        nrm = normals[tid[solid]]
        light = np.array([-0.45, 0.75, -0.45])
        light /= np.linalg.norm(light)
        shade = np.clip(nrm @ light, 0, 1) / light[1]          # 1 on flat ground
        shade = np.clip(shade, 0.45, 1.15) * (0.7 + 0.3 * nrm[:, 1])
        rgba[solid, :3] = np.clip(base * shade[:, None], 0, 255)
        rgba[solid, 3] = 255
        drawn = (float(hs.min()), float(hs.max()))
    else:
        drawn = None
    if death_planes:
        dmask = have & death[tid]
        rgba[dmask, :3] = DEATH_RGB
        rgba[dmask, 3] = 160

    if ceilings:
        for _, _, v, _ in tris_of(area, 'ceil'):
            ix, iz, h = _raster(v, frame.x1, frame.z1, step, W, H, top)
            vis = h > zbuf[iz, ix]
            a = CEIL_RGBA[3] / 255.0
            sel = (iz[vis], ix[vis])
            rgba[sel[0], sel[1], :3] = rgba[sel[0], sel[1], :3] * (1 - a) + np.array(CEIL_RGBA[:3]) * a
            rgba[sel[0], sel[1], 3] = np.maximum(rgba[sel[0], sel[1], 3], CEIL_RGBA[3])

    # walls: every edge's projection, sampled at a quarter sample, drawn where its height is below the band top and
    # not under a floor (height within 8 units of the top floor there or above it); marked on the output grid, so a
    # wall pixel is one that an edge passes through
    wall = np.zeros((frame.h, frame.w), bool)
    for _, _, v, _ in tris_of(area, 'wall'):
        if top is not None and v[:, 1].min() >= top:
            continue
        for a, b in ((0, 1), (1, 2), (2, 0)):
            p, q = v[a], v[b]
            L = math.hypot(q[0] - p[0], q[2] - p[2])
            n = max(int(L / (step / 4)) + 1, 2)
            t = np.linspace(0, 1, n)
            x = p[0] + (q[0] - p[0]) * t
            z = p[2] + (q[2] - p[2]) * t
            y = p[1] + (q[1] - p[1]) * t
            ix = np.floor((x - frame.x1) / step).astype(int)
            iz = np.floor((z - frame.z1) / step).astype(int)
            ok = (ix >= 0) & (ix < W) & (iz >= 0) & (iz < H)
            if top is not None:
                ok &= y < top
            ix, iz, y = ix[ok], iz[ok], y[ok]
            vis = y >= zbuf[iz, ix] - 8
            wall[iz[vis] // ss, ix[vis] // ss] = True

    # box filter down to the output grid (alpha-weighted colour)
    rgba = rgba.reshape(frame.h, ss, frame.w, ss, 4)
    a = rgba[..., 3:4]
    asum = a.sum(axis=(1, 3))
    col = (rgba[..., :3] * a).sum(axis=(1, 3)) / np.maximum(asum, 1e-9)
    out = np.concatenate([col, asum / (ss * ss)], axis=-1)
    out[wall] = WALL_RGB + (255,)
    return Image.fromarray(np.clip(np.round(out), 0, 255).astype(np.uint8), 'RGBA'), drawn


# ------------------------------------------------------------------ STROOP entries
def map_element(map_id, name, sub, level, area, img_path, size, frame, y=None, background='Blue Background',
                hack=None, kind=None):
    """One <Map>. `hack` and `kind` ('collision' | 'textured') are the tags the patched STROOP's "Recommended" reads;
    stock STROOP ignores unknown attributes."""
    m = ET.Element('Map', {'name': name, 'id': map_id, 'level': str(level), 'area': str(area)})
    if sub:
        m.set('subName', sub)
    if hack:
        m.set('hack', hack)
    if kind:
        m.set('kind', kind)
    ET.SubElement(m, 'Image', {'path': img_path, 'sizeX': str(size[0]), 'sizeY': str(size[1])})
    c = {'x1': _num(frame.x1), 'x2': _num(frame.x2), 'z1': _num(frame.z1), 'z2': _num(frame.z2)}
    if y is not None:
        c['y'] = _num(y)
    ET.SubElement(m, 'Coordinates', c)
    if background:
        # STROOP's parser reads name; the xsd requires path (its file in Background Images)
        ET.SubElement(m, 'BackgroundImage', {'name': background, 'path': background + '.png'})
    return m


def _num(v):
    return str(int(v)) if float(v).is_integer() else repr(float(v))


def plan_area(ar, scale=None, margin=64.0, bands='auto', min_sep=200.0, min_area=256 * 256, min_frac=0.01):
    """The image rectangle and the band splits of an area source: (Frame, [split y, ...])."""
    frame = Frame(ar, scale, margin)
    if bands == 'auto':
        splits = [s for s, _ in find_bands(ar, min_sep=min_sep, min_area=min_area, min_frac=min_frac)]
    elif bands in ('none', '', None):
        splits = []
    else:
        splits = sorted(float(s) for s in str(bands).split(','))
    return frame, splits


def as_area(state):
    """An area source: `state` itself when it already is one (it has .tris), else the savestate at that path."""
    return state if hasattr(state, 'tris') else Area(state)


def build(state, slug, name, out, subfolder='SM64 Island', id_prefix=None, scale=None, margin=64.0, ss=2,
          ceilings=False, death_planes=False, bands='auto', background='Blue Background', min_sep=200.0,
          min_area=256 * 256, min_frac=0.01, level=None, area_index=None, log=print, cancel=None, plan=None,
          on_image=None):
    """Render every band of one area. `state` is a savestate path or an area source (sources.StateSource,
    romload.RomAreaSource). Returns (list of <Map> elements, info dict); a True `cancel()` between bands stops it
    (info['cancelled']). `plan` is plan_area's (Frame, splits) when the caller already has it; on_image(path) is
    called after each PNG."""
    ar = as_area(state)
    level = ar.level if level is None else level
    area_index = ar.area if area_index is None else area_index
    frame, splits = plan or plan_area(ar, scale, margin, bands, min_sep, min_area, min_frac)
    edges = [None] + splits + [None]
    hrange = height_range(ar, frame)
    os.makedirs(os.path.join(out, subfolder), exist_ok=True)
    id_prefix = id_prefix or f'I{level:02d}{area_index}'
    elems, info = [], {'state': getattr(ar, 'path', None) or str(state), 'shift': ar.shift, 'level': level,
                       'area': area_index, 'scale': frame.scale,
                       'bounds': (frame.x1, frame.x2, frame.z1, frame.z2), 'size': (frame.w, frame.h),
                       'splits': splits, 'colour_range': hrange, 'bands': []}
    for b in range(len(edges) - 1):
        if cancel and cancel():
            info['cancelled'] = True
            break
        lo, hi = edges[b], edges[b + 1]
        img, drawn = render(ar, frame, hi, hrange, ss, ceilings, death_planes)
        multi = len(edges) > 2
        fname = f'{slug}_{b + 1}.png' if multi else f'{slug}.png'
        img.save(os.path.join(out, subfolder, fname), optimize=True)
        if on_image:
            on_image(os.path.join(out, subfolder, fname))
        sub = f'Floor {b + 1}' if multi else None      # STROOP adds " (y ≥ Y)" itself
        rel = f'{subfolder}\\{fname}' if subfolder else fname
        elems.append(map_element(f'{id_prefix}{chr(65 + b)}', name, sub, level, area_index, rel,
                                 (frame.w, frame.h), frame, lo, background))
        info['bands'].append({'file': fname, 'y': lo, 'top': hi, 'heights': drawn})
        log(f'{fname}: {frame.w}x{frame.h} px, {frame.scale:g} u/px, y from {lo} to {hi}, floors drawn {drawn}')
    return elems, info


def write_snippet(path, elems, extra_elems=()):
    """The <Map> entries, indented as in STROOP's file, ready to paste after its last <Map>. `extra_elems` (a
    <MapHack>, see exporter.py) are written first."""
    lines = ['<!-- Paste these <Map> entries into STROOP\'s Config/MapAssociations.xml, after the last <Map>.',
             '     Copy the PNG folder into Resources\\Maps\\Map Images\\. -->']
    for e in list(extra_elems) + list(elems):
        ET.indent(e, '  ')
        lines +=['  ' + s for s in ET.tostring(e, encoding='unicode').splitlines()]
    with open(path, 'w', encoding='utf-8', newline='\r\n') as f:
        f.write('\n'.join(lines) + '\n')


def main():
    """The command line: one area from a savestate (see --help)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('state')
    ap.add_argument('--slug', required=True, help='file name stem, e.g. castle')
    ap.add_argument('--name', required=True, help="the map's name in STROOP's dropdown")
    ap.add_argument('--out', default=os.path.join(HERE, 'work', 'maps'))
    ap.add_argument('--subfolder', default='SM64 Island', help='folder under Map Images (default "SM64 Island")')
    ap.add_argument('--level', type=int, help='override the level number read from sWarpDest')
    ap.add_argument('--area', type=int, help='override the area number read from sWarpDest')
    ap.add_argument('--id', help='map id prefix (default I<level><area>); the band letter is appended')
    ap.add_argument('--scale', type=float, help='units per pixel (default: the vertex grid, 4 under ROM Manager '
                                                 "ELB, else 8; STROOP's vanilla BoB is ~9)")
    ap.add_argument('--margin', type=float, default=64.0)
    ap.add_argument('--ss', type=int, default=2, help='supersampling per axis')
    ap.add_argument('--ceilings', action='store_true', help='tint ceilings no floor covers')
    ap.add_argument('--death-planes', action='store_true', help='draw death planes (dark red, half transparent)')
    ap.add_argument('--bands', default='auto', help='"auto" (from the floor heights), "none", or y1,y2,...')
    ap.add_argument('--min-sep', type=float, default=200.0)
    ap.add_argument('--min-area', type=float, default=256 * 256, help='square units a band split must separate')
    ap.add_argument('--min-frac', type=float, default=0.01, help="... or this fraction of the area's floor, if more")
    ap.add_argument('--background', default='Blue Background')
    a = ap.parse_args()
    elems, info = build(a.state, a.slug, a.name, a.out, a.subfolder, a.id, a.scale, a.margin, a.ss, a.ceilings,
                        a.death_planes, a.bands, a.background, a.min_sep, a.min_area, a.min_frac, a.level, a.area)
    write_snippet(os.path.join(a.out, f'{a.slug}.xml'), elems)
    print(info)


if __name__ == '__main__':
    main()
