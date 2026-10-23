"""Stage 2: a textured top-down map of one area (geodecode.py -> Blender), in stage 1's frame and bands.

    python render_textured.py STATE.st --slug castle --out DIR [--frame-from ../../out/stroopmaps/maps.json]
                              [--no-textures] [--check] [--samples 16]

The frame (bounds, 4 u/px with vertices on pixel centres) and the bands come from stage 1: from maps.json's entry for
the slug when given, else computed from the state's collision the same way (stroopmap.Frame, find_bands). --check
also renders a height pass and compares it with the collision's topmost floor per pixel (coverage, height
agreement, best-fit shift), writing <slug>_check.json.
"""
import argparse, glob, json, os, re, shutil, subprocess, sys, time
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import geodecode                                   # noqa: E402
import stroopmap                                   # noqa: E402


class BlenderMissing(RuntimeError):
    """Textured maps are rendered in headless Blender; none was found."""


class Cancelled(Exception):
    """cancel() returned True while Blender was running (its process was ended)."""


def _version_key(path):
    m = re.search(r'Blender (\d+)\.(\d+)', path)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def find_blender():
    """Path of blender.exe, or '' when there is none. Order: the configured path (sm64io.machine_path), the
    environment variable BLENDER, `blender` on PATH, then Blender's standard install folders (the newest version)."""
    import sm64io
    cands = [sm64io.machine_path('blender')]
    cands.append(os.environ.get('BLENDER', ''))
    cands.append(shutil.which('blender') or '')
    for c in cands:
        if c and os.path.isfile(c):
            return c
    roots = [os.environ.get(v, '') for v in ('ProgramFiles', 'ProgramW6432', 'ProgramFiles(x86)')]
    roots.append(os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs'))
    found = []
    for r in filter(None, roots):
        for sub in ('Blender Foundation', ''):
            found += glob.glob(os.path.join(r, sub, 'Blender *', 'blender.exe'))
    found = sorted(set(found), key=_version_key, reverse=True)
    return found[0] if found else ''


# The Blender used. The GUI sets it when one is chosen there; blender_exe() falls back to find_blender().
BLENDER = find_blender()


def blender_exe():
    """The Blender to run; BlenderMissing, with the message the GUI shows, when there is none."""
    if BLENDER and os.path.isfile(BLENDER):
        return BLENDER
    found = find_blender()
    if found:
        return found
    raise BlenderMissing('Blender was not found, so textured maps cannot be made (collision maps still work). '
                         'Install Blender 5.2 (blender.org), choose blender.exe in the window, or name it in the '
                         'STROOPMAPS_BLENDER or BLENDER environment variable.')


def shift_factor(sh):
    """The scale a tile shift applies to texture coordinates (G_SETTILE: 1-10 divide, 11-15 multiply)."""
    return 1.0 if sh == 0 else 1.0 / (1 << sh) if sh <= 10 else float(1 << (16 - sh))


def relief(pos):
    """Stage 1's hill shade per triangle (light from the north-west, darker with steepness), per corner."""
    v = pos.reshape(-1, 3, 3)
    n = np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 0])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-9)
    n *= np.where(n[:, 1:2] < 0, -1, 1)
    light = np.array([-0.45, 0.75, -0.45])
    light /= np.linalg.norm(light)
    f = np.clip(np.clip(n @ light, 0, 1) / light[1], 0.45, 1.15) * (0.7 + 0.3 * n[:, 1])
    return np.repeat(f, 3)


def export(ram, scene, work, shade_relief=False):
    """npz of every batch + the material list; textures as PNGs (mirrored axes unrolled)."""
    texdir = os.path.join(work, 'tex')
    os.makedirs(texdir, exist_ok=True)
    texs = geodecode.textures(ram, scene)
    arrays, mats, ys = {}, [], []
    for i, ((layer, tex, mode, const), b) in enumerate(sorted(scene.items(), key=lambda kv: kv[0][0])):
        pos = np.array(b['pos'], dtype=np.float64)
        col = np.array(b['col'], dtype=np.float64)
        st = np.array(b['st'], dtype=np.float64)
        uv = np.zeros((len(pos), 2))
        if shade_relief and len(pos):
            col[:, :3] = np.clip(col[:, :3] * relief(pos)[:, None], 0, 255)
        m = {'layer': layer, 'shade': 'shade' in mode, 'const': list(const[:3]) if const else None}
        if tex is not None:
            addr, fmt, siz, w, h, tl, cms, cmt = tex
            img = texs[tex]
            uls, ult, ss, ts, shs, sht = b['tile']
            s = st[:, 0] / 32.0 * ss * shift_factor(shs) - uls
            t = st[:, 1] / 32.0 * ts * shift_factor(sht) - ult
            ww, hh = w, h
            if cms & 1 and not cms & 2:
                img = np.concatenate([img, img[:, ::-1]], 1)
                ww = 2 * w
            if cmt & 1 and not cmt & 2:
                img = np.concatenate([img, img[::-1]], 0)
                hh = 2 * h
            # the N64's bilinear filter has texel centres on integer coordinates; Blender's on i + 0.5
            uv[:, 0] = (s + 0.5) / ww
            uv[:, 1] = 1.0 - (t + 0.5) / hh
            p = os.path.join(texdir, f'{addr:08X}_{fmt}{siz}_{w}x{h}_{cms}{cmt}.png')
            if not os.path.exists(p):
                Image.fromarray(img, 'RGBA').save(p)
            m['tex'] = p
            m['clamp'] = bool(cms & 2 and cmt & 2)
        arrays[f'p{i}'], arrays[f'c{i}'], arrays[f'u{i}'] = pos, col, uv
        mats.append(m)
        ys.append(pos[:, 1])
    npz = os.path.join(work, 'scene.npz')
    np.savez(npz, **arrays)
    ys = np.concatenate(ys)
    return npz, mats, float(ys.min()), float(ys.max())


def frame_and_bands(state, slug, frame_from):
    """({x1, x2, z1, z2, scale, w, h}, [split y]) of an area: from a maps.json path (the entry for `slug`) or an entry
    dict ({'bounds', 'scale', 'splits'}, what stroopmap.build returns), else computed from the collision the way
    stage 1 does. `state` is a savestate path or an area source."""
    if frame_from:
        e = frame_from if isinstance(frame_from, dict) else json.load(open(frame_from))[slug]
        x1, x2, z1, z2 = e['bounds']
        sc = e['scale']
        fr = dict(x1=x1, x2=x2, z1=z1, z2=z2, scale=sc, w=int(round((x2 - x1) / sc)), h=int(round((z2 - z1) / sc)))
        return fr, list(e['splits'])
    ar = stroopmap.as_area(state)
    f = stroopmap.Frame(ar, None, 64.0)
    splits = [s for s, _ in stroopmap.find_bands(ar)]
    return dict(x1=f.x1, x2=f.x2, z1=f.z1, z2=f.z2, scale=f.scale, w=f.w, h=f.h), splits


class BlenderFailed(RuntimeError):
    """Blender ran but did not write every image."""
    pass


def run_blender(job, work, timeout, cancel=None, on_band=None):
    """Blender headless on one job (blender_render.py). A True `cancel()` (polled twice a second) ends the process and
    raises Cancelled; a missing Blender raises BlenderMissing, a failed run BlenderFailed."""
    exe = blender_exe()
    jp = os.path.join(work, f'job_{job["mode"]}.json')
    json.dump(job, open(jp, 'w'))
    t0 = time.time()
    log = os.path.join(work, f'blender_{job["mode"]}.log')
    with open(log, 'wb') as lf:
        p = subprocess.Popen([exe, '-b', '--factory-startup', '-P', os.path.join(HERE, 'blender_render.py'), '--', jp],
                             stdout=lf, stderr=subprocess.STDOUT,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            seen = 0
            while p.poll() is None:
                if on_band:
                    n = open(log, encoding='utf-8', errors='replace').read().count('BAND DONE')
                    if n != seen:
                        seen = n
                        on_band(n)
                if cancel and cancel():
                    p.kill()
                    p.wait()
                    raise Cancelled()
                if time.time() - t0 > timeout:
                    p.kill()
                    p.wait()
                    raise BlenderFailed(f'blender timed out after {timeout} s')
                time.sleep(0.5)
        finally:
            if p.poll() is None:
                p.kill()
    out = open(log, encoding='utf-8', errors='replace').read()
    done = [l for l in out.splitlines() if l.startswith('BAND DONE')]
    if on_band and not p.returncode:
        on_band(len(done))
    if p.returncode or len(done) != len(job['bands']):
        raise BlenderFailed(f'blender failed ({p.returncode}), {len(done)}/{len(job["bands"])} bands:\n' + out[-3000:])
    return time.time() - t0


def collision_top(state, fr, top):
    """Height of the topmost collision floor (not death planes) below `top` per pixel centre, NaN where none."""
    ar = stroopmap.as_area(state)
    zb = np.full((fr['h'], fr['w']), -np.inf)
    for _, ttype, v, _ in stroopmap.tris_of(ar, 'floor', death=False):
        ix, iz, h = stroopmap._raster(v, fr['x1'], fr['z1'], fr['scale'], fr['w'], fr['h'], top)
        better = h > zb[iz, ix]
        zb[iz[better], ix[better]] = h[better]
    return np.where(np.isfinite(zb), zb, np.nan)


def compare(vis, col, tol=8.0, rng=3):
    """How well a visual height map matches the collision one (the optional check)."""
    both = ~np.isnan(vis) & ~np.isnan(col)
    res = {'collision_px': int((~np.isnan(col)).sum()), 'visual_px': int((~np.isnan(vis)).sum()),
           'visual_over_floor': float(both.sum() / max((~np.isnan(col)).sum(), 1)),
           'floor_under_visual': float(both.sum() / max((~np.isnan(vis)).sum(), 1))}
    d = np.abs(vis[both] - col[both])
    res.update(median_dh=float(np.median(d)) if len(d) else None, p90_dh=float(np.percentile(d, 90)) if len(d) else None,
               within_tol=float((d <= tol).mean()) if len(d) else None)
    best = None
    H, W = vis.shape
    for dz in range(-rng, rng + 1):
        for dx in range(-rng, rng + 1):
            a = vis[max(dz, 0):H + min(dz, 0), max(dx, 0):W + min(dx, 0)]
            b = col[max(-dz, 0):H + min(-dz, 0), max(-dx, 0):W + min(-dx, 0)]
            ok = ~np.isnan(a) & ~np.isnan(b)
            f = float((np.abs(a[ok] - b[ok]) <= tol).mean()) if ok.any() else 0
            # ties (flat areas match at every shift) go to the smallest shift
            if best is None or f > best[0] + 1e-9 or (f > best[0] - 1e-9 and dx * dx + dz * dz < best[1] ** 2 + best[2] ** 2):
                best = (f, dx, dz)
    res['best_shift_px'] = best[1:]
    res['best_within_tol'] = best[0]
    return res


def render_source(src, slug, out, frame, splits, work, samples=16, objects=True, light='stroop', relief=False,
                  textures=True, check=False, timeout=1800, log=print, cancel=None, on_band=None):
    """The textured images of one area source in `frame` (a frame_and_bands dict) with the bands `splits`: PNGs
    `<slug>[_N].png` in `out`, scratch in `work`. `light` is 'stroop' (STROOP's map lighting) or 'rom'; `objects` draws
    the objects the object rule keeps. `check` also renders a height pass and compares it with the collision.
    on_band(n) is called as Blender finishes each band. Returns the info dict (also written by the CLI as
    <slug>_check.json). Raises BlenderMissing, BlenderFailed, Cancelled."""
    os.makedirs(work, exist_ok=True)
    os.makedirs(out, exist_ok=True)
    ram, scene = src.visual(objects=objects, light=None if light == 'rom' else geodecode.STROOP_LIGHT)
    walker = getattr(src, 'last_walker', None)
    ntri = sum(len(b['pos']) for b in scene.values()) // 3
    nobj = getattr(walker, 'nobj', None)
    unknown = getattr(getattr(walker, 'gfx', None), 'unknown', None)
    log(f'{slug}: {ntri} triangles in {len(scene)} batches, {nobj} objects; unknown ops {unknown}')
    if not scene:
        raise RuntimeError(f'{slug}: the area has no visual geometry')
    npz, mats, ymin, ymax = export(ram, scene, work, relief)
    edges = [None] + list(splits) + [None]
    multi = len(edges) > 2
    names = [f'{slug}_{k + 1}' if multi else slug for k in range(len(edges) - 1)]
    bands = [{'out': os.path.join(out, n + '.png'), 'top': edges[k + 1]} for k, n in enumerate(names)]
    job = dict(npz=npz, materials=mats, frame=frame, bands=bands, ymin=ymin, ymax=ymax, mode='color',
               samples=samples, textures=textures)
    secs = run_blender(job, work, timeout, cancel, on_band)
    log(f'{slug}: frame {frame["w"]}x{frame["h"]} px, splits {splits}; render {secs:.0f} s')
    info = {'slug': slug, 'triangles': ntri, 'frame': frame, 'splits': list(splits), 'objects': nobj,
            'unknown_ops': unknown, 'files': [n + '.png' for n in names], 'bands': []}
    if check:
        hb = [{'out': os.path.join(work, n + '_height'), 'top': b['top']} for n, b in zip(names, bands)]
        # the height check is against the static collision, so it renders the level without objects
        _, sscene = src.visual(objects=False, light=None)
        os.makedirs(os.path.join(work, 'static'), exist_ok=True)
        snpz, smats, _, _ = export(ram, sscene, os.path.join(work, 'static'))
        run_blender(dict(job, npz=snpz, materials=smats, mode='height', bands=hb), work, timeout, cancel)
        for n, b, h in zip(names, bands, hb):
            vis = np.load(h['out'] + '.npy')
            r = compare(vis, collision_top(src, frame, b['top']))
            r['file'] = n
            info['bands'].append(r)
            log(json.dumps(r))
    return info


def main():
    """The command line: render one savestate area (see --help)."""
    ap = argparse.ArgumentParser()
    ap.add_argument('state')
    ap.add_argument('--slug', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--frame-from')
    ap.add_argument('--no-textures', action='store_true')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--samples', type=int, default=16)
    ap.add_argument('--no-objects', action='store_true', help='leave out the objects with collision models')
    ap.add_argument('--light', choices=('stroop', 'rom'), default='stroop',
                    help="lit geometry: STROOP's map lighting (fitted on BoB.png, the same in every level) or the ROM's")
    ap.add_argument('--relief', action='store_true', help="multiply in stage 1's slope shade")
    ap.add_argument('--timeout', type=int, default=1800)
    a = ap.parse_args()
    import sources
    a.out = os.path.abspath(a.out)
    src = sources.StateSource(a.state)
    fr, splits = frame_and_bands(src, a.slug, a.frame_from)
    work = os.path.join(HERE, 'work', 'stage2', a.slug)
    info = render_source(src, a.slug, a.out, fr, splits, work, a.samples, not a.no_objects, a.light, a.relief,
                         not a.no_textures, a.check, a.timeout)
    info['state'] = a.state
    json.dump(info, open(os.path.join(a.out, f'{a.slug}_check.json'), 'w'), indent=1)


if __name__ == '__main__':
    main()
