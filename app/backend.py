"""The one adapter between the window and the map modules: everything the window needs from romload, sources,
exporter, stroop_install and sm64io goes through this file. A module that cannot be imported (or lacks the functions
below) is replaced by app/fakes.py, per module (--fake replaces all of them: a demo and the self-test). `MODE` says
which is which; the window shows a banner for every stand-in in use.

THE CALLS THIS FILE MAKES (change them here only):

  romload.list_areas(rom_path, include_unchanged=bool) -> list[AreaInfo]
        AreaInfo.level / .area / .name / .notes (list) / .unchanged; raises ValueError for a ROM it cannot read
  romload.RomAreaSource(rom_path, level, area, with_object_collision=bool)     an area source; .hack is writable
  sources.StateSource(state_path)                        an area source (.name .hack .level .area ...)
  sources.hack_name_for(path)                            the name prefilled in "Hack name" (else the ROM header's)
  exporter.export(sources, kinds=('collision','textured'), out_dir=..., options=Options, log=fn(str),
                  progress=fn(done, total), cancel=fn() -> bool) -> ExportResult
        options keys that exporter reads: hack, scale (float | None), bands ('auto'|'none'|'y1,y2'), objects (a geodecode.OBJECT_MODES key: static | scenery | grabbable | collision | all | none),
        light ('stroop'|'rom'), relief (bool). The GUI sends exactly these.
        ExportResult: .out_dir .hack .images .warnings .errors .cancelled .ok
  exporter.blender_status() -> (ok, text)                the Blender row; render_textured.BLENDER is set from the
                                                         user's choice (render_textured.blender_exe() reads it first)
  stroop_install.find_stroop() -> path | None
  stroop_install.install(result_or_dir, stroop_dir, hack, log=fn) -> dict(hack, kinds, entries, backup, renamed_ids)
  stroop_install.uninstall(stroop_dir, hack, log=fn)
  stroop_install.installed_sets(stroop_dir) -> [InstalledSet(hack, kinds (list), entries, legacy, images)]
  stroop_install.patch_status(stroop_dir) -> 'stock' | 'patched' | 'unknown'
  stroop_install.set_patch(stroop_dir, on, log=fn)       (needs stroop_patch/bin/ at the same relative path when packaged)
  stroop_install.get_hack_override(stroop_dir) / set_hack_override(stroop_dir, value, log=fn)
        value 'auto' | 'off' | a hack's name

Exceptions from any of these are turned into short messages by `explain()`; the window never shows a traceback (the
full one goes to the error log, `error_log_path()`).
"""
import importlib
import os
import subprocess
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
FROZEN = bool(getattr(sys, 'frozen', False))
VERSION = '1.0.0'                # the release (its git tag is v + this); shown in the title and build_info.json
if not FROZEN:
    for _p in (os.path.dirname(HERE), HERE):            # the stroopmaps modules (sm64io reaches the rest)
        if os.path.isdir(_p) and _p not in sys.path:
            sys.path.insert(0, _p)

ROM_EXT = ('.z64', '.n64', '.v64')
STATE_EXT = ('.st', '.savestate')
KINDS = ('collision', 'textured')

_NEED = {
    'romload': ('list_areas', 'RomAreaSource'),
    'sources': ('StateSource',),
    'exporter': ('export', 'blender_status'),
    'stroop_install': ('find_stroop', 'install', 'uninstall', 'installed_sets', 'patch_status', 'set_patch',
                       'get_hack_override', 'set_hack_override'),
}
MODE = {}          # module name -> 'real' | 'fake'
WHY = {}           # module name -> why it is a fake
_mods = {}
BLENDER_OVERRIDE = ['']
ERROR_LOG = ['']


class Options(dict):
    """A dict that also answers attribute reads, so the exporter may take either form."""
    __getattr__ = dict.get

    def __setattr__(self, k, v):
        self[k] = v


# exceptions of the map modules whose message is written to be shown as it is
USER_FACING = ('InstallError', 'ExportError', 'BlenderMissing')


class Cancelled(Exception):
    """The export was cancelled from the window."""
    pass


class UserError(Exception):
    """A problem the person using the window can act on; the message is shown as it is."""


def select(force_fake=False):
    """(Re)pick the real or fake module for each group. Returns MODE."""
    import fakes
    for name, need in _NEED.items():
        mod, why = None, ''
        if force_fake:
            why = 'started with --fake'
        else:
            try:
                m = importlib.import_module(name)
                missing = [n for n in need if not hasattr(m, n)]
                if missing:
                    why = f'{name}.py lacks {", ".join(missing)}'
                else:
                    mod = m
            except Exception as e:                      # noqa: BLE001 - any import failure means "not landed yet"
                why = f'{name}: {type(e).__name__}: {str(e)[:120]}'
        if mod is None:
            mod = getattr(fakes, name)
            MODE[name], WHY[name] = 'fake', why
        else:
            MODE[name], WHY[name] = 'real', ''
        _mods[name] = mod
    return MODE


def fakes_in_use():
    """The stand-in modules in use, each with the reason (empty when every module is real)."""
    return [f'{n} ({WHY[n]})' for n, m in MODE.items() if m == 'fake']


def mod(name):
    """The module the window uses for `name` (romload, sources, exporter, stroop_install): real or fake."""
    if name not in _mods:
        select()
    return _mods[name]


# ------------------------------------------------------------------------------------------------ errors
def error_log_path():
    """Where explain() writes the traceback of an unexpected error."""
    return ERROR_LOG[0] or os.path.join(tempfile.gettempdir(), 'stroopmaps_error.log')


def explain(exc):
    """A short, readable message for an exception (and the traceback goes to the error log)."""
    try:
        with open(error_log_path(), 'a', encoding='utf-8') as f:
            f.write(f'--- {time.strftime("%Y-%m-%d %H:%M:%S")}\n')
            f.write(''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)) + '\n')
    except OSError:
        pass
    if isinstance(exc, UserError) or type(exc).__name__ in USER_FACING:
        return str(exc)
    if isinstance(exc, FileNotFoundError):
        return f'File or folder not found: {exc.filename or exc}'
    if isinstance(exc, PermissionError):
        return f'No permission for {exc.filename or "a file"}. Is it open in another program (or STROOP running)?'
    if isinstance(exc, MemoryError):
        return 'Out of memory (the area is too large for this scale; try a larger units-per-pixel value).'
    if isinstance(exc, SystemExit):
        return str(exc.code) if exc.code not in (None, 0) else 'The export stopped early.'
    first = (str(exc).strip().splitlines() or [''])[0]
    return f'{type(exc).__name__}: {first[:300]}' if first else type(exc).__name__


# ------------------------------------------------------------------------------------------------ inputs
def input_kind(path):
    """'rom', 'state' or None, from the file extension."""
    e = os.path.splitext(path)[1].lower()
    if e in ROM_EXT:
        return 'rom'
    if e in STATE_EXT:
        return 'state'
    return None


def _rom_header_name(path):
    try:
        with open(path, 'rb') as f:
            head = f.read(0x40)
    except OSError:
        return ''
    if len(head) < 0x34:
        return ''
    if head[:4] == b'\x37\x80\x40\x12':                  # .v64: 16-bit byte-swapped
        head = b''.join(head[i + 1:i + 2] + head[i:i + 1] for i in range(0, len(head) - 1, 2))
    elif head[:4] == b'\x40\x12\x37\x80':                # .n64: 32-bit little-endian
        head = b''.join(head[i:i + 4][::-1] for i in range(0, len(head) - 3, 4))
    name = head[0x20:0x34].decode('ascii', 'ignore').strip('\x00 ').strip()
    return name if name and all(c.isprintable() for c in name) else ''


def default_hack(path):
    """A name for the map set: the ROM header's name, else the file name (a state: its file name)."""
    stem = os.path.splitext(os.path.basename(path))[0]
    try:                                                   # the exporter's own naming (ROM header through versions)
        n = mod('sources').hack_name_for(path)
        if n:
            return n
    except Exception:                                      # noqa: BLE001 - the fakes have none: fall through
        pass
    if input_kind(path) == 'rom':
        n = _rom_header_name(path)
        if n and n.upper() not in ('SUPER MARIO 64', 'SM64'):
            return n
    return stem.split('.')[0].strip() or stem


class Area:
    """An area as the window lists it: level, area, name, notes, unchanged."""
    def __init__(self, level, area, name, notes='', unchanged=False):
        self.level, self.area, self.name, self.notes, self.unchanged = level, area, name, notes, unchanged

    @property
    def key(self):
        """(level, area): what the window keys its tick marks on."""
        return (self.level, self.area)


def _area(a):
    g = (lambda k, d=None: a.get(k, d)) if isinstance(a, dict) else (lambda k, d=None: getattr(a, k, d))
    notes = g('notes', '') or ''
    if isinstance(notes, (list, tuple)):
        notes = '; '.join(map(str, notes))
    return Area(int(g('level')), int(g('area')), str(g('name', '') or f'Level {g("level")} area {g("area")}'), notes,
                bool(g('unchanged', False)))


def read_input(path, include_unchanged=False):
    """What the chosen file holds: (kind, areas, hack). A ROM lists every area it defines (no Mupen); a savestate has its
    one area. include_unchanged: also list a hack's levels that are byte-identical to vanilla (STROOP ships those maps).
    Slow for a big ROM: call it from a worker thread."""
    kind = input_kind(path)
    if not os.path.isfile(path):
        raise UserError(f'File not found: {path}')
    if kind is None:
        raise UserError('Choose a ROM (.z64, .n64, .v64) or a savestate (.st).')
    if MODE.get('sources') == 'real':
        # the folder joins the ROM search: a savestate's own ROM, and the vanilla US ROM a hack's signature needs
        import sm64io
        sm64io.remember_rom_dir(os.path.dirname(os.path.abspath(path)))
    hack = default_hack(path)
    if kind == 'rom':
        try:
            infos = mod('romload').list_areas(path, include_unchanged=bool(include_unchanged))
        except ValueError as e:
            first = str(e).splitlines()[0].replace(path + ': ', '')[:160] if str(e) else ''
            raise UserError("This ROM can't be read directly" + (f' ({first})' if first else '') +
                            '. Make a savestate in the level with Mupen and choose that instead.') from e
        areas = [_area(a) for a in infos]
        if not areas:
            raise UserError('No areas were found in this ROM (is it a Super Mario 64 ROM or hack?).')
        return kind, areas, hack
    try:
        src = mod('sources').StateSource(path)
    except (Exception, SystemExit) as e:                    # noqa: BLE001 - the collision reader has its limits
        why = ((str(e.code if isinstance(e, SystemExit) else e).splitlines() or [''])[0].replace(path + ': ', '')[:200]
               or type(e).__name__)
        explain(e)                                          # traceback to the error log
        raise UserError(f"This savestate's collision can't be read ({type(e).__name__}: {why}). Take the state after "
                        'the level has finished loading; some hacks are not supported by the collision reader yet.') from e
    a = Area(int(src.level), int(src.area), str(getattr(src, 'name', '') or f'Level {src.level} area {src.area}'),
             'the savestate\'s loaded area')
    hk = getattr(src, 'hack', '')
    return kind, [a], (hk or hack)


# ------------------------------------------------------------------------------------------------ Blender
def blender_status():
    """(ok, text): text is the Blender path when ok, else the reason textured maps are unavailable."""
    ok, text = mod('exporter').blender_status()
    return bool(ok), str(text)


def set_blender(path):
    """Use this blender.exe: render_textured.BLENDER is the first place the exporter looks."""
    BLENDER_OVERRIDE[0] = path or ''
    if not path:
        return
    if MODE.get('exporter') == 'fake':
        mod('exporter').set_blender(path)
        return
    try:
        rt = sys.modules.get('render_textured') or importlib.import_module('render_textured')
        rt.BLENDER = path
    except Exception:                                    # noqa: BLE001
        pass


# ------------------------------------------------------------------------------------------------ export
class ImageInfo:
    """One exported image: its path, kind and name (the thumbnail's caption)."""
    def __init__(self, path, kind):
        self.path, self.kind = path, kind
        self.name = os.path.splitext(os.path.basename(path))[0]


class Result:
    """An export's outcome as the window shows it (the exporter's own result is .raw)."""
    def __init__(self, out_dir, images, warnings, raw, cancelled=False, hack=''):
        self.out_dir, self.images, self.warnings, self.raw = out_dir, images, warnings, raw
        self.cancelled, self.hack = cancelled, hack
        self.errors = []


def _scan_images(root):
    out = []
    for dp, _dn, fn in os.walk(root):
        for f in sorted(fn):
            if f.lower().endswith('.png'):
                p = os.path.join(dp, f)
                out.append(ImageInfo(p, 'textured' if 'textured' in dp.lower() else 'collision'))
    return out


def run_export(path, kind, areas, kinds, out_dir, hack, options, log, progress, cancel, object_collision=False):
    """Build the sources for the chosen areas and export them. Raises Cancelled / UserError / whatever the modules
    raise (the window calls explain()). Runs in a worker thread."""
    if not areas:
        raise UserError('Tick at least one area.')
    if not kinds:
        raise UserError('Choose collision, textured or both.')
    if 'textured' in kinds:
        ok, why = blender_status()
        if not ok:
            raise UserError(why)
    os.makedirs(out_dir, exist_ok=True)
    opts = Options(options)
    opts['hack'] = hack
    log(f'Reading {os.path.basename(path)} ...')
    srcs = []
    for a in areas:
        if cancel():
            raise Cancelled()
        if kind == 'rom':
            s = mod('romload').RomAreaSource(path, a.level, a.area, with_object_collision=bool(object_collision))
        else:
            s = mod('sources').StateSource(path)
        try:
            s.hack = hack
        except AttributeError:
            log(f'(the source for {a.name} keeps its own hack name)')
        srcs.append(s)
    log(f'{len(srcs)} area(s): {", ".join(a.name for a in areas)}')
    res = mod('exporter').export(srcs, kinds=tuple(kinds), out_dir=out_dir, options=opts, log=log,
                                 progress=progress, cancel=cancel)
    root = getattr(res, 'out_dir', None) or out_dir
    paths = getattr(res, 'images', None)
    if paths:
        images = [ImageInfo(p if isinstance(p, str) else str(getattr(p, 'path', p)),
                            'textured' if 'textured' in str(p).lower() else 'collision') for p in paths]
    else:
        images = _scan_images(root)
    images = [i for i in images if os.path.isfile(i.path)]
    warn = [str(w) for w in (getattr(res, 'warnings', None) or [])]
    errs = [str(w) for w in (getattr(res, 'errors', None) or [])]
    r = Result(root, images, warn, res, cancelled=bool(getattr(res, 'cancelled', False) or cancel()),
               hack=getattr(res, 'hack', '') or hack)
    r.errors = errs
    return r


# ------------------------------------------------------------------------------------------------ STROOP
def find_stroop():
    """A STROOP folder found without asking (stroop_install.find_stroop), or None."""
    try:
        p = mod('stroop_install').find_stroop()
    except Exception:                                    # noqa: BLE001 - "not found" is the answer
        p = None
    return str(p) if p else ''


def check_stroop_dir(d):
    """(ok, reason) for a folder claimed to be STROOP's."""
    if not d:
        return False, 'No STROOP folder chosen.'
    if not os.path.isdir(d):
        return False, 'That folder does not exist.'
    if not os.path.isfile(os.path.join(d, 'Config', 'MapAssociations.xml')):
        return False, 'Not a STROOP folder (Config\\MapAssociations.xml is missing).'
    return True, ''


def stroop_version(d):
    """The installed STROOP version from its VERSION.txt: (short, full text) or ('', '')."""
    import re
    try:
        with open(os.path.join(d, 'VERSION.txt'), encoding='utf-8', errors='replace') as f:
            text = f.read().strip()
    except OSError:
        return '', ''
    m = re.search(r'v?(\d+\.\d+(?:\.\d+)*)', text)
    first = text.splitlines()[0] if text else ''
    short = ('v' + m.group(1)) if m else first[:40]
    return short, text


def stroop_running(d):
    """True when a STROOP.exe is running (from this folder, or one whose folder cannot be read)."""
    if mod('stroop_install') is not None and MODE['stroop_install'] == 'fake':
        return bool(mod('stroop_install').RUNNING[0])
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-Command',
                            "Get-CimInstance Win32_Process -Filter \"Name='STROOP.exe'\" | "
                            "ForEach-Object { if ($_.ExecutablePath) { $_.ExecutablePath } else { '?' } }"],
                           capture_output=True, text=True, timeout=15,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError):
        return False
    here = os.path.normcase(os.path.abspath(d))
    for line in r.stdout.splitlines():
        line = line.strip()
        if line == '?' or (line and os.path.normcase(os.path.dirname(line)) == here):
            return True
    return False


class InstalledSet:
    """A map set installed in a STROOP folder, as the STROOP tab lists it."""
    def __init__(self, hack, entries=None, kinds='', legacy=False):
        self.hack, self.entries, self.kinds, self.legacy = hack, entries, kinds, legacy


def installed_sets(d):
    """The map sets installed in STROOP folder `d` (InstalledSet rows)."""
    out = []
    for s in mod('stroop_install').installed_sets(d) or []:
        if isinstance(s, str):
            out.append(InstalledSet(s))
        else:
            g = (lambda k, df='': s.get(k, df)) if isinstance(s, dict) else (lambda k, df='': getattr(s, k, df))
            k = g('kinds', '')
            out.append(InstalledSet(str(g('hack') or g('name')), g('entries', None),
                                    ', '.join(k) if isinstance(k, (list, tuple, set)) else str(k), bool(g('legacy', False))))
    return out


def suggest_tag(hack, stroop_dir='', remembered=None):
    """The short name put before the hack's map names (stroop_install.suggest_tag: remembered, else the installed
    set's, else made from the name and unique in that STROOP); '' when the modules can't say (fakes)."""
    f = getattr(mod('stroop_install'), 'suggest_tag', None)
    if f is None:
        return remembered or ''
    try:
        return f(hack, stroop_dir or None, remembered) or ''
    except Exception:                                      # noqa: BLE001 - a suggestion only
        return remembered or ''


def install(result, d, hack, log=None):
    """Install an export (a Result) into STROOP. Returns a one-line summary."""
    if not result:
        raise UserError('Make maps first (or choose an output folder that already holds them).')
    src = getattr(result, 'raw', None)
    return _install(src if src is not None and not isinstance(src, str) else result.out_dir, d, hack, log)


def install_dir(out_dir, d, hack, log=None):
    """Install the maps in output folder `out_dir` (an earlier export) into STROOP folder `d`."""
    if not os.path.isdir(out_dir):
        raise UserError('The output folder does not exist yet; make maps first.')
    return _install(out_dir, d, hack, log)


def _install(what, d, hack, log):
    ok, why = check_stroop_dir(d)
    if not ok:
        raise UserError(why)
    r = mod('stroop_install').install(what, d, hack, log=log or (lambda *_: None))
    if isinstance(r, dict):
        k = r.get('kinds')
        return (f'{r.get("entries", "?")} map entries ({", ".join(k) if isinstance(k, (list, tuple)) else k})'
                + (f'; backup {os.path.basename(str(r["backup"]))}' if r.get('backup') else ''))
    return ''


def uninstall(d, hack, log=None):
    """Remove `hack`'s maps and their images from STROOP folder `d`."""
    ok, why = check_stroop_dir(d)
    if not ok:
        raise UserError(why)
    mod('stroop_install').uninstall(d, hack, log=log or (lambda *_: None))


def patch_status(d):
    """'stock', 'patched' or 'unknown' for STROOP folder `d`."""
    try:
        return str(mod('stroop_install').patch_status(d))
    except Exception:                                    # noqa: BLE001
        return 'unknown'


def set_patch(d, on, log=None):
    """Switch the "Recommended" patch on or off in STROOP folder `d` (STROOP must be closed)."""
    ok, why = check_stroop_dir(d)
    if not ok:
        raise UserError(why)
    if stroop_running(d):
        raise UserError('STROOP is running. Close it first: the patch swaps its program files.')
    mod('stroop_install').set_patch(d, bool(on), log=log or (lambda *_: None))


def get_override(d):
    """'auto' | 'off' | a hack's name (the patched STROOP's HackOverride)."""
    try:
        return str(mod('stroop_install').get_hack_override(d) or 'auto')
    except Exception:                                    # noqa: BLE001
        return 'auto'


def set_override(d, value):
    """Set the patched STROOP's hack choice: 'auto', 'off' or a hack's name."""
    ok, why = check_stroop_dir(d)
    if not ok:
        raise UserError(why)
    mod('stroop_install').set_hack_override(d, value, log=lambda *_: None)
