"""STROOP Maps: the window that makes STROOP Map-tab images from a ROM or a savestate, installs them into STROOP and
switches the "Recommended picks romhack maps" patch.

    python app/main.py [--fake] [--settings FILE] [--input ROM_OR_ST]
    StroopMaps.exe [same options]
    python app/selftest.py          the check of the window with stand-in modules, writes a screenshot sheet

--fake uses the stand-ins in fakes.py for every module (otherwise only for those that cannot be imported yet).
All the real work goes through backend.py."""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def main(argv=None):
    """Start the window (or, with --check, report which modules load and exit)."""
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(description='StroopMaps')
    ap.add_argument('--fake', action='store_true', help='use stand-in modules for everything (a demo / test mode)')
    ap.add_argument('--settings', help='settings file (default %%APPDATA%%/StroopMaps/settings.json)')
    ap.add_argument('--check', metavar='FILE', help='write which modules are real / fake as JSON to FILE and exit '
                    '(the packaged exe has no console; build_dist.py uses this as its smoke test)')
    ap.add_argument('--input', help='open this ROM or savestate at start')
    a = ap.parse_args(argv)
    import backend
    backend.select(force_fake=a.fake)
    if a.check:
        import json
        if os.path.exists(a.check) and not a.check.lower().endswith('.json'):
            # a mistyped path (a savestate, a ROM) must not be overwritten by the report
            ap.error(f'--check writes a JSON report: {a.check} exists and is not a .json file')
        import tkinter
        import gui.app, gui.maps_view, gui.stroop_view, gui.thumbs          # noqa: F401  (the window's modules import)
        try:
            import PIL.ImageTk                                              # noqa: F401
            pil = True
        except ImportError:
            pil = False
        with open(a.check, 'w', encoding='utf-8') as f:
            json.dump({'mode': backend.MODE, 'why': backend.WHY, 'frozen': backend.FROZEN, 'pillow': pil,
                       'tk': tkinter.TkVersion, 'blender': backend.blender_status()}, f, indent=1)
        return 0
    from gui import app as appmod
    from gui.settings import Settings
    st = Settings(a.settings)
    if a.input:
        st['input'] = os.path.abspath(a.input)
    return appmod.run(st)


def crash_log(exc_text):
    """Append an unexpected error to %APPDATA%/StroopMaps/crash.log (best effort: a failure is ignored)."""
    base = os.environ.get('APPDATA') or os.path.expanduser('~')
    try:
        os.makedirs(os.path.join(base, 'StroopMaps'), exist_ok=True)
        with open(os.path.join(base, 'StroopMaps', 'crash.log'), 'a', encoding='utf-8') as f:
            f.write(exc_text + '\n')
    except OSError:
        pass


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:                                  # noqa: BLE001 - a windowed exe has no console to show it on
        import traceback
        crash_log(traceback.format_exc())
        raise
