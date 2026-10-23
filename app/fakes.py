"""Stand-ins for the map modules (romload, sources, exporter, stroop_install), with the same call signatures as the
real ones (backend.py lists them). backend.select() uses them when the real module cannot be imported or lacks a
function, or with --fake. They do real work on real folders (PNG files, a fake STROOP tree) so the
window and the self-test exercise every path, but they know nothing about SM64.

Knobs for tests: DELAY[0] (seconds per image), BLENDER[0] (True/False), stroop_install.RUNNING[0], FAIL (set of
function names that raise).
"""
import json
import os
import shutil
import struct
import time
import zlib
from types import SimpleNamespace

DELAY = [0.12]
BLENDER = ['C:\\Fake\\Blender\\blender.exe']       # '' = missing
FAIL = set()


class FakeError(Exception):
    """An error a stand-in raises on purpose (the FAIL knob, a missing Blender)."""
    pass


# ------------------------------------------------------------------------------------------------ PNG writer
def write_png(path, w, h, pixel):
    """pixel(x, y) -> (r, g, b, a). Plain PNG, no libraries."""
    rows = bytearray()
    for y in range(h):
        rows.append(0)
        for x in range(w):
            rows += bytes(pixel(x, y))
    def chunk(tag, data):
        """One PNG chunk: length, tag, data, CRC."""
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0))
                + chunk(b'IDAT', zlib.compress(bytes(rows), 6)) + chunk(b'IEND', b''))


def _art(seed, textured):
    hue = (seed * 53) % 255
    def px(x, y):
        """A pixel's colour: a few soft shapes, varied by the seed."""
        cx, cy = x - 70 - (seed % 5) * 6, y - 70
        inside = (abs(cx) < 44 + (seed % 4) * 8 and abs(cy) < 30 + (seed % 3) * 12) or cx * cx + (cy - 20) ** 2 < 1400
        if not inside:
            return (0, 0, 0, 0)
        if textured:
            n = ((x // 6 + y // 6) & 1) * 30
            return (90 + n + hue // 4, 120 + n, 70 + n // 2 + (255 - hue) // 5, 255)
        edge = abs(abs(cx) - 44 - (seed % 4) * 8) < 2
        return (30, 30, 40, 255) if edge else (60 + hue // 3, 140 + (cy % 40), 200 - hue // 3, 255)
    return px


# ------------------------------------------------------------------------------------------------ romload
_NAMES = [(1, 1, 'Castle Grounds', ''), (2, 1, 'Mushroom Hill', ''), (3, 1, 'Cobalt Cavern', 'MIO0 segment skipped'),
          (4, 1, 'Sandy Ruins', ''), (4, 2, 'Sandy Ruins (inside)', ''), (5, 1, 'Frost Peak', ''),
          (6, 1, 'Ghost Manor', 'no collision objects found'), (7, 1, 'Bowser Arena', '')]


class AreaInfo:
    """A listed area, like romload.AreaInfo."""
    def __init__(self, level, area, name, notes='', unchanged=False):
        self.level, self.area, self.name, self.unchanged = level, area, name, unchanged
        self.notes = [notes] if notes else []


_UNCHANGED = [(8, 1, 'Hazy Cave (vanilla)', 'identical to the vanilla US level'),
              (9, 1, 'Whomp Fortress (vanilla)', 'identical to the vanilla US level')]


def _list_areas(rom_path, levels=None, include_unchanged=None):
    if 'list_areas' in FAIL:
        raise FakeError('fake list_areas failure')
    with open(rom_path, 'rb') as f:
        head = f.read(4)
    if head not in (b'\x80\x37\x12\x40', b'\x40\x12\x37\x80', b'\x37\x80\x40\x12'):
        raise ValueError('not an N64 ROM file (bad header)')
    time.sleep(0.05)
    out = [AreaInfo(*a) for a in _NAMES]
    if include_unchanged:
        out += [AreaInfo(*a, unchanged=True) for a in _UNCHANGED]
    return out


class _RomAreaSource:
    def __init__(self, rom_path, level, area, shift=None, act=1, switch='first', with_object_collision=False):
        self.with_object_collision = with_object_collision
        self.name = next((n for lv, ar, n, _ in _NAMES + _UNCHANGED if (lv, ar) == (level, area)),
                         f'Level {level} area {area}')
        self.hack, self.level, self.area, self.shift, self.tris, self.mario = 'Fake Hack', level, area, 2, [], None
        self.rom_path = rom_path

    def visual(self, objects=True, light=None):
        raise FakeError('fake sources have no visual')


romload = SimpleNamespace(list_areas=_list_areas, RomAreaSource=_RomAreaSource, AreaInfo=AreaInfo)


# ------------------------------------------------------------------------------------------------ sources
class _StateSource:
    def __init__(self, path):
        if os.path.getsize(path) == 0:
            raise ValueError('the savestate is empty')
        stem = os.path.splitext(os.path.basename(path))[0]
        self.name, self.hack = stem, 'Fake Hack'
        self.level, self.area = 9, 1
        self.shift, self.tris, self.mario = 0, [], (0.0, 0.0, 0.0)

    def visual(self, objects=True, light=None):
        raise FakeError('fake sources have no visual')


sources = SimpleNamespace(StateSource=_StateSource)


# ------------------------------------------------------------------------------------------------ exporter
class ExportResult:
    """An export's outcome, like exporter.ExportResult."""
    def __init__(self, out_dir, images, warnings, hack='', cancelled=False):
        self.out_dir, self.images, self.warnings, self.hack, self.cancelled = out_dir, images, warnings, hack, cancelled
        self.errors = []

    @property
    def ok(self):
        """True when the export finished with no error and was not cancelled."""
        return not self.errors and not self.cancelled


def _slug(s):
    return ''.join(c.lower() if c.isalnum() else '_' for c in s).strip('_') or 'area'


def _export(srcs, kinds=('collision', 'textured'), out_dir='.', options=None, log=print, progress=None, cancel=None):
    if 'export' in FAIL:
        raise FakeError('fake export failure')
    cancel = cancel or (lambda: False)
    kinds = tuple(kinds)
    if 'textured' in kinds and not BLENDER[0]:
        raise FakeError('Blender not found: textured export needs it (collision export still works)')
    hack = srcs[0].hack if srcs else 'Fake Hack'
    opts = options or {}
    log(f'options: scale={opts.get("scale")} bands={opts.get("bands")} light={opts.get("light")} '
        f'relief={opts.get("relief")} objects={opts.get("objects")} objcol={getattr(srcs[0], "with_object_collision", None)} '
        f'resume={opts.get("resume")} name_prefix={opts.get("name_prefix")}')
    total = len(srcs) * len(kinds)
    done, images, entries = 0, [], []
    if progress:
        progress(0, total)
    for s in srcs:
        for k in kinds:
            if cancel():
                log('cancelled')
                break
            sub = hack if k == 'collision' else hack + ' Textured'
            path = os.path.join(out_dir, 'Map Images', sub, _slug(s.name) + '.png')
            time.sleep(DELAY[0])
            write_png(path, 160, 140, _art(s.level * 7 + s.area, k == 'textured'))
            images.append(path)
            entries.append(f'  <Map name="{s.name}" id="F{s.level}{s.area}{k[0]}" level="{s.level}" area="{s.area}">'
                           f'<Image path="{sub}\\{_slug(s.name)}.png"/></Map>')
            done += 1
            log(f'{k:9s} {s.name}: {os.path.basename(path)}')
            if progress:
                progress(done, total)
        else:
            continue
        break
    with open(os.path.join(out_dir, f'MapAssociations_{_slug(hack)}.xml'), 'w', encoding='utf-8') as f:
        f.write('<MapAssociations>\n' + '\n'.join(entries) + '\n</MapAssociations>\n')
    warn = ['fake export: the images are placeholders'] if images else []
    return ExportResult(out_dir, images, warn, hack, cancelled=bool(cancel()))


def _blender_status():
    if BLENDER[0]:
        return True, BLENDER[0]
    return False, 'Blender was not found (textured maps render in Blender 5.2 headless; choose blender.exe below).'


def _set_blender(path):
    BLENDER[0] = path or ''


exporter = SimpleNamespace(export=_export, ExportResult=ExportResult, blender_status=_blender_status,
                           set_blender=_set_blender)


# ------------------------------------------------------------------------------------------------ stroop_install
RUNNING = [False]
_DB = os.path.join('Config', 'fake_installed.json')
_FLAG = os.path.join('Config', 'fake_patched.flag')


def make_fake_stroop(root, version='v0.9.2 (fake)'):
    """A minimal STROOP folder for the self-test: its config file, image folder and version."""
    os.makedirs(os.path.join(root, 'Config'), exist_ok=True)
    os.makedirs(os.path.join(root, 'Resources', 'Maps', 'Map Images'), exist_ok=True)
    with open(os.path.join(root, 'Config', 'MapAssociations.xml'), 'w', encoding='utf-8') as f:
        f.write('<MapAssociations>\n</MapAssociations>\n')
    with open(os.path.join(root, 'VERSION.txt'), 'w', encoding='utf-8') as f:
        f.write(f"FramePerfection's STROOP {version}, fake tree for the GUI self-test.\n")
    return root


def _db(d):
    try:
        with open(os.path.join(d, _DB), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(d, db):
    with open(os.path.join(d, _DB), 'w', encoding='utf-8') as f:
        json.dump(db, f)


def _install(result_or_dir, stroop_dir, hack=None, prefer=None, log=print):
    if 'install' in FAIL:
        raise FakeError('fake install failure')
    src = getattr(result_or_dir, 'out_dir', result_or_dir)
    mi = os.path.join(src, 'Map Images')
    if not os.path.isdir(mi):
        raise FileNotFoundError(mi)
    dest = os.path.join(stroop_dir, 'Resources', 'Maps', 'Map Images')
    kinds, n = [], 0
    for sub in sorted(os.listdir(mi)):
        if sub == hack or sub == hack + ' Textured':
            shutil.copytree(os.path.join(mi, sub), os.path.join(dest, sub), dirs_exist_ok=True)
            kinds.append('textured' if sub.endswith('Textured') else 'collision')
            n += len([f for f in os.listdir(os.path.join(mi, sub)) if f.endswith('.png')])
    if not kinds:
        raise FakeError(f'no images for "{hack}" in {mi}')
    shutil.copyfile(os.path.join(stroop_dir, 'Config', 'MapAssociations.xml'),
                    os.path.join(stroop_dir, 'Config', 'MapAssociations.backup-fake.xml'))
    db = _db(stroop_dir)
    db[hack] = {'entries': n, 'kinds': kinds}
    _save(stroop_dir, db)
    return {'hack': hack, 'kinds': kinds, 'entries': n, 'backup': 'MapAssociations.backup-fake.xml', 'renamed_ids': []}


def _uninstall(stroop_dir, hack, kinds=None, log=print):
    db = _db(stroop_dir)
    if hack not in db:
        raise FakeError(f'"{hack}" is not installed')
    dest = os.path.join(stroop_dir, 'Resources', 'Maps', 'Map Images')
    for sub in (hack, hack + ' Textured'):
        shutil.rmtree(os.path.join(dest, sub), ignore_errors=True)
    del db[hack]
    _save(stroop_dir, db)


def _installed_sets(stroop_dir):
    return [{'hack': h, 'entries': v['entries'], 'kinds': v['kinds'], 'legacy': False}
            for h, v in sorted(_db(stroop_dir).items())]


def _patch_status(stroop_dir):
    return 'patched' if os.path.exists(os.path.join(stroop_dir, _FLAG)) else 'stock'


def _set_patch(stroop_dir, on, log=print):
    if 'set_patch' in FAIL:
        raise FakeError('fake set_patch failure')
    p = os.path.join(stroop_dir, _FLAG)
    if on:
        with open(p, 'w') as f:
            f.write('1')
    elif os.path.exists(p):
        os.remove(p)


_OV = os.path.join('Config', 'fake_override.txt')


def _get_override(stroop_dir):
    try:
        with open(os.path.join(stroop_dir, _OV), encoding='utf-8') as f:
            return f.read().strip() or 'auto'
    except OSError:
        return 'auto'


def _set_override(stroop_dir, value, log=print):
    with open(os.path.join(stroop_dir, _OV), 'w', encoding='utf-8') as f:
        f.write(value or 'auto')


def _find_stroop():
    p = os.environ.get('STROOPMAPS_FAKE_STROOP', '')
    return p if p and os.path.isdir(p) else None


stroop_install = SimpleNamespace(find_stroop=_find_stroop, install=_install, uninstall=_uninstall,
                                 installed_sets=_installed_sets, patch_status=_patch_status, set_patch=_set_patch,
                                 get_hack_override=_get_override, set_hack_override=_set_override, RUNNING=RUNNING)
