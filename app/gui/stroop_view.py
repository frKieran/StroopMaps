"""The "STROOP" tab: which STROOP, its version, the installed map sets (Install / Uninstall) and the switch that makes
STROOP's "Recommended" map pick a romhack's maps. Talks only to backend.py."""
import os
import tkinter as tk
from tkinter import filedialog, ttk

import backend

from . import theme
from .theme import px

AUTO_TEXT, OFF_TEXT = 'Auto-detect the running hack', 'Off (never a hack\'s maps)'
PATCH_NOTE = ("Recommended picks romhack maps: with this on, the Map tab's \"Recommended\" choice picks the maps tagged for "
              "the running hack instead of STROOP's blank ROM-hack map. This swaps STROOP's program files (the patched "
              "build replaces the stock one; switching off puts the stock one back). STROOP must be closed.")


class StroopView(tk.Frame):
    """The "STROOP" tab: the folder, the installed map sets, the "Recommended" switch and override."""
    def __init__(self, master, app):
        super().__init__(master, bg=theme.NAVY)
        self.app, self.fonts, self.tasks, self.st = app, app.fonts, app.tasks, app.settings
        self.dir_var = tk.StringVar(value=self.st['stroop_dir'])
        self.sets = []
        self.version_full = ''
        self.patch = 'unknown'
        self.dir_ok = False
        self.busy_task = None
        self._build()
        if not self.dir_var.get():
            found = backend.find_stroop()
            if found:
                self.dir_var.set(found)
        self.dir_var.trace_add('write', lambda *a: self.refresh())
        self.refresh()

    def _build(self):
        f = self.fonts
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # -- folder and version
        c = theme.card(self)
        c.grid(row=0, column=0, sticky='ew', padx=px(14), pady=(px(10), px(6)))
        c.columnconfigure(0, weight=1)
        theme.label(c, 'STROOP FOLDER', f.small_bold, theme.DIM).grid(row=0, column=0, columnspan=3, sticky='w',
                                                                      padx=px(12), pady=(px(9), px(3)))
        ttk.Entry(c, textvariable=self.dir_var).grid(row=1, column=0, sticky='ew', padx=(px(12), px(6)), pady=px(2))
        ttk.Button(c, text='Browse...', command=self.browse).grid(row=1, column=1, padx=(0, px(6)))
        ttk.Button(c, text='Detect', command=self.detect).grid(row=1, column=2, padx=(0, px(12)))
        self.version_lbl = theme.label(c, '', f.title, theme.TEXT, anchor='w')
        self.version_lbl.grid(row=2, column=0, columnspan=3, sticky='ew', padx=px(12), pady=(px(4), px(1)))
        self.version_note = theme.label(c, '', f.small, theme.DIM, anchor='w', justify='left', wraplength=px(900))
        self.version_note.grid(row=3, column=0, columnspan=3, sticky='ew', padx=px(12), pady=(0, px(9)))
        theme.Tip(self.version_lbl, lambda: self.version_full)

        # -- installed sets
        c = theme.card(self)
        c.grid(row=1, column=0, sticky='nsew', padx=px(14), pady=px(6))
        c.columnconfigure(0, weight=1)
        c.rowconfigure(1, weight=1)
        top = tk.Frame(c, bg=theme.PANEL)
        top.grid(row=0, column=0, columnspan=2, sticky='ew', padx=px(12), pady=(px(9), px(3)))
        theme.label(top, 'MAP SETS INSTALLED IN THIS STROOP', f.small_bold, theme.DIM).pack(side='left')
        self.uninstall_btn = ttk.Button(top, text='Uninstall selected', command=self.uninstall_clicked)
        self.uninstall_btn.pack(side='right')
        self.install_btn = ttk.Button(top, text='Install the maps from Make maps', command=self.app.install_maps)
        self.install_btn.pack(side='right', padx=(0, px(6)))
        self.tree = ttk.Treeview(c, columns=('hack', 'entries', 'kinds'), show='headings', selectmode='browse', height=5)
        for col, text, w in (('hack', 'Hack', 260), ('entries', 'Entries', 80), ('kinds', 'Kinds', 220)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=px(w), anchor='w')
        self.tree.grid(row=1, column=0, sticky='nsew', padx=(px(12), 0), pady=(0, px(6)))
        sb = ttk.Scrollbar(c, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.grid(row=1, column=1, sticky='ns', padx=(0, px(12)), pady=(0, px(6)))
        self.tree.bind('<<TreeviewSelect>>', lambda e: self.refresh_buttons())
        self.sets_note = theme.label(c, '', f.small, theme.DIM, anchor='w', justify='left', wraplength=px(900))
        self.sets_note.grid(row=2, column=0, columnspan=2, sticky='ew', padx=px(12), pady=(0, px(9)))

        # -- the recommended switch
        c = theme.card(self)
        c.grid(row=2, column=0, sticky='ew', padx=px(14), pady=(px(6), px(12)))
        c.columnconfigure(1, weight=1)
        theme.label(c, 'RECOMMENDED PICKS ROMHACK MAPS', f.small_bold, theme.DIM).grid(
            row=0, column=0, columnspan=3, sticky='w', padx=px(12), pady=(px(9), px(3)))
        self.patch_lbl = theme.label(c, '', f.outcome, theme.TEXT, anchor='w')
        self.patch_lbl.grid(row=1, column=0, sticky='w', padx=px(12))
        self.patch_btn = ttk.Button(c, text='Turn on', style='Go.TButton', command=self.patch_clicked)
        self.patch_btn.grid(row=1, column=2, padx=px(12), pady=px(4))
        self.patch_detail = theme.label(c, '', f.small, theme.DIM, anchor='w')
        self.patch_detail.grid(row=2, column=0, columnspan=3, sticky='ew', padx=px(12))
        theme.label(c, PATCH_NOTE, f.small, theme.SOFT, anchor='w', justify='left', wraplength=px(900)).grid(
            row=3, column=0, columnspan=3, sticky='ew', padx=px(12), pady=(px(4), px(6)))
        orow = tk.Frame(c, bg=theme.PANEL)
        orow.grid(row=4, column=0, columnspan=3, sticky='ew', padx=px(12), pady=(0, px(10)))
        theme.label(orow, 'Recommended uses:', f.base, theme.SOFT).pack(side='left')
        self.override_var = tk.StringVar(value=AUTO_TEXT)
        self.override_box = ttk.Combobox(orow, textvariable=self.override_var, state='readonly', width=34,
                                         values=[AUTO_TEXT, OFF_TEXT])
        self.override_box.pack(side='left', padx=px(8))
        self.override_box.bind('<<ComboboxSelected>>', self.override_changed)
        theme.label(orow, 'read by the patched STROOP when it starts; auto-detect recognises the running hack by its '
                    'code', f.small, theme.DIM).pack(side='left')
        self.msg = theme.label(self, '', f.base, theme.SOFT, bg=theme.NAVY, anchor='w', justify='left',
                               wraplength=px(980))
        self.msg.grid(row=3, column=0, sticky='ew', padx=px(16), pady=(0, px(8)))

    # ------------------------------------------------------------------ state
    @property
    def dir(self):
        """The STROOP folder in the box."""
        return self.dir_var.get().strip().strip('"')

    def say(self, text, level='info'):
        """Show a one-line status on the tab."""
        col = {'info': theme.SOFT, 'ok': theme.OK, 'bad': theme.BAD, 'warn': theme.WARN}[level]
        self.msg.configure(text=text, fg=col)

    def browse(self):
        """Ask for STROOP's folder."""
        d = filedialog.askdirectory(title="Choose STROOP's folder (the one with STROOP.exe)", initialdir=self.dir or None)
        if d:
            self.dir_var.set(os.path.normpath(d))

    def detect(self):
        """Fill in a STROOP folder found without asking."""
        found = backend.find_stroop()
        if found:
            self.dir_var.set(found)
            self.say('Found STROOP at ' + found, 'ok')
        else:
            self.say('STROOP was not found automatically; use Browse to choose its folder.', 'warn')

    def refresh(self):
        """Read the folder again: version, installed sets, patch state, override."""
        d = self.dir
        ok, why = backend.check_stroop_dir(d)
        self.dir_ok = ok
        self.sets = []
        self.tree.delete(*self.tree.get_children())
        if ok:
            self.st['stroop_dir'] = d
            short, full = backend.stroop_version(d)
            self.version_full = full
            self.version_lbl.configure(text=('STROOP ' + short) if short else 'STROOP (no VERSION.txt, version unknown)',
                                       fg=theme.TEXT)
            self.version_note.configure(text=(full.splitlines()[0] if full else ''))
            try:
                self.sets = backend.installed_sets(d)
            except Exception as e:                       # noqa: BLE001
                self.sets_note.configure(text='Could not read the installed sets: ' + backend.explain(e), fg=theme.BAD)
            else:
                self.sets_note.configure(
                    text='' if self.sets else 'No map sets are installed yet. Make maps, then install them.', fg=theme.DIM)
            for s in self.sets:
                self.tree.insert('', 'end', iid=s.hack, values=(s.hack, '' if s.entries is None else s.entries,
                                                            s.kinds + ('  (installed by hand)' if s.legacy else '')))
            self.patch = backend.patch_status(d)
            self.fill_override()
        else:
            self.version_full = ''
            self.version_lbl.configure(text='STROOP: ' + why, fg=theme.WARN if d else theme.DIM)
            self.version_note.configure(text='')
            self.sets_note.configure(text='', fg=theme.DIM)
            self.patch = 'unknown'
            self.override_box.configure(values=[AUTO_TEXT, OFF_TEXT])
            self.override_var.set(AUTO_TEXT)
        self.show_patch()
        self.refresh_buttons()

    def fill_override(self):
        """The dropdown: auto-detect / off / each installed hack (and the stored override if it names another)."""
        cur = backend.get_override(self.dir)
        names = [s.hack for s in self.sets]
        if cur not in ('auto', 'off') and cur not in names:
            names.append(cur)
        self.override_map = {AUTO_TEXT: 'auto', OFF_TEXT: 'off'}
        for n in names:
            self.override_map['Always ' + n] = n
        self.override_box.configure(values=list(self.override_map))
        self.override_var.set(next((k for k, v in self.override_map.items() if v == cur), AUTO_TEXT))

    def override_changed(self, _e=None):
        """Write the chosen hack override into STROOP's file."""
        if not self.dir_ok or (self.busy_task is not None and self.busy_task.running):
            return
        value = self.override_map.get(self.override_var.get(), 'auto')
        d = self.dir
        self.say('Saving...')
        self._run('override', lambda log: backend.set_override(d, value),
                  {'auto': 'Recommended auto-detects the running hack.', 'off': 'Recommended ignores romhack maps.'}
                  .get(value, f'Recommended always uses the "{value}" maps.'))

    def show_patch(self):
        """Show the patch state and the switch's label."""
        txt, col, det = {
            'patched': ('On', theme.OK, 'STROOP is the patched build.'),
            'stock': ('Off', theme.SOFT, 'STROOP is the stock build.'),
        }.get(self.patch, ('Unknown', theme.WARN, 'STROOP\'s program files match neither build (or no STROOP folder).'))
        self.patch_lbl.configure(text=txt, fg=col)
        self.patch_detail.configure(text=det)
        self.patch_btn.configure(text='Turn off' if self.patch == 'patched' else 'Turn on',
                                 style='Stop.TButton' if self.patch == 'patched' else 'Go.TButton')

    def refresh_buttons(self):
        """Enable or disable the buttons for the current state."""
        busy = self.busy_task is not None and self.busy_task.running
        ok = self.dir_ok and not busy
        self.uninstall_btn.state(['!disabled'] if ok and self.tree.selection() else ['disabled'])
        has = self.app.result is not None or (self.app.maps.out_has_maps() if hasattr(self.app, 'maps') else False)
        self.install_btn.state(['!disabled'] if ok and has else ['disabled'])
        self.patch_btn.state(['!disabled'] if ok and self.patch in ('stock', 'patched') else ['disabled'])
        self.override_box.state(['!disabled'] if ok else ['disabled'])

    # ------------------------------------------------------------------ actions
    def _run(self, name, fn, done_text):
        def done(task, res):
            self.busy_task = None
            text = done_text + (f' ({res})' if isinstance(res, str) and res else '')
            self.say(text, 'ok')
            self.app.maps.log(text, 'ok')
            self.refresh()

        def failed(task, exc, tb):
            self.busy_task = None
            msg = backend.explain(exc)
            self.say(msg, 'bad')
            self.app.maps.log(msg, 'err')
            self.refresh()
        self.busy_task = self.tasks.start(name, lambda task: fn(lambda s: self.tasks.post(self.app.maps.log, s)),
                                          on_done=done, on_error=failed)
        self.refresh_buttons()

    def install(self, result, hack):
        """Install an export's maps into the folder, in a worker thread."""
        if self.busy_task is not None and self.busy_task.running:
            return
        d = self.dir
        out = os.path.normpath(self.app.maps.vars['out'].get().strip() or '.')
        self.say('Installing...')
        self._run('install', lambda log: backend.install(result, d, hack, log) if result is not None
                  else backend.install_dir(out, d, hack, log),
                  f'Installed "{hack}" into STROOP. Restart STROOP to see the maps.')

    def uninstall_clicked(self):
        """Remove the selected map set, after asking."""
        sel = self.tree.selection()
        if not sel or not self.dir_ok:
            return
        hack = sel[0]
        if not self.app.confirm('Uninstall', f'Remove the "{hack}" map set (its images and Map entries) from this STROOP?'):
            return
        d = self.dir
        self.say('Uninstalling...')
        self._run('uninstall', lambda log: backend.uninstall(d, hack, log), f'Uninstalled "{hack}".')

    def patch_clicked(self):
        """Switch the "Recommended" patch on or off."""
        if not self.dir_ok or self.patch not in ('stock', 'patched'):
            return
        on = self.patch != 'patched'
        if not self.app.confirm(
                'Recommended picks romhack maps',
                ('Turn the patch ON?' if on else 'Turn the patch OFF?') +
                '\n\nThis swaps STROOP\'s program files between the stock and the patched build. STROOP must be closed '
                'while it happens.'):
            return
        d = self.dir
        self.say('Switching...')
        self._run('patch', lambda log: backend.set_patch(d, on, log),
                  'Recommended now picks romhack maps (patched STROOP).' if on else 'STROOP is stock again.')
