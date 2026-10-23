"""Export STROOP map images and Map Associations entries for any number of area sources.

    import exporter, sources
    res = exporter.export([sources.StateSource('castle.st')], kinds=('collision', 'textured'), out_dir='out',
                          options={'hack': 'SM64 Island'}, log=print, progress=lambda done, total: ...,
                          cancel=lambda: False)

Writes, under out_dir (the layout the Island outputs have):
  Map Images/<hack>/<slug>[_N].png             collision maps (stroopmap.py)
  Map Images/<hack> Textured/<slug>[_N].png    textured maps (render_textured.py, Blender)
  MapAssociations_<hslug>.xml                  the <Map> entries (+ a <MapHack>), "collision"
  MapAssociations_<hslug>_textured.xml         the same for the textured set
  maps_<hslug>.json                            bounds, bands and scale of every area
  export.json                                  what the folder holds (read by stroop_install.install)
<hslug> is the hack's name without a leading "SM64 " / "Super Mario 64 ", lower case ("island").

Every <Map> carries hack="<hack>" and kind="collision|textured" and the <MapHack> holds the hack's name, the kind
the patched STROOP prefers and the RAM signature that tells the patched STROOP's "Recommended" which hack is running
(stroop_patch/README.md). Stock STROOP ignores all three.

Area sources are anything with the attributes sources.py lists. Blender is needed for 'textured' only: textured-only
without Blender raises render_textured.BlenderMissing before any work; with 'collision' too, collision is exported and
the textured set is reported in result.errors (and result.warnings), not raised.

Options (a dict, or an object with these attributes; unknown keys are ignored):
  hack          the map set's name (default: the sources' own; they must all agree)
  scale         units per pixel (None: 4 under ROM Manager 4x ELB, else 8)    margin 64   ss 2 (supersampling)
  bands         'none' | 'auto' | 'y1,y2,...'                                 ceilings False   death_planes False
  background    STROOP background for the entries ('Blue Background'; a source's own .background wins)
  objects 'static' (a geodecode.OBJECT_MODES name; True/False = static/none)
  light 'stroop'|'rom'  relief False  samples 16  timeout 1800 (seconds per area)  check False
  id_prefix     entry id prefix (default: the hack name's first letter); textured ids add 'T' after it
  name_prefix   words put before an area's name in the dropdown (default: hack_tag, 'TLD' / 'Island'); kept as the
                <MapHack> tag, which the installer and the GUI read back
  prefer        the kind "Recommended" prefers ('textured' when exported, else 'collision')
  signature     True: the hack signature (sources' .signature() / rom_path); False: none
  keep_work     keep the scratch folder out_dir/_work
  resume        False; True: a textured area whose band PNGs are all already in out_dir (an earlier, interrupted
                export of the same areas and options) isn't rendered again
"""
import dataclasses, json, os, re, shutil, sys, time
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import stroopmap                                   # noqa: E402

KINDS = ('collision', 'textured')
DEFAULTS = dict(hack=None, scale=None, margin=64.0, ss=2, bands='none', ceilings=False, death_planes=False,
                background='Blue Background', objects=True, light='stroop', relief=False, samples=16, timeout=1800,
                check=False, id_prefix=None, name_prefix=None, prefer=None, signature=True, keep_work=False,
                resume=False)


class ExportError(ValueError):
    """The request can't be exported (no hack name, two areas with the same level and area, ...)."""


@dataclasses.dataclass
class ExportSet:
    """One kind of map set written by export(): its entries file, folder and images."""
    hack: str
    kind: str                  # 'collision' | 'textured'
    folder: str                # the subfolder of Map Images
    xml: str                   # the entries file
    images: list               # PNG paths
    entries: int


@dataclasses.dataclass
class ExportResult:
    """What export() did: the images, sets, signature, warnings and errors."""
    out_dir: str
    hack: str = ''
    sets: list = dataclasses.field(default_factory=list)
    images: list = dataclasses.field(default_factory=list)
    xml_files: list = dataclasses.field(default_factory=list)
    infos: dict = dataclasses.field(default_factory=dict)
    signature: list = dataclasses.field(default_factory=list)
    warnings: list = dataclasses.field(default_factory=list)
    errors: list = dataclasses.field(default_factory=list)
    cancelled: bool = False
    seconds: float = 0.0
    manifest: str = ''

    @property
    def ok(self):
        """True when the export finished with no error and was not cancelled."""
        return not self.errors and not self.cancelled


def hack_slug(hack):
    """'SM64 Island' -> 'island': the part of the file names that follows MapAssociations_."""
    t = re.sub(r'^(sm64|super mario 64)\s+', '', hack.strip(), flags=re.I)
    return re.sub(r'[^a-z0-9]+', '_', t.lower()).strip('_') or 'hack'


GENERIC_LEAD = {'super', 'mario', 'sm', 'sm64', '64'}   # leading words that don't tell hacks apart
TAG_MAX = 6                                             # longest tag made of initials


def tag_words(hack):
    """The words of a hack name that count for its tag: parentheses and a version tail off, camelCase and letter /
    digit runs split ('SS1Repainted' -> SS 1 Repainted), the leading generic words (Super Mario 64, SM64, SM) dropped.
    An all-capitals name (a ROM header) is read in title case first."""
    t = re.sub(r'\([^)]*\)', ' ', hack or '')
    t = re.sub(r'\s+v?\d+(\.\d+)+\w*\s*$', '', t.strip(), flags=re.I)
    if t.isupper():
        t = t.title()
    words = []
    for w in re.findall(r'[A-Za-z0-9]+', t):
        words += re.findall(r'[A-Z]{2,}(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|[A-Z]|\d+', w)
    while words and words[0].lower() in GENERIC_LEAD:
        words.pop(0)
    return words


def hack_tag(hack, taken=()):
    """The short tag put before a hack's map names, so they fit STROOP's map list, made from the name alone (nothing
    is written in per hack). One word left after tag_words is kept ('SM64 Island' -> 'Island'); several
    give initials, a number or an all-capitals run whole ('Super Mario The Lost Dreams' -> 'TLD', 'SS1Repainted' ->
    'SS1R', 'Super Mario 74 Ex V1.5' -> '74E'), at most TAG_MAX characters. A name of generic words only gives 'Hack'.
    `taken`: tags other hacks already use (any case); a clash gets a number ('TLD2')."""
    words = tag_words(hack)
    if len(words) == 1:
        tag = words[0][:12]
    else:
        tag = ''.join(w if w.isdigit() or (w.isupper() and len(w) > 1) else w[0].upper() for w in words)[:TAG_MAX]
    tag = tag or 'Hack'
    used = {t.lower() for t in taken}
    base, k = tag, 2
    while tag.lower() in used:
        tag, k = f'{base}{k}', k + 1
    return tag


def folder_name(hack):
    """The hack's folder under Map Images (the hack's name, made safe for a path)."""
    return re.sub(r'[<>:"/\\|?*]+', '_', hack).strip(' .') or 'hack'


def _opts(options):
    out = dict(DEFAULTS)
    if options is None:
        return out
    for k in DEFAULTS:
        v = options.get(k) if isinstance(options, dict) else getattr(options, k, None)
        if v is not None:
            out[k] = v
    return out


def blender_status():
    """(ok, text): text is the Blender path when ok, else why textured maps are unavailable."""
    import render_textured
    try:
        return True, render_textured.blender_exe()
    except render_textured.BlenderMissing as e:
        return False, str(e)


def _png_complete(path):
    """True when `path` is a PNG written to the end (its IEND chunk), not one cut off by an interrupted render."""
    try:
        with open(path, 'rb') as f:
            f.seek(-12, os.SEEK_END)
            return f.read()[4:8] == b'IEND'
    except OSError:
        return False


def _slugs(srcs):
    """One file stem per source: its own `slug`, else its name; duplicates get _a<area> or _<n>."""
    import sources
    out, used = [], set()
    for s in srcs:
        base = sources.slug_of(getattr(s, 'slug', None) or s.name)
        slug = base
        if slug in used:
            slug = f'{base}_a{s.area}'
        n = 2
        while slug in used:
            slug = f'{base}_{n}'
            n += 1
        used.add(slug)
        out.append(slug)
    return out


def _signature(srcs, o, log):
    if not o['signature']:
        return []
    for s in srcs:
        try:
            if hasattr(s, 'signature'):
                sig = list(s.signature())
            elif getattr(s, 'rom_path', None):
                import sources
                sig = sources.signature_from_rom(s.rom_path)
            else:
                continue
        except Exception as e:                     # noqa: BLE001 - a missing vanilla ROM etc.: no signature
            log(f'(no hack signature: {e})')
            return []
        if sig:
            return sig
    log('(no hack signature: nothing in the game code differs from the vanilla US ROM, or it can\'t be read; '
        'the patched STROOP then needs the manual override)')
    return []


def _visual_frame(src, o):
    """stroopmap.Frame around an area's visual geometry (for an area without collision), or None without visuals."""
    import types
    import numpy as np
    try:
        _, scene = src.visual(objects=o['objects'], light=None)
    except Exception:                              # noqa: BLE001
        return None
    pos = [np.asarray(b['pos'], dtype=np.float64).reshape(-1, 3) for b in scene.values() if len(b['pos'])]
    if not pos:
        return None
    proxy = types.SimpleNamespace(tris=[('floor', 0, np.concatenate(pos), None)], shift=getattr(src, 'shift', 0))
    return stroopmap.Frame(proxy, o['scale'], o['margin'])


def hack_element(hack, prefer, signature, tag=None):
    """The <MapHack> element: name, preferred kind, the short tag before its map names, and the signature words."""
    e = ET.Element('MapHack', {'name': hack})
    if prefer:
        e.set('prefer', prefer)
    if tag:
        e.set('tag', tag)
    for addr, val in signature:
        ET.SubElement(e, 'Signature', {'address': f'0x{addr:08X}', 'value': f'0x{val:08X}'})
    return e


def export(sources, kinds=KINDS, out_dir=None, options=None, log=print, progress=None, cancel=None):
    """Export `sources` (area sources) as `kinds` into `out_dir`; see the module doc. Returns an ExportResult.
    Also callable as export(sources, out_dir, ...) (a path in the `kinds` place is taken as the folder).
    progress(done, total) counts images; cancel() returning True stops between images (result.cancelled, and no
    entries files are written). Raises ExportError, render_textured.BlenderMissing (textured only)."""
    if isinstance(kinds, (str, os.PathLike)) and str(kinds) not in KINDS:      # export(sources, out_dir[, kinds])
        kinds, out_dir = (out_dir if out_dir is not None else KINDS), kinds
    kinds = (kinds,) if isinstance(kinds, str) else tuple(kinds)
    bad = [k for k in kinds if k not in KINDS]
    if bad or not kinds:
        raise ExportError(f'kinds must be a non-empty subset of {KINDS}, got {kinds!r}')
    if not out_dir:
        raise ExportError('out_dir is required')
    srcs = list(sources)
    if not srcs:
        raise ExportError('no area sources')
    o = _opts(options)
    import geodecode
    o['objects'] = geodecode.object_mode(o['objects'])   # True / False / a mode name -> a mode (ValueError if unknown)
    log = log or (lambda *_: None)
    cancel = cancel or (lambda: False)
    t0 = time.time()
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    res = ExportResult(out_dir)

    import render_textured
    textured_error = None
    if 'textured' in kinds:
        try:
            render_textured.blender_exe()
        except render_textured.BlenderMissing as e:
            if kinds == ('textured',):
                raise
            textured_error = str(e)
            kinds = tuple(k for k in kinds if k != 'textured')
            res.errors.append('textured: ' + textured_error)
            res.warnings.append('textured maps skipped: ' + textured_error)
            log('textured maps skipped: ' + textured_error)

    hacks = {o['hack'] or getattr(s, 'hack', '') or '' for s in srcs}
    if len(hacks) != 1 or not next(iter(hacks)):
        raise ExportError('every area needs the same, non-empty hack name (set options["hack"] or source.hack); '
                          f'got {sorted(hacks)!r}')
    hack = hacks.pop()
    for s in srcs:
        try:
            s.hack = hack
        except AttributeError:                      # a source that keeps its own (read-only) hack name
            pass
    res.hack = hack
    hslug = hack_slug(hack)
    short = re.sub(r'^(sm64|super mario 64)\s+', '', hack.strip(), flags=re.I) or hack
    name_prefix = o['name_prefix'] if o['name_prefix'] is not None else hack_tag(hack)
    id_prefix = o['id_prefix'] or (re.sub(r'[^A-Za-z]', '', short)[:1].upper() or 'H')
    keys = [(s.level, s.area) for s in srcs]
    if len(set(keys)) != len(keys):
        raise ExportError('two areas have the same level and area (STROOP keys maps on them): ' + str(keys))
    # ---- plan: every area's frame and bands (so the progress total is known). An area that can't be planned (no
    # collision: STROOP places maps by the collision; a visual-only area has nothing to frame) is skipped, not fatal.
    plans, kept, visual_only = [], [], set()
    for s in srcs:
        if cancel():
            res.cancelled = True
            return _finish(res, t0)
        if not any(t[1] != stroopmap.DEATH_PLANE for t in s.tris):
            # no collision: STROOP still keys a map on the area, so a textured map framed by the visuals (no bands)
            frame = _visual_frame(s, o) if 'textured' in kinds else None
            if frame is None:
                res.warnings.append(f'{s.name} [{s.level}:{s.area}]: no collision and no visuals, skipped'
                                    if 'textured' in kinds else f'{s.name} [{s.level}:{s.area}]: no collision, skipped')
                log(res.warnings[-1])
                continue
            res.warnings.append(f'{s.name} [{s.level}:{s.area}]: no collision: textured map only, framed by its visuals')
            log(res.warnings[-1])
            plans.append((frame, []))
            kept.append(s)
            visual_only.add(id(s))
            continue
        try:
            plans.append(stroopmap.plan_area(s, o['scale'], o['margin'], o['bands']))
            kept.append(s)
        except Exception as e:                     # noqa: BLE001 - one bad area doesn't stop the others
            res.errors.append(f'{s.name} [{s.level}:{s.area}]: {type(e).__name__}: {e}')
            log(res.errors[-1])
    srcs = kept
    slugs = _slugs(srcs)
    per_area = [len(sp) + 1 for _, sp in plans]
    total = sum(per_area) * len(kinds)
    done = [0]

    def tick(n=1):
        done[0] += n
        if progress:
            progress(min(done[0], total), total)

    if progress:
        progress(0, total)
    work_root = os.path.join(out_dir, '_work', hslug)
    maps_dir = os.path.join(out_dir, 'Map Images')
    entries = {k: [] for k in kinds}
    folder = {'collision': folder_name(hack), 'textured': folder_name(hack) + ' Textured'}
    infos = {}
    try:
        # ---- collision
        if 'collision' in kinds:
            for s, slug, plan in zip(srcs, slugs, plans):
                if cancel():
                    res.cancelled = True
                    return _finish(res, t0)
                if id(s) in visual_only:
                    tick()
                    continue
                bg = getattr(s, 'background', None) or o['background']
                e, info = stroopmap.build(
                    s, slug, (f'{name_prefix} {s.name}'.strip() if not s.name.lower().startswith(name_prefix.lower())
                              else s.name) + (' (collision)' if 'textured' in kinds else ''),
                    maps_dir, subfolder=folder['collision'],
                    id_prefix=f'{id_prefix}{s.level:02d}{s.area}', scale=o['scale'], margin=o['margin'], ss=o['ss'],
                    ceilings=o['ceilings'], death_planes=o['death_planes'], bands=o['bands'], background=bg,
                    log=log, cancel=cancel, plan=plan, on_image=lambda p: (res.images.append(p), tick()))
                if info.get('cancelled'):
                    res.cancelled = True
                    return _finish(res, t0)
                for el in e:
                    el.set('hack', hack)
                    el.set('kind', 'collision')
                entries['collision'] += e
                infos[slug] = info
        # ---- textured
        if 'textured' in kinds:
            img_dir = os.path.join(maps_dir, folder['textured'])
            for s, slug, plan in zip(srcs, slugs, plans):
                if cancel():
                    res.cancelled = True
                    return _finish(res, t0)
                frame, splits = plan
                fr = dict(x1=frame.x1, x2=frame.x2, z1=frame.z1, z2=frame.z2, scale=frame.scale, w=frame.w, h=frame.h)
                last = [0]

                def on_band(n, last=last):
                    tick(n - last[0])
                    last[0] = n
                names = [f'{slug}_{b + 1}.png' if splits else f'{slug}.png' for b in range(len(splits) + 1)]
                resumed = o['resume'] and all(_png_complete(os.path.join(img_dir, n)) for n in names)
                try:
                    if resumed:
                        log(f'{s.name}: textured images already there (resume), not rendered again')
                        tick(len(splits) + 1)
                        info = {'resumed': True, 'files': names}
                    else:
                        info = render_textured.render_source(
                            s, slug, img_dir, fr, splits, os.path.join(work_root, slug), samples=o['samples'],
                            objects=o['objects'], light=o['light'], relief=o['relief'],
                            check=o['check'] and id(s) not in visual_only,
                            timeout=o['timeout'], log=log, cancel=cancel, on_band=on_band)
                except render_textured.Cancelled:
                    res.cancelled = True
                    return _finish(res, t0)
                except render_textured.BlenderMissing:
                    raise
                except Exception as e:                 # noqa: BLE001 - one area's render failing doesn't stop the rest
                    res.errors.append(f'{s.name} [{s.level}:{s.area}] textured: {type(e).__name__}: {str(e)[:300]}')
                    log(res.errors[-1])
                    tick(len(splits) + 1)
                    continue
                bg = getattr(s, 'background', None) or o['background']
                edges = [None] + list(splits) + [None]
                multi = len(edges) > 2
                for b in range(len(edges) - 1):
                    fname = f'{slug}_{b + 1}.png' if multi else f'{slug}.png'
                    res.images.append(os.path.join(img_dir, fname))
                    nm = f'{name_prefix} {s.name}'.strip() if not s.name.lower().startswith(name_prefix.lower()) \
                        else s.name
                    entries['textured'].append(stroopmap.map_element(
                        f'{id_prefix}T{s.level:02d}{s.area}{chr(65 + b)}',
                        # the kind only when the area has both (a visual-only area has no collision map)
                        nm + (' (textured)' if 'collision' in kinds and id(s) not in visual_only else ''),
                        f'Floor {b + 1}' if multi else None, s.level, s.area, f'{folder["textured"]}\\{fname}',
                        (frame.w, frame.h), frame, edges[b], bg))
                    entries['textured'][-1].set('hack', hack)
                    entries['textured'][-1].set('kind', 'textured')
                base = infos.setdefault(slug, {'level': s.level, 'area': s.area, 'shift': s.shift, 'scale': frame.scale,
                                               'bounds': (frame.x1, frame.x2, frame.z1, frame.z2),
                                               'size': (frame.w, frame.h), 'splits': splits})
                base['textured'] = info
    finally:
        if not o['keep_work']:
            shutil.rmtree(os.path.join(out_dir, '_work'), ignore_errors=True)

    # ---- the entries files, the maps file and the manifest
    sig = _signature(srcs, o, log)
    res.signature = sig
    prefer = o['prefer'] or ('textured' if 'textured' in kinds else 'collision')
    hel = hack_element(hack, prefer, sig, name_prefix)
    for k in kinds:
        xml = os.path.join(out_dir, f'MapAssociations_{hslug}' + ('_textured' if k == 'textured' else '') + '.xml')
        stroopmap.write_snippet(xml, entries[k], [hel])
        res.xml_files.append(xml)
        prefix = os.path.join(maps_dir, folder[k])
        res.sets.append(ExportSet(hack, k, folder[k], xml, [p for p in res.images if os.path.dirname(p) == prefix],
                                  len(entries[k])))
    res.infos = infos
    with open(os.path.join(out_dir, f'maps_{hslug}.json'), 'w', encoding='utf-8') as fh:
        json.dump({slug: {k: v for k, v in i.items() if k != 'textured'} for slug, i in infos.items()}, fh, indent=1,
                  default=str)
    res.manifest = _write_manifest(res, kinds, prefer, sig, [(s.name, s.level, s.area, sl) for s, sl in zip(srcs, slugs)])
    return _finish(res, t0)


def _write_manifest(res, kinds, prefer, sig, areas):
    path = os.path.join(res.out_dir, 'export.json')
    old = {}
    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as fh:
                old = json.load(fh)
        except (OSError, ValueError):
            old = {}
    keep = [s for s in old.get('sets', []) if not (s['hack'] == res.hack and s['kind'] in kinds)]
    for st in res.sets:
        keep.append({'hack': st.hack, 'kind': st.kind, 'folder': st.folder, 'xml': os.path.basename(st.xml),
                     'images': [os.path.basename(p) for p in st.images], 'entries': st.entries, 'prefer': prefer,
                     'signature': [[a, v] for a, v in sig], 'areas': [list(a) for a in areas]})
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump({'format': 1, 'made': time.strftime('%Y-%m-%d %H:%M:%S'), 'sets': keep}, fh, indent=1)
    return path


def _finish(res, t0):
    res.seconds = time.time() - t0
    return res


def main():
    """The command line: export savestates (see --help)."""
    import argparse
    import sources as _sources
    ap = argparse.ArgumentParser(description='Export STROOP maps of savestates (ROMs: use the GUI).')
    ap.add_argument('states', nargs='+', help='savestates (.st), one area each')
    ap.add_argument('--out', required=True)
    ap.add_argument('--hack', help="the map set's name (default: from the ROM header)")
    ap.add_argument('--kinds', default='collision,textured')
    ap.add_argument('--scale', type=float)
    ap.add_argument('--bands', default='none',
                    help='"none" (one top view per area), "auto" (split by floor) or y1,y2,...')
    ap.add_argument('--objects', default='static', help='static (default), scenery, grabbable, collision, all, none')
    ap.add_argument('--no-objects', action='store_true', help='same as --objects none')
    ap.add_argument('--light', choices=('stroop', 'rom'), default='stroop')
    a = ap.parse_args()
    srcs = [_sources.StateSource(p, hack=a.hack) for p in a.states]
    res = export(srcs, tuple(a.kinds.split(',')), a.out,
                 {'hack': a.hack, 'scale': a.scale, 'bands': a.bands, 'objects': 'none' if a.no_objects else a.objects, 'light': a.light})
    print(f'{len(res.images)} images, {len(res.xml_files)} entries files in {res.out_dir}; errors: {res.errors}')


if __name__ == '__main__':
    main()
