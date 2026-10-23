"""The window: header, tab bar (Make maps / STROOP), a banner when stand-in modules are in use, and the shared helpers
(confirm, open a path, install the current maps, close). Tk is touched only from the Tk thread (workers.Tasks)."""
import ctypes
import os
import re
import sys
import tkinter as tk
from tkinter import messagebox, ttk

import backend

from . import theme, workers
from .maps_view import MapsView
from .settings import Settings
from .stroop_view import StroopView
from .theme import px

WINDOW = (1120, 900)
CLOSE_WAIT = 6.0


class App:
    """The window: its two tabs, the settings, the shared workers and the last export."""
    def __init__(self, root, settings=None):
        self.root = root
        root.title(f'StroopMaps {backend.VERSION}')
        root.minsize(px(940), px(660))
        self.settings = settings or Settings()
        backend.ERROR_LOG[0] = os.path.join(self.settings.folder, 'last_error.log')
        theme.init_scale(root)
        self.fonts = theme.Fonts()
        theme.apply_style(root, self.fonts)
        self.tasks = workers.Tasks(root)
        self.result = None                    # backend.Result of the last export
        self.errors = []
        root.report_callback_exception = self._tk_error
        root.protocol('WM_DELETE_WINDOW', self.close)
        self._build()
        self.show(self.settings['tab'] if self.settings['tab'] in ('maps', 'stroop') else 'maps')
        if self.settings['blender']:
            self.maps.set_blender(self.settings['blender'])
        inp = self.settings['input']
        if inp and os.path.isfile(inp):
            self.root.after(150, lambda: self.maps.open_input(inp))

    def _build(self):
        f = self.fonts
        head = tk.Frame(self.root, bg=theme.NAVY)
        head.pack(fill='x', padx=px(14), pady=(px(10), 0))
        tk.Label(head, text='StroopMaps', font=f.head, fg=theme.TEXT, bg=theme.NAVY).pack(side='left')
        tk.Label(head, text='map images for STROOP\'s Map tab, from a ROM or a savestate', font=f.base, fg=theme.DIM,
                 bg=theme.NAVY).pack(side='left', padx=px(12), pady=(px(5), 0))
        self.tab_btns = {}
        tabs = tk.Frame(self.root, bg=theme.NAVY)
        tabs.pack(fill='x', padx=px(8), pady=(px(6), 0))
        for name, text in (('maps', 'Make maps'), ('stroop', 'STROOP')):
            b = ttk.Button(tabs, text=text, style='Tab.TButton', command=lambda n=name: self.show(n))
            b.pack(side='left', padx=px(2))
            self.tab_btns[name] = b
        fakes = backend.fakes_in_use()
        self.banner = tk.Label(self.root, bg='#4a3a12', fg=theme.WARN, font=f.small, anchor='w', justify='left',
                               text='')
        if fakes:
            self.banner.configure(text='  Test mode: stand-in modules are used for ' +
                                       ', '.join(n.split(' ')[0] for n in fakes) + '. The images are placeholders.')
            self.banner.pack(fill='x', padx=px(14), pady=(px(6), 0), ipady=px(3))
            theme.Tip(self.banner, '\n'.join(fakes))
        self.body = tk.Frame(self.root, bg=theme.NAVY)
        self.body.pack(fill='both', expand=True)
        self.body.rowconfigure(0, weight=1)
        self.body.columnconfigure(0, weight=1)
        self.maps = MapsView(self.body, self)
        self.stroop = StroopView(self.body, self)
        self.views = {'maps': self.maps, 'stroop': self.stroop}
        for v in self.views.values():
            v.grid(row=0, column=0, sticky='nsew')

    def show(self, name):
        """Bring tab `name` ('maps' or 'stroop') to the front."""
        self.current = name
        self.views[name].tkraise()
        for n, b in self.tab_btns.items():
            b.configure(style='TabOn.TButton' if n == name else 'Tab.TButton')
        self.settings['tab'] = name

    # ------------------------------------------------------------------ helpers
    def confirm(self, title, text):
        """Ask OK / Cancel; True for OK."""
        return messagebox.askokcancel(title, text, parent=self.root)

    def open_path(self, p):
        """Open a file or folder with its Windows program."""
        try:
            if os.name == 'nt':
                os.startfile(p)                           # noqa: S606 - a file or folder the window was given or wrote
            else:                                         # pragma: no cover
                import subprocess
                subprocess.Popen(['xdg-open', p])
        except OSError as e:
            self.maps.say('Could not open ' + p + ': ' + str(e), 'bad')

    def maps_changed(self):
        """Tell the STROOP tab a new export is there to install."""
        if hasattr(self, 'stroop'):
            self.stroop.refresh_buttons()

    def install_maps(self):
        """Install the last export (or the maps already in the output folder) into the STROOP of the STROOP tab."""
        if not self.stroop.dir_ok:
            self.show('stroop')
            self.stroop.say('Choose STROOP\'s folder first.', 'warn')
            return
        hack = self.result.hack if self.result else self.maps.vars['hack'].get().strip()
        if not hack:
            self.maps.say('Give the map set a hack name.', 'warn')
            return
        self.stroop.install(self.result, hack)
        self.maps.say('Installing into STROOP...')

    def _tk_error(self, exc, val, tb):
        import traceback
        text = ''.join(traceback.format_exception(exc, val, tb))
        self.errors.append(text)
        try:
            with open(backend.error_log_path(), 'a', encoding='utf-8') as f:
                f.write(text + '\n')
        except OSError:
            pass
        try:
            self.maps.say('Something went wrong inside the window (details in ' + backend.error_log_path() + ').', 'bad')
        except (tk.TclError, AttributeError):
            pass

    # ------------------------------------------------------------------ closing
    def close(self):
        """Save the settings and close, after asking when an export runs."""
        if self.maps.running and not self.confirm('Quit', 'An export is running. Cancel it and quit?'):
            return
        self.maps.save_settings()
        try:
            self.settings['geometry'] = self.root.geometry()
        except tk.TclError:
            pass
        self.settings.save()
        t = self.maps.run_task
        if t and t.running:
            t.cancel()
            import time
            end = time.time() + CLOSE_WAIT
            while t.thread.is_alive() and time.time() < end:
                try:
                    self.root.update()
                except tk.TclError:
                    break
                time.sleep(0.05)
        self.tasks.close()
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def set_dpi_aware():
    """Tell Windows the window scales itself (sharp text on high-dpi screens)."""
    if os.name == 'nt':
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError, AttributeError):
            pass


def make_root():
    """The Tk root, styled."""
    set_dpi_aware()
    root = tk.Tk()
    root.withdraw()
    if getattr(sys, 'frozen', False):
        try:
            root.iconbitmap(default=sys.executable)
        except tk.TclError:
            pass
    return root


def place(root, geometry=''):
    """Size and place the window: the remembered geometry when it fits the screen."""
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w, h = min(px(WINDOW[0]), sw - 60), min(px(WINDOW[1]), sh - 100)
    m = re.match(r'^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$', geometry or '')
    if m and int(m[1]) >= px(940) and int(m[2]) >= px(660) and 0 <= int(m[3]) < sw - 100 and 0 <= int(m[4]) < sh - 100:
        root.geometry(geometry)
        return
    root.geometry('%dx%d+%d+%d' % (w, h, max(0, (sw - w) // 2), max(0, (sh - h) // 3)))


def run(settings=None):
    """Open the window and run until it closes."""
    root = make_root()
    app = App(root, settings)
    place(root, app.settings['geometry'])
    root.deiconify()
    root.app = app
    root.mainloop()
    return 0
