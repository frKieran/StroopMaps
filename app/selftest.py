"""Headless-style self-test of the window with the stand-in modules (fakes.py): drives the real Tk window through
pick ROM -> select areas -> options -> export (with progress, a cancel, errors) -> thumbnails -> install -> uninstall ->
patch on/off -> savestate input -> settings remembered, checking the window's state after each step, and writes a
screenshot contact sheet of the main window states.

    python app/selftest.py [--sheet PATH] [--keep] [--no-shots]

Exit code 0 when every check passes. Uses a temporary folder for the fake ROM, the fake STROOP tree, the output and the
settings file; nothing outside it is touched. It needs a Windows desktop session (Tk, and a screen grab for the sheet).
"""
import argparse
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
SHEET_DEFAULT = os.path.abspath(os.path.join(HERE, '..', 'work', 'selftest_sheet.png'))

RESULTS = []


def check(name, cond, detail=''):
    """Record one check and print PASS or FAIL."""
    RESULTS.append((name, bool(cond), detail))
    print(('PASS ' if cond else 'FAIL ') + name + (f'  [{detail}]' if detail and not cond else ''), flush=True)
    return bool(cond)


def keep_display_on():
    """The display going to sleep blanks screen grabs (and STROOP's OpenGL view): ask Windows to keep it on."""
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000003)      # ES_CONTINUOUS | SYSTEM | DISPLAY
    except (OSError, AttributeError):
        pass


def grab_window(root):
    """A picture of the window: PrintWindow (works while another window covers it or the display is off), else a screen
    grab of its rectangle."""
    from PIL import Image, ImageGrab
    try:
        import ctypes
        from ctypes import wintypes
        u, g = ctypes.windll.user32, ctypes.windll.gdi32
        u.GetParent.restype = wintypes.HWND
        hwnd = u.GetParent(root.winfo_id()) or root.winfo_id()
        rc = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(rc))
        w, h = rc.right - rc.left, rc.bottom - rc.top
        for fn in (u.GetWindowDC, g.CreateCompatibleDC, g.CreateCompatibleBitmap, g.SelectObject, g.DeleteObject,
                   g.DeleteDC, u.ReleaseDC, u.PrintWindow, g.GetDIBits):
            fn.restype = ctypes.c_void_p if fn in (u.GetWindowDC, g.CreateCompatibleDC, g.CreateCompatibleBitmap,
                                                    g.SelectObject) else ctypes.c_int
        hdc = u.GetWindowDC(wintypes.HWND(hwnd))
        mdc = g.CreateCompatibleDC(ctypes.c_void_p(hdc))
        bmp = g.CreateCompatibleBitmap(ctypes.c_void_p(hdc), w, h)
        g.SelectObject(ctypes.c_void_p(mdc), ctypes.c_void_p(bmp))
        u.PrintWindow(wintypes.HWND(hwnd), ctypes.c_void_p(mdc), 2)          # PW_RENDERFULLCONTENT

        class BIH(ctypes.Structure):
            _fields_ = [('biSize', ctypes.c_uint32), ('biWidth', ctypes.c_int32), ('biHeight', ctypes.c_int32),
                        ('biPlanes', ctypes.c_uint16), ('biBitCount', ctypes.c_uint16), ('biCompression', ctypes.c_uint32),
                        ('biSizeImage', ctypes.c_uint32), ('x', ctypes.c_int32), ('y', ctypes.c_int32),
                        ('c', ctypes.c_uint32), ('i', ctypes.c_uint32)]
        bi = BIH(ctypes.sizeof(BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        g.GetDIBits(ctypes.c_void_p(mdc), ctypes.c_void_p(bmp), 0, h, buf, ctypes.byref(bi), 0)
        g.DeleteObject(ctypes.c_void_p(bmp))
        g.DeleteDC(ctypes.c_void_p(mdc))
        u.ReleaseDC(wintypes.HWND(hwnd), ctypes.c_void_p(hdc))
        im = Image.frombuffer('RGBA', (w, h), bytes(buf), 'raw', 'BGRA', 0, 1).convert('RGB')
        if im.getbbox() is not None and im.getextrema() != ((0, 0), (0, 0), (0, 0)):
            return im
    except Exception:                                        # noqa: BLE001 - fall back to the screen
        pass
    x, y, w, h = root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height()
    return ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)


class Shots:
    """Screenshots of the window for the contact sheet."""
    def __init__(self, root, enabled=True):
        self.root, self.enabled, self.items = root, enabled, []

    def take(self, caption):
        """Capture the window now, with a caption."""
        if not self.enabled:
            return
        r = self.root
        r.attributes('-topmost', True)
        r.lift()
        for _ in range(6):
            r.update()
            time.sleep(0.04)
        im = grab_window(r)
        self.items.append((caption, im))

    def sheet(self, path, cols=2, width=700):
        """Write the captured screenshots as one captioned contact sheet."""
        from PIL import Image, ImageDraw, ImageFont
        if not self.items:
            return
        try:
            font = ImageFont.truetype('segoeui.ttf', 16)
        except OSError:
            font = ImageFont.load_default()
        tiles = []
        for cap, im in self.items:
            h = int(im.height * width / im.width)
            tiles.append((cap, im.resize((width, h), Image.LANCZOS)))
        rows = [tiles[i:i + cols] for i in range(0, len(tiles), cols)]
        pad, cap_h = 14, 28
        row_h = [max(t[1].height for t in r) + cap_h for r in rows]
        W = cols * width + (cols + 1) * pad
        H = sum(row_h) + (len(rows) + 1) * pad
        sheet = Image.new('RGB', (W, H), (11, 26, 51))
        d = ImageDraw.Draw(sheet)
        y = pad
        for r, rh in zip(rows, row_h):
            for i, (cap, im) in enumerate(r):
                x = pad + i * (width + pad)
                d.text((x, y + 2), cap, fill=(232, 240, 255), font=font)
                sheet.paste(im, (x, y + cap_h))
            y += rh + pad
        os.makedirs(os.path.dirname(path), exist_ok=True)
        sheet.save(path)
        print('contact sheet:', path, sheet.size)


def write_fake_rom(path, magic=b'\x80\x37\x12\x40'):
    """A small file with a ROM header (the stand-ins only look at that)."""
    with open(path, 'wb') as f:
        f.write(magic + b'\x00' * 28 + b'FAKE HACK 64'.ljust(20, b' ') + b'\x00' * 0x200)


def main(argv=None):
    """Run every check; the exit code is the number that failed."""
    ap = argparse.ArgumentParser()
    ap.add_argument('--sheet', default=SHEET_DEFAULT)
    ap.add_argument('--keep', action='store_true', help='keep the temp folder')
    ap.add_argument('--no-shots', action='store_true')
    a = ap.parse_args(argv)

    tmp = tempfile.mkdtemp(prefix='stroopmaps_selftest_')
    os.environ['STROOPMAPS_SETTINGS'] = os.path.join(tmp, 'settings.json')
    import backend
    import fakes
    backend.select(force_fake=True)
    backend.ERROR_LOG[0] = os.path.join(tmp, 'last_error.log')
    fakes.DELAY[0] = 0.15
    from gui import app as appmod
    from gui.settings import Settings

    rom = os.path.join(tmp, 'Fake Hack.z64')
    write_fake_rom(rom)
    bad_rom = os.path.join(tmp, 'broken.z64')
    write_fake_rom(bad_rom, magic=b'NOPE')
    state = os.path.join(tmp, 'castle_lobby.st')
    with open(state, 'wb') as f:
        f.write(b'\x01' * 64)
    stroop = fakes.make_fake_stroop(os.path.join(tmp, 'STROOP'))
    out = os.path.join(tmp, 'maps_out')

    root = appmod.make_root()
    app = appmod.App(root, Settings())
    appmod.place(root)
    root.deiconify()
    root.update()
    mv, sv = app.maps, app.stroop
    app.confirm = lambda title, text: True
    pump = app.tasks.pump_until
    shots = Shots(root, not a.no_shots)

    try:
        # -- start state
        check('window starts on Make maps, run disabled', app.current == 'maps' and 'disabled' in mv.run_btn.state())
        check('fake banner shown', 'Test mode' in app.banner.cget('text'), app.banner.cget('text'))
        check('output folder defaults to Documents', mv.vars['out'].get().endswith('STROOP Maps'))
        mv.vars['out'].set(out)
        shots.take('1  Start: nothing chosen')

        # -- a broken file shows an error, not a traceback
        mv.open_input(bad_rom)
        check('broken ROM: reads', pump(lambda: not mv.reading, 10))
        txt = mv.input_info.cget('text')
        check('broken ROM: message in the window, no traceback', 'not an N64 ROM' in txt and 'Traceback' not in txt, txt)
        check('broken ROM: run stays disabled', 'disabled' in mv.run_btn.state())
        mv.open_input(os.path.join(tmp, 'notes.txt'))
        pump(lambda: not mv.reading, 5)
        check('missing file: friendly message', 'File not found' in mv.input_info.cget('text'), mv.input_info.cget('text'))
        open(os.path.join(tmp, 'notes.txt'), 'w').close()
        mv.open_input(os.path.join(tmp, 'notes.txt'))
        pump(lambda: not mv.reading, 5)
        check('wrong type: friendly message', 'Choose a ROM' in mv.input_info.cget('text'), mv.input_info.cget('text'))

        # -- pick the ROM
        mv.open_input(rom)
        check('ROM: areas read', pump(lambda: mv.input is not None, 10))
        check('ROM: 8 areas listed, all ticked', len(mv.tree.get_children()) == 8 and all(mv.checked.values()))
        check('ROM: hack name prefilled from the header', mv.vars['hack'].get() == 'FAKE HACK 64', mv.vars['hack'].get())
        check('ROM: notes shown', any('MIO0' in str(mv.tree.set(i, 'notes')) for i in mv.tree.get_children()))
        check('ROM: run enabled', 'disabled' not in mv.run_btn.state(), mv.why_not())
        shots.take('2  ROM chosen: its areas, all ticked')
        mv.vars['unchanged'].set(True)
        mv.reread()
        check('unchanged vanilla levels listed on request, unticked', pump(lambda: not mv.reading and mv.input is not None, 10)
              and len(mv.input['areas']) == 10 and sum(mv.checked.values()) == 8)
        mv.vars['unchanged'].set(False)
        mv.reread()
        check('unchanged levels hidden again', pump(lambda: not mv.reading and mv.input is not None, 10) and
              len(mv.input['areas']) == 8)

        # -- select areas
        mv.set_all(False)
        check('select none', not any(mv.checked.values()) and 'disabled' in mv.run_btn.state() and
              mv.why_not() == 'Tick at least one area.')
        for iid in ('1:1', '4:1', '4:2'):
            mv.toggle(iid)
        check('three areas ticked', len(mv.selected_areas()) == 3 and '3 of 8' in mv.count_lbl.cget('text'),
              mv.count_lbl.cget('text'))
        check('tick mark follows', mv.tree.set('4:2', 'ck') == '\u2611' and mv.tree.set('1:1', 'ck') == '\u2611' and
              mv.tree.set('2:1', 'ck') == '\u2610')

        # -- options
        mv.vars['kinds'].set('both')
        mv.vars['scale_mode'].set('custom')
        mv.vars['scale'].set('6')
        mv.vars['bands_mode'].set('custom')
        mv.vars['bands'].set('1200, 400')
        mv.vars['light'].set('relief')
        mv.vars['hack'].set('Test Hack')
        mv.vars['objcol'].set(True)
        root.update()
        opts, err = mv.collect()
        check('options collected', opts == {'scale': 6.0, 'bands': '400,1200', 'light': 'stroop', 'relief': True,
                                            'objects': 'static'} and not err, f'{opts} {err}')
        from gui import maps_view as _mv
        check('objects dropdown lists the six modes', list(mv.obj_btn.cget('values')) ==
              [label for _, label in _mv.OBJECT_LABELS] and len(_mv.OBJECT_LABELS) == 6)
        mv.vars['objects'].set(dict(_mv.OBJECT_LABELS)['grabbable'])
        check('objects mode from the dropdown', mv.collect()[0]['objects'] == 'grabbable')
        mv.vars['objects'].set(dict(_mv.OBJECT_LABELS)['none'])
        check('no objects', mv.collect()[0]['objects'] == 'none')
        check('old True / False settings map to modes', (_mv.object_mode(True), _mv.object_mode(False)) ==
              ('static', 'none'))
        mv.vars['objects'].set(dict(_mv.OBJECT_LABELS)['static'])
        mv.vars['scale'].set('abc')
        check('bad scale is reported', mv.collect()[1].startswith('Scale must be a number'))
        mv.vars['scale'].set('6')
        mv.vars['bands'].set('x,y')
        check('bad bands are reported', 'Floor bands' in mv.collect()[1])
        mv.vars['bands'].set('1200, 400')
        mv.vars['tag'].set('TST')                          # the short name, edited by hand
        mv.vars['resume'].set(True)
        shots.take('3  Three areas, both kinds, custom options')

        # -- export with progress
        mv.start_run()
        check('run started: cancel enabled, run disabled', mv.running and 'disabled' in mv.run_btn.state() and
              'disabled' not in mv.cancel_btn.state())
        check('progress moves', pump(lambda: mv.progress.cget('value') >= 2, 10), str(mv.progress.cget('value')))
        shots.take('4  Exporting: progress bar and log')
        check('export finishes', pump(lambda: not mv.running and app.result is not None, 30))
        res = app.result
        check('6 images (3 areas x 2 kinds)', len(res.images) == 6 and not res.cancelled, str(len(res.images)))
        check('images exist on disk', all(os.path.isfile(i.path) for i in res.images))
        check('options reached the exporter', any('scale=6.0' in l and 'relief=True' in l for l in
                                                   mv.log_text.get('1.0', 'end').splitlines()))
        check('object collision option reaches the sources', 'objcol=True' in mv.log_text.get('1.0', 'end'))
        check('resume option reaches the exporter', 'resume=True' in mv.log_text.get('1.0', 'end'))
        check('hack name reached the sources', os.path.isdir(os.path.join(out, 'Map Images', 'Test Hack')))
        check('short name remembered for the hack', mv.st['tags'].get('Test Hack') == 'TST', str(mv.st['tags']))
        mv.vars['hack'].set('Other Hack')
        mv.vars['hack'].set('Test Hack')
        check('short name comes back with the hack name', mv.vars['tag'].get() == 'TST', mv.vars['tag'].get())
        check('thumbnails load', pump(lambda: len(mv.grid_view.tiles) == 6 and
                                      all(t['photo'] is not None for t in mv.grid_view.tiles), 15))
        check('progress full, status says done', mv.progress.cget('value') == mv.progress.cget('maximum') and
              mv.status.cget('text').startswith('Done'), mv.status.cget('text'))
        shots.take('5  Done: thumbnails of the results')

        # -- cancel
        fakes.DELAY[0] = 0.4
        mv.start_run()
        pump(lambda: mv.progress.cget('value') >= 1, 10)
        mv.cancel_run()
        check('cancel: stops', pump(lambda: not mv.running, 15))
        check('cancel: partial result, marked cancelled', app.result is not None and app.result.cancelled and
              len(app.result.images) < 6, str(len(app.result.images) if app.result else None))
        check('cancel: status says so', 'Cancelled' in mv.status.cget('text'), mv.status.cget('text'))
        fakes.DELAY[0] = 0.05

        # -- an exporter failure shows a message
        fakes.FAIL.add('export')
        mv.start_run()
        check('failure: finishes', pump(lambda: not mv.running, 10))
        check('failure: message, no traceback', 'fake export failure' in mv.status.cget('text') and
              'Traceback' not in mv.status.cget('text'), mv.status.cget('text'))
        check('failure: traceback went to the error log', os.path.isfile(backend.error_log_path()) and
              'Traceback' in open(backend.error_log_path(), encoding='utf-8').read())
        fakes.FAIL.discard('export')
        shots.take('6  An export error: message in the window, details in a log file')

        # -- Blender missing
        fakes.BLENDER[0] = ''
        mv.refresh_blender()
        root.update()
        check('no Blender: textured and both disabled', 'disabled' in mv.kind_btns['textured'].state() and
              'disabled' in mv.kind_btns['both'].state() and 'disabled' not in mv.kind_btns['collision'].state())
        check('no Blender: kinds fell back to collision', mv.vars['kinds'].get() == 'collision')
        check('no Blender: reason shown', 'Blender' in mv.kind_note.cget('text'))
        check('no Blender: lighting greyed (collision only)', 'disabled' in mv.light_btns[0].state())
        check('no Blender: collision still runs', 'disabled' not in mv.run_btn.state(), mv.why_not())
        shots.take('7  Blender missing: textured is disabled with the reason')
        mv.set_blender('C:\\Fake\\Blender 5.2\\blender.exe')
        check('Blender chosen: textured enabled again', 'disabled' not in mv.kind_btns['both'].state())

        # -- STROOP tab
        app.show('stroop')
        sv.dir_var.set(os.path.join(tmp, 'nowhere'))
        root.update()
        check('STROOP: bad folder explained', 'does not exist' in sv.version_lbl.cget('text'), sv.version_lbl.cget('text'))
        check('STROOP: install/patch disabled', 'disabled' in sv.install_btn.state() and 'disabled' in sv.patch_btn.state())
        sv.dir_var.set(stroop)
        root.update()
        check('STROOP: version shown', 'v0.9.2' in sv.version_lbl.cget('text'), sv.version_lbl.cget('text'))
        check('STROOP: no sets, patch off', sv.sets == [] and sv.patch == 'stock' and sv.patch_lbl.cget('text') == 'Off')
        shots.take('8  STROOP tab: folder, version, no sets, patch off')

        app.install_maps()
        check('install: runs', pump(lambda: sv.busy_task is None, 10))
        check('install: set listed', [s.hack for s in sv.sets] == ['Test Hack'] and sv.tree.get_children() == ('Test Hack',),
              str([s.hack for s in sv.sets]))
        check('install: files in STROOP', os.path.isdir(os.path.join(stroop, 'Resources', 'Maps', 'Map Images', 'Test Hack')))
        check('install: message', 'Installed "Test Hack"' in sv.msg.cget('text'), sv.msg.cget('text'))

        sv.tree.selection_set('Test Hack')
        root.update()
        check('uninstall enabled on selection', 'disabled' not in sv.uninstall_btn.state())
        sv.uninstall_clicked()
        check('uninstall: runs, set gone', pump(lambda: sv.busy_task is None and not sv.sets, 10))
        check('uninstall: files gone', not os.path.isdir(os.path.join(stroop, 'Resources', 'Maps', 'Map Images', 'Test Hack')))
        app.install_maps()
        pump(lambda: sv.busy_task is None, 10)
        check('install again', len(sv.sets) == 1)
        fakes.FAIL.add('install')
        app.install_maps()
        pump(lambda: sv.busy_task is None, 10)
        check('install failure: message', 'fake install failure' in sv.msg.cget('text'), sv.msg.cget('text'))
        fakes.FAIL.discard('install')
        shots.take('9  STROOP tab: one set installed')

        # -- patch
        sv.patch_clicked()
        check('patch on', pump(lambda: sv.busy_task is None and sv.patch == 'patched', 10) and
              sv.patch_lbl.cget('text') == 'On' and sv.patch_btn.cget('text') == 'Turn off', sv.patch)
        shots.take('10  Recommended patch: on')
        sv.override_var.set('Always Test Hack')
        sv.override_changed()
        check('override dropdown: always Test Hack', pump(lambda: sv.busy_task is None, 10) and
              backend.get_override(stroop) == 'Test Hack' and 'Always Test Hack' in sv.override_box.cget('values'),
              backend.get_override(stroop))
        sv.override_var.set('Auto-detect the running hack')
        sv.override_changed()
        check('override dropdown: auto', pump(lambda: sv.busy_task is None, 10) and backend.get_override(stroop) == 'auto')
        fakes.stroop_install.RUNNING[0] = True
        sv.patch_clicked()
        pump(lambda: sv.busy_task is None, 10)
        check('patch off refused while STROOP runs', sv.patch == 'patched' and 'STROOP is running' in sv.msg.cget('text'),
              sv.msg.cget('text'))
        fakes.stroop_install.RUNNING[0] = False
        sv.patch_clicked()
        check('patch off', pump(lambda: sv.busy_task is None and sv.patch == 'stock', 10) and
              sv.patch_lbl.cget('text') == 'Off')
        cancel_confirm = app.confirm
        app.confirm = lambda title, text: False
        sv.patch_clicked()
        root.update()
        check('patch: declining the warning changes nothing', sv.patch == 'stock')
        app.confirm = cancel_confirm

        # -- a savestate
        app.show('maps')
        mv.open_input(state)
        check('state: one area', pump(lambda: mv.input is not None and mv.input['kind'] == 'state', 10) and
              len(mv.input['areas']) == 1 and len(mv.tree.get_children()) == 1)
        check('state: hack prefilled', mv.vars['hack'].get() == 'Fake Hack', mv.vars['hack'].get())
        mv.vars['kinds'].set('collision')
        mv.start_run()
        check('state: exports', pump(lambda: not mv.running, 15) and len(app.result.images) == 1)
        shots.take('11  A savestate: its one area')

        # -- settings remembered
        app.close()
        check('settings file written', os.path.isfile(os.environ['STROOPMAPS_SETTINGS']))
        st2 = Settings()
        check('settings: input, kind, scale, bands, STROOP remembered',
              st2['input'] == state and st2['scale'] == '6' and st2['bands'] == '1200, 400' and st2['stroop_dir'] == stroop
              and st2['light'] == 'relief' and st2['out_dir'] == out and st2['kinds'] == 'collision', str(dict(st2)))
        root2 = appmod.make_root()
        app2 = appmod.App(root2, st2)
        app2.confirm = lambda t, x: True
        check('settings: new window restores the input', app2.tasks.pump_until(lambda: app2.maps.input is not None, 10)
              and app2.maps.vars['scale'].get() == '6' and app2.stroop.dir == stroop)
        app2.close()
    finally:
        try:
            root.destroy()
        except Exception:                                   # noqa: BLE001
            pass
        if app.errors:
            check('no exceptions inside the window', False, app.errors[0][-300:])
        else:
            check('no exceptions inside the window', True)
        if not a.no_shots:
            shots.sheet(a.sheet)
        if not a.keep:
            shutil.rmtree(tmp, ignore_errors=True)
    bad = [n for n, ok, _ in RESULTS if not ok]
    print(f'\n{len(RESULTS) - len(bad)} of {len(RESULTS)} checks passed' + (f'; FAILED: {bad}' if bad else ''))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
