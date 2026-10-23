"""The "Make maps" tab: input (ROM or savestate), the area list, the options, Run / Cancel with progress and a log, and
the results. Talks only to backend.py."""
import os
import time
import tkinter as tk
from tkinter import filedialog, ttk

import backend

from . import theme
from .theme import px
from .thumbs import ThumbGrid

CHECKED, UNCHECKED = '☑', '☐'
KIND_LABELS = (('collision', 'Collision'), ('textured', 'Textured'), ('both', 'Both'))
LIGHT_LABELS = (('stroop', 'STROOP-fitted'), ('rom', 'ROM lights'), ('relief', 'Relief (slope shade)'))
# What objects textured maps draw: geodecode.OBJECT_MODES (kept here too so the window works without it)
OBJECT_LABELS = (('static', 'Everything that doesn\'t move (default)'),
                 ('scenery', 'Everything that doesn\'t move, no collectibles'),
                 ('grabbable', 'Collision objects + grabbables at rest'),
                 ('collision', 'Collision objects only'),
                 ('all', 'All objects, enemies too (rest pose)'),
                 ('none', 'No objects'))
OBJECT_TIP = ('What the textured maps draw besides the level. Every choice leaves out Mario.\n'
              'Everything that doesn\'t move: collision objects (platforms, doors, posts) and every drawn object '
              'that isn\'t animated or built to move (trees, signs, coins, boxes). No collectibles: the same '
              'without coins, stars, keys, caps and rings. All objects: enemies and moving objects too, '
              'in their model\'s rest pose. Collision maps never draw objects (see "Collision").')


def object_mode(value):
    """A saved setting (old ones are True / False) or a label -> a mode key."""
    if value is True or value in (None, ''):
        return 'static'
    if value is False:
        return 'none'
    for k, label in OBJECT_LABELS:
        if value in (k, label):
            return k
    return 'static'


def object_label(mode):
    """The objects dropdown's label for an object mode."""
    return dict(OBJECT_LABELS)[object_mode(mode)]


def kinds_of(v):
    """The exporter's kinds for the Maps choice ('collision', 'textured' or 'both')."""
    return {'collision': ('collision',), 'textured': ('textured',), 'both': ('collision', 'textured')}[v]


class MapsView(tk.Frame):
    """The "Make maps" tab: input, areas, options, the export with its log and thumbnails."""
    def __init__(self, master, app):
        super().__init__(master, bg=theme.NAVY)
        self.app, self.fonts, self.tasks, self.st = app, app.fonts, app.tasks, app.settings
        self.input = None                 # {'path', 'kind', 'areas', 'hack'} once read
        self.checked = {}                 # (level, area) -> bool
        self.run_task = None
        self.read_task = None
        self.blender_ok, self.blender_text = True, ''
        v = self.vars = {}
        for k, val in (('input', self.st['input']), ('kinds', self.st['kinds']), ('scale_mode', self.st['scale_mode']),
                       ('scale', self.st['scale']), ('bands_mode', self.st['bands_mode']), ('bands', self.st['bands']),
                       ('light', self.st['light']), ('hack', ''), ('tag', ''),
                       ('out', self.st['out_dir'] or default_out())):
            v[k] = tk.StringVar(value=val)
        v['objects'] = tk.StringVar(value=object_label(self.st['objects']))
        v['unchanged'] = tk.BooleanVar(value=bool(self.st['show_unchanged']))
        v['objcol'] = tk.BooleanVar(value=bool(self.st['object_collision']))
        v['resume'] = tk.BooleanVar(value=False)            # per run, not remembered: stale images would be kept
        self._build()
        for var in v.values():
            var.trace_add('write', lambda *a: self.refresh())
        v['hack'].trace_add('write', lambda *a: self.suggest_tag())
        self.refresh_blender()

    # ------------------------------------------------------------------ layout
    def _build(self):
        f = self.fonts
        self.columnconfigure(0, weight=3, uniform='c')
        self.columnconfigure(1, weight=2, uniform='c')
        self.rowconfigure(0, weight=2)
        self.rowconfigure(1, weight=0)
        self.rowconfigure(2, weight=3)
        left = tk.Frame(self, bg=theme.NAVY)
        left.grid(row=0, column=0, sticky='nsew', padx=(px(14), px(7)), pady=(px(10), px(6)))
        right = tk.Frame(self, bg=theme.NAVY)
        right.grid(row=0, column=1, sticky='nsew', padx=(px(7), px(14)), pady=(px(10), px(6)))
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        # -- 1 input
        c = theme.card(left)
        c.grid(row=0, column=0, sticky='ew')
        c.columnconfigure(0, weight=1)
        theme.label(c, '1   INPUT: A ROM OR A SAVESTATE', f.small_bold, theme.DIM).grid(
            row=0, column=0, columnspan=2, sticky='w', padx=px(12), pady=(px(9), px(3)))
        self.input_entry = ttk.Entry(c, textvariable=self.vars['input'])
        self.input_entry.grid(row=1, column=0, sticky='ew', padx=(px(12), px(6)), pady=px(2))
        self.input_entry.bind('<Return>', lambda e: self.open_input(self.vars['input'].get().strip().strip('"')))
        self.input_btn = ttk.Button(c, text='Choose...', command=self.choose_input)
        self.input_btn.grid(row=1, column=1, padx=(0, px(12)))
        self.input_info = theme.label(c, 'ROM (.z64 .n64 .v64): every area it defines, read from the file, no emulator. '
                                      'Savestate (.st): its loaded area.', f.small, theme.DIM, anchor='w', justify='left',
                                      wraplength=px(560))
        self.input_info.grid(row=2, column=0, columnspan=2, sticky='ew', padx=px(12), pady=(px(2), px(9)))

        # -- 2 areas
        c = theme.card(left)
        c.grid(row=1, column=0, sticky='nsew', pady=(px(8), 0))
        c.rowconfigure(1, weight=1)
        c.columnconfigure(0, weight=1)
        top = tk.Frame(c, bg=theme.PANEL)
        top.grid(row=0, column=0, columnspan=2, sticky='ew', padx=px(12), pady=(px(9), px(3)))
        theme.label(top, '2   AREAS', f.small_bold, theme.DIM).pack(side='left')
        self.count_lbl = theme.label(top, '', f.small, theme.DIM)
        self.count_lbl.pack(side='left', padx=px(10))
        ttk.Button(top, text='None', command=lambda: self.set_all(False)).pack(side='right')
        self.unch_btn = ttk.Checkbutton(top, text='Show unchanged vanilla levels', variable=self.vars['unchanged'],
                                        command=self.reread)
        self.unch_btn.pack(side='right', padx=(0, px(14)))
        theme.Tip(self.unch_btn, 'A hack keeps some levels byte-identical to the vanilla game; STROOP ships those maps '
                                 'already, so they are hidden by default.')
        ttk.Button(top, text='All', command=lambda: self.set_all(True)).pack(side='right', padx=(0, px(6)))
        cols = ('ck', 'level', 'area', 'name', 'notes')
        self.tree = ttk.Treeview(c, columns=cols, show='headings', selectmode='browse', height=6)
        for col, text, w, anchor in (('ck', '', 34, 'center'), ('level', 'Level', 52, 'e'), ('area', 'Area', 46, 'e'),
                                     ('name', 'Name', 200, 'w'), ('notes', 'Notes', 220, 'w')):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=px(w), minwidth=px(30), anchor=anchor, stretch=col in ('name', 'notes'))
        sb = ttk.Scrollbar(c, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=1, column=0, sticky='nsew', padx=(px(12), 0), pady=(0, px(10)))
        sb.grid(row=1, column=1, sticky='ns', padx=(0, px(12)), pady=(0, px(10)))
        self.tree.bind('<ButtonRelease-1>', self._click)
        self.tree.bind('<space>', self._space)
        self.tree.tag_configure('note', foreground=theme.WARN)
        self.tree.tag_configure('dim', foreground=theme.DIM)

        # -- 3 options
        c = theme.card(right)
        c.grid(row=0, column=0, sticky='nsew')
        c.columnconfigure(1, weight=1)
        theme.label(c, '3   OPTIONS', f.small_bold, theme.DIM).grid(row=0, column=0, columnspan=3, sticky='w',
                                                                     padx=px(12), pady=(px(9), px(4)))
        r = 1

        def row(text):
            nonlocal r
            theme.label(c, text, f.base, theme.SOFT).grid(row=r, column=0, sticky='nw', padx=(px(12), px(8)), pady=px(3))
            fr = tk.Frame(c, bg=theme.PANEL)
            fr.grid(row=r, column=1, columnspan=2, sticky='ew', padx=(0, px(12)), pady=px(2))
            r += 1
            return fr

        fr = row('Maps')
        self.kind_btns = {}
        for val, text in KIND_LABELS:
            b = ttk.Radiobutton(fr, text=text, value=val, variable=self.vars['kinds'])
            b.pack(side='left', padx=(0, px(10)))
            self.kind_btns[val] = b
        self.kind_note = theme.label(c, '', f.small, theme.WARN, anchor='w', justify='left', wraplength=px(330))
        self.kind_note.grid(row=r, column=1, columnspan=2, sticky='ew', padx=(0, px(12)))
        r += 1

        fr = row('Scale')
        ttk.Radiobutton(fr, text='Auto (4 u/px ROM Manager, 8 vanilla)', value='auto',
                        variable=self.vars['scale_mode']).pack(anchor='w')
        f2 = tk.Frame(fr, bg=theme.PANEL)
        f2.pack(anchor='w')
        ttk.Radiobutton(f2, text='Custom', value='custom', variable=self.vars['scale_mode']).pack(side='left')
        self.scale_entry = ttk.Entry(f2, textvariable=self.vars['scale'], width=6)
        self.scale_entry.pack(side='left', padx=px(6))
        theme.label(f2, 'units per pixel', f.small, theme.DIM).pack(side='left')

        fr = row('Floor bands')
        f2 = tk.Frame(fr, bg=theme.PANEL)
        f2.pack(anchor='w')
        ttk.Radiobutton(f2, text='None', value='none', variable=self.vars['bands_mode']).pack(side='left')
        ttk.Radiobutton(f2, text='Auto', value='auto', variable=self.vars['bands_mode']).pack(side='left', padx=px(10))
        ttk.Radiobutton(f2, text='Custom', value='custom', variable=self.vars['bands_mode']).pack(side='left')
        self.bands_entry = ttk.Entry(fr, textvariable=self.vars['bands'])
        self.bands_entry.pack(fill='x', pady=(px(2), 0))
        theme.label(fr, 'split heights, e.g. 400,1200 (game units)', f.small, theme.DIM, anchor='w').pack(fill='x')

        fr = row('Lighting')
        self.light_btns = []
        for val, text in LIGHT_LABELS:
            b = ttk.Radiobutton(fr, text=text, value=val, variable=self.vars['light'])
            b.pack(anchor='w')
            self.light_btns.append(b)

        fr = row('Objects')
        self.obj_btn = ttk.Combobox(fr, textvariable=self.vars['objects'], state='readonly', width=44,
                                    values=[label for _, label in OBJECT_LABELS])
        self.obj_btn.pack(anchor='w')
        theme.Tip(self.obj_btn, OBJECT_TIP)

        fr = row('Collision')
        self.objcol_btn = ttk.Checkbutton(fr, text='Collision maps include collision objects',
                                          variable=self.vars['objcol'])
        self.objcol_btn.pack(anchor='w')
        theme.Tip(self.objcol_btn, 'ROM input only: also draw the triangles of objects that load a collision model '
                                   '(platforms, doors ...) into the collision maps. A savestate already holds them.')

        fr = row('Resume')
        self.resume_btn = ttk.Checkbutton(fr, text='Keep textured maps already in the output folder',
                                          variable=self.vars['resume'])
        self.resume_btn.pack(anchor='w')
        theme.Tip(self.resume_btn, 'For an export that stopped partway (a long ROM export, a closed window): areas '
                                   'whose textured images are all complete in the output folder are not rendered '
                                   'again. Only tick it with the same areas and options as the stopped run.')

        fr = row('Hack name')
        ttk.Entry(fr, textvariable=self.vars['hack']).pack(fill='x')

        fr = row('Short name')
        self.tag_entry = ttk.Entry(fr, textvariable=self.vars['tag'], width=12)
        self.tag_entry.pack(anchor='w')
        theme.label(fr, "put before every map name in STROOP's list (e.g. TLD Castle)", f.small, theme.DIM,
                    anchor='w').pack(fill='x')
        theme.Tip(self.tag_entry, 'Made from the hack name: generic words (Super Mario 64, SM64) dropped, then the '
                                  'one word left or the initials, and not one another installed hack uses. Edit it '
                                  'freely; it is remembered for this hack. Empty: the area names alone.')

        fr = row('Output folder')
        fr.columnconfigure(0, weight=1)
        ttk.Entry(fr, textvariable=self.vars['out']).grid(row=0, column=0, sticky='ew')
        ttk.Button(fr, text='Browse...', command=self.choose_out).grid(row=0, column=1, padx=(px(6), 0))

        fr = row('Blender')
        fr.columnconfigure(0, weight=1)
        self.blender_lbl = theme.label(fr, '', f.small, theme.DIM, anchor='w', justify='left', wraplength=px(260))
        self.blender_lbl.grid(row=0, column=0, sticky='ew')
        ttk.Button(fr, text='Choose...', command=self.choose_blender).grid(row=0, column=1, padx=(px(6), 0))
        theme.Tip(self.blender_lbl, lambda: self.blender_text)

        # -- run bar
        bar = tk.Frame(self, bg=theme.NAVY)
        bar.grid(row=1, column=0, columnspan=2, sticky='ew', padx=px(14), pady=(px(2), px(4)))
        bar.columnconfigure(2, weight=1)
        self.run_btn = ttk.Button(bar, text='Make maps', style='Go.TButton', command=self.start_run)
        self.run_btn.grid(row=0, column=0)
        self.cancel_btn = ttk.Button(bar, text='Cancel', style='Stop.TButton', command=self.cancel_run)
        self.cancel_btn.grid(row=0, column=1, padx=px(8))
        self.progress = ttk.Progressbar(bar, mode='determinate', maximum=1, value=0)
        self.progress.grid(row=0, column=2, sticky='ew', padx=(px(6), px(10)))
        self.status = tk.Label(bar, text='', font=f.base, fg=theme.SOFT, bg=theme.NAVY, anchor='e', justify='right',
                               wraplength=px(420))
        self.status.grid(row=0, column=3, sticky='e')

        # -- results and log
        pan = tk.PanedWindow(self, orient='horizontal', bg=theme.NAVY, sashwidth=px(6), bd=0)
        pan.grid(row=2, column=0, columnspan=2, sticky='nsew', padx=px(14), pady=(px(2), px(12)))
        rc = theme.card(pan)
        rc.columnconfigure(0, weight=1)
        rc.rowconfigure(1, weight=1)
        top = tk.Frame(rc, bg=theme.PANEL)
        top.grid(row=0, column=0, sticky='ew', padx=px(12), pady=(px(8), px(4)))
        theme.label(top, 'RESULTS', f.small_bold, theme.DIM).pack(side='left')
        self.result_lbl = theme.label(top, 'double-click an image to open it', f.small, theme.DIM)
        self.result_lbl.pack(side='left', padx=px(10))
        self.install_btn = ttk.Button(top, text='Install into STROOP', command=self.install_clicked)
        self.install_btn.pack(side='right')
        self.folder_btn = ttk.Button(top, text='Open folder', command=self.open_folder)
        self.folder_btn.pack(side='right', padx=(0, px(6)))
        self.grid_view = ThumbGrid(rc, self.app)
        self.grid_view.grid(row=1, column=0, sticky='nsew', padx=px(10), pady=(0, px(10)))
        lc = theme.card(pan)
        lc.columnconfigure(0, weight=1)
        lc.rowconfigure(1, weight=1)
        theme.label(lc, 'LOG', f.small_bold, theme.DIM).grid(row=0, column=0, sticky='w', padx=px(12), pady=(px(8), px(4)))
        self.log_text = tk.Text(lc, height=6, bg=theme.FIELD, fg=theme.SOFT, insertbackground=theme.TEXT, relief='flat',
                                font=f.mono, wrap='word', state='disabled', highlightthickness=0, padx=px(6), pady=px(4))
        lsb = ttk.Scrollbar(lc, orient='vertical', command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsb.set)
        self.log_text.grid(row=1, column=0, sticky='nsew', padx=(px(10), 0), pady=(0, px(10)))
        lsb.grid(row=1, column=1, sticky='ns', padx=(0, px(10)), pady=(0, px(10)))
        self.log_text.tag_configure('err', foreground=theme.BAD)
        self.log_text.tag_configure('ok', foreground=theme.OK)
        self.log_text.tag_configure('dim', foreground=theme.DIM)
        pan.add(rc, minsize=px(300), stretch='always')
        pan.add(lc, minsize=px(240), stretch='always')

    # ------------------------------------------------------------------ small things
    def log(self, text, tag=None):
        """Add a line to the log pane."""
        t = self.log_text
        t.configure(state='normal')
        t.insert('end', text.rstrip('\n') + '\n', tag or ())
        t.see('end')
        t.configure(state='disabled')

    def say(self, text, level='info'):
        """Show a one-line status under the buttons."""
        col = {'info': theme.SOFT, 'ok': theme.OK, 'bad': theme.BAD, 'warn': theme.WARN}[level]
        self.status.configure(text=text, fg=col)

    @property
    def running(self):
        """True while an export runs."""
        return self.run_task is not None and self.run_task.running

    @property
    def reading(self):
        """True while the input is being read."""
        return self.read_task is not None and self.read_task.running

    # ------------------------------------------------------------------ input
    def choose_input(self):
        """Ask for a ROM or savestate and read it."""
        p = filedialog.askopenfilename(
            title='Choose a ROM or a savestate', initialdir=self.st['input_dir'] or None,
            filetypes=[('ROM or savestate', '*.z64 *.n64 *.v64 *.st'), ('ROMs', '*.z64 *.n64 *.v64'),
                       ('Savestates', '*.st'), ('All files', '*.*')])
        if p:
            self.open_input(os.path.normpath(p))

    def reread(self):
        """The "unchanged levels" box changed: list the ROM again."""
        if self.input and self.input['kind'] == 'rom' and not self.running and not self.reading:
            self.open_input(self.input['path'], keep_hack=True)
        elif self.running or self.reading:
            self.vars['unchanged'].set(not self.vars['unchanged'].get())

    def open_input(self, path, keep_hack=False):
        """Read a ROM's areas / a savestate's area in a worker thread."""
        if self.running or self.reading or not path:
            return
        self._keep_hack = self.vars['hack'].get() if keep_hack else None
        unch = bool(self.vars['unchanged'].get())
        path = os.path.normpath(path)
        self.vars['input'].set(path)
        self.input = None
        self.checked = {}
        self._fill_areas([])
        self.say('Reading ' + os.path.basename(path) + ' ...')
        self.input_info.configure(text='Reading...', fg=theme.DIM)
        self.read_task = self.tasks.start('read', lambda task, p: (p,) + backend.read_input(p, unch), path,
                                          on_done=self._read_done, on_error=self._read_failed)
        self.refresh()

    def _read_done(self, task, res):
        path, kind, areas, hack = res
        self.input = {'path': path, 'kind': kind, 'areas': areas, 'hack': hack}
        self.checked = {a.key: not a.unchanged for a in areas}
        self.vars['hack'].set(self._keep_hack or hack)
        self.st['input'] = path
        self.st['input_dir'] = os.path.dirname(path)
        what = 'ROM' if kind == 'rom' else 'Savestate'
        self.input_info.configure(text=f'{what}: {len(areas)} area{"s" if len(areas) != 1 else ""}. '
                                       f'{"No emulator is needed: the areas are read from the file." if kind == "rom" else ""}',
                                  fg=theme.SOFT)
        self.unch_btn.state(['!disabled'] if kind == 'rom' else ['disabled'])
        self._fill_areas(areas)
        self.say('')
        self.log(f'{os.path.basename(path)}: {what.lower()}, {len(areas)} area(s)', 'dim')
        self.refresh()

    def _read_failed(self, task, exc, tb):
        msg = backend.explain(exc)
        self.input_info.configure(text=msg, fg=theme.BAD)
        self.say(msg, 'bad')
        self.log('Could not read the input: ' + msg, 'err')
        self.refresh()

    # ------------------------------------------------------------------ areas
    def _fill_areas(self, areas):
        self.tree.delete(*self.tree.get_children())
        for a in areas:
            # the reader's "segment 0x0E = ROM ..." lines say where the data sits: not worth the Notes column
            a.notes = '; '.join(n for n in a.notes.split('; ') if not n.startswith('segment 0x'))
            self.tree.insert('', 'end', iid=f'{a.level}:{a.area}', tags=('dim',) if a.unchanged else
                             ('note',) if a.notes else (),
                             values=(UNCHECKED if a.unchanged else CHECKED, a.level, a.area, a.name, a.notes))
        self._count()

    def _count(self):
        n = sum(1 for v in self.checked.values() if v)
        self.count_lbl.configure(text=f'{n} of {len(self.checked)} selected' if self.checked else '')

    def toggle(self, iid):
        """Tick or untick one area's row."""
        if self.running or iid not in self.tree.get_children():
            return
        lv, ar = map(int, iid.split(':'))
        self.checked[(lv, ar)] = not self.checked.get((lv, ar), False)
        self.tree.set(iid, 'ck', CHECKED if self.checked[(lv, ar)] else UNCHECKED)
        self._count()
        self.refresh()

    def _click(self, e):
        if self.tree.identify_region(e.x, e.y) == 'cell':
            iid = self.tree.identify_row(e.y)
            if iid:
                self.toggle(iid)

    def _space(self, e):
        for iid in self.tree.selection():
            self.toggle(iid)
        return 'break'

    def set_all(self, on):
        """Tick or untick every area."""
        if self.running:
            return
        for k in self.checked:
            self.checked[k] = on
        for iid in self.tree.get_children():
            self.tree.set(iid, 'ck', CHECKED if on else UNCHECKED)
        self._count()
        self.refresh()

    def selected_areas(self):
        """The ticked areas, in list order."""
        return [a for a in (self.input['areas'] if self.input else []) if self.checked.get(a.key)]

    # ------------------------------------------------------------------ options
    def choose_out(self):
        """Ask for the output folder."""
        d = filedialog.askdirectory(title='Output folder', initialdir=self.vars['out'].get() or None)
        if d:
            self.vars['out'].set(os.path.normpath(d))

    def choose_blender(self):
        """Ask for blender.exe."""
        p = filedialog.askopenfilename(title='Choose blender.exe', filetypes=[('Blender', 'blender.exe'), ('All', '*.*')])
        if p:
            self.set_blender(os.path.normpath(p))

    def set_blender(self, p):
        """Use (and remember) the Blender at `p`."""
        backend.set_blender(p)
        self.st['blender'] = p
        self.refresh_blender()

    def refresh_blender(self):
        """Look for Blender again; textured maps are switched off without it."""
        ok, text = backend.blender_status()
        self.blender_ok, self.blender_text = ok, text
        self.blender_lbl.configure(text=(os.path.basename(os.path.dirname(text)) + ' found' if ok else
                                         'not found: textured maps are off'), fg=theme.OK if ok else theme.WARN)
        if not ok and self.vars['kinds'].get() != 'collision':
            self.vars['kinds'].set('collision')
        self.refresh()

    def collect(self):
        """(options, error) from the option widgets."""
        v = self.vars
        opts = {'scale': None, 'bands': 'none', 'light': 'stroop', 'relief': False, 'objects': object_mode(v['objects'].get())}
        if v['scale_mode'].get() == 'custom':
            try:
                s = float(v['scale'].get().replace(',', '.'))
            except ValueError:
                return None, 'Scale must be a number (units per pixel).'
            if not 0.25 <= s <= 256:
                return None, 'Scale must be between 0.25 and 256 units per pixel.'
            opts['scale'] = s
        bm = v['bands_mode'].get()
        if bm == 'auto':
            opts['bands'] = 'auto'
        elif bm == 'custom':
            parts = [p for p in v['bands'].get().replace(';', ',').replace(' ', ',').split(',') if p]
            try:
                ys = sorted(float(p) for p in parts)
            except ValueError:
                return None, 'Floor bands: write heights separated by commas, e.g. 400,1200.'
            if not ys:
                return None, 'Floor bands: custom needs at least one split height.'
            opts['bands'] = ','.join(('%g' % y) for y in ys)
        lt = v['light'].get()
        opts['light'] = 'rom' if lt == 'rom' else 'stroop'
        opts['relief'] = lt == 'relief'
        return opts, ''

    # ------------------------------------------------------------------ enabling
    def why_not(self):
        """Why an export can't start now ('' when it can)."""
        if self.reading:
            return 'Reading the input...'
        if not self.input:
            return 'Choose a ROM or a savestate.'
        if not self.selected_areas():
            return 'Tick at least one area.'
        if not self.vars['hack'].get().strip():
            return 'Give the map set a hack name.'
        if not self.vars['out'].get().strip():
            return 'Choose an output folder.'
        k = self.vars['kinds'].get()
        if k != 'collision' and not self.blender_ok:
            return self.blender_text
        return ''

    def refresh(self):
        """Enable or disable the widgets for the current state."""
        if not hasattr(self, 'run_btn'):
            return
        busy = self.running
        why = self.why_not()
        self.run_btn.state(['disabled'] if busy or why else ['!disabled'])
        self.cancel_btn.state(['!disabled'] if busy else ['disabled'])
        self.input_btn.state(['disabled'] if busy or self.reading else ['!disabled'])
        textured = self.vars['kinds'].get() != 'collision'
        for val, b in self.kind_btns.items():
            b.state(['disabled'] if (val != 'collision' and not self.blender_ok) or busy else ['!disabled'])
        self.kind_note.configure(text='' if self.blender_ok else 'Textured maps need Blender: ' + self.blender_text)
        self.objcol_btn.state(['!disabled'] if not busy and not (self.input and self.input['kind'] == 'state')
                              else ['disabled'])
        self.resume_btn.state(['disabled'] if busy or self.vars['kinds'].get() == 'collision' else ['!disabled'])
        for b in self.light_btns:
            b.state(['!disabled'] if textured and not busy else ['disabled'])
        self.obj_btn.configure(state='readonly' if textured and not busy else 'disabled')
        self.scale_entry.state(['!disabled'] if self.vars['scale_mode'].get() == 'custom' else ['disabled'])
        self.bands_entry.state(['!disabled'] if self.vars['bands_mode'].get() == 'custom' else ['disabled'])
        if why and not busy and self.input is not None and not self.status.cget('text'):
            pass
        has = self.app.result is not None or self.out_has_maps()
        self.install_btn.state(['!disabled'] if has and not busy else ['disabled'])
        self.folder_btn.state(['!disabled'] if os.path.isdir(self.vars['out'].get().strip() or '?') else ['disabled'])
        self.app.maps_changed()

    def out_has_maps(self):
        """True when the output folder holds exported maps (so Install can use them)."""
        return os.path.isdir(os.path.join(self.vars['out'].get().strip() or '?', 'Map Images'))

    # ------------------------------------------------------------------ run
    def start_run(self):
        """Start the export with the current options in a worker thread."""
        why = self.why_not()
        if why or self.running:
            self.say(why, 'warn')
            return
        opts, err = self.collect()
        if err:
            self.say(err, 'bad')
            return
        inp = self.input
        areas = self.selected_areas()
        kinds = kinds_of(self.vars['kinds'].get())
        out = os.path.normpath(self.vars['out'].get().strip())
        hack = self.vars['hack'].get().strip()
        tag = self.vars['tag'].get().strip()
        opts['name_prefix'] = tag
        self.st['tags'] = dict(self.st.get('tags') or {}, **{hack: tag})
        self.save_settings()
        self.grid_view.clear()
        self.app.result = None
        self.progress.configure(value=0, maximum=1)
        self.say('Working...')
        self.log(f'--- {time.strftime("%H:%M:%S")}  {hack}: {len(areas)} area(s), {"+".join(kinds)} -> {out}', 'dim')
        t0 = time.time()
        objcol = bool(self.vars['objcol'].get()) and inp['kind'] == 'rom'
        opts['resume'] = bool(self.vars['resume'].get())

        def work(task):
            return backend.run_export(inp['path'], inp['kind'], areas, kinds, out, hack, opts,
                                      log=lambda s: self.tasks.post(self.log, s),
                                      progress=lambda d, n: task.progress((d, n)), cancel=lambda: task.cancelled,
                                      object_collision=objcol)
        self.run_task = self.tasks.start('export', work, on_done=lambda t, r: self._run_done(r, t0),
                                         on_error=self._run_failed, on_progress=self._run_progress)
        self.refresh()

    def _run_progress(self, task, val):
        d, n = val
        self.progress.configure(maximum=max(1, n), value=d)
        self.say(f'{d} of {n} images' + (' (cancelling after this one)' if task.cancelled else ''))

    def _run_done(self, res, t0):
        self.run_task = None
        secs = time.time() - t0
        self.app.result = res
        self.grid_view.set_images(res.images)
        self.result_lbl.configure(text=f'{len(res.images)} image(s) in {res.out_dir}')
        for w in res.warnings:
            self.log('warning: ' + w, 'dim')
        for w in res.errors:
            self.log('error: ' + w, 'err')
        if res.errors and not res.cancelled:
            self.say(f'Done with problems: {res.errors[0][:200]}', 'warn')
        elif res.cancelled:
            self.say(f'Cancelled after {len(res.images)} image(s).', 'warn')
            self.log('Cancelled.', 'err')
        else:
            self.say(f'Done: {len(res.images)} image(s) in {secs:.0f} s.', 'ok')
            self.log(f'Done: {len(res.images)} image(s) in {res.out_dir}', 'ok')
        self.progress.configure(value=self.progress.cget('maximum'))
        self.refresh()

    def _run_failed(self, task, exc, tb):
        self.run_task = None
        if isinstance(exc, backend.Cancelled):
            self.say('Cancelled.', 'warn')
            self.log('Cancelled.', 'err')
        else:
            msg = backend.explain(exc)
            self.say(msg, 'bad')
            self.log('Failed: ' + msg, 'err')
        self.refresh()

    def cancel_run(self):
        """Ask the running export to stop after its current image."""
        if self.running:
            self.run_task.cancel()
            self.say('Cancelling after the current image...', 'warn')
            self.log('Cancel requested (the exporter stops between images).', 'dim')

    # ------------------------------------------------------------------ results
    def open_folder(self):
        """Open the output folder in Explorer."""
        d = self.app.result.out_dir if self.app.result else self.vars['out'].get().strip()
        self.app.open_path(d)

    def install_clicked(self):
        """Install the last export (the STROOP tab does the work)."""
        self.app.install_maps()

    def suggest_tag(self):
        """Fill the short name for the hack name now in the box (remembered, installed, or made from the name)."""
        hack = self.vars['hack'].get().strip()
        tags = self.st.get('tags') or {}
        self.vars['tag'].set(backend.suggest_tag(hack, self.st.get('stroop_dir', ''), tags.get(hack)) if hack else '')

    # ------------------------------------------------------------------ settings
    def save_settings(self):
        """Copy the options into the settings (saved when the window closes)."""
        v, st = self.vars, self.st
        st.update(kinds=v['kinds'].get(), scale_mode=v['scale_mode'].get(), scale=v['scale'].get(),
                  bands_mode=v['bands_mode'].get(), bands=v['bands'].get(), light=v['light'].get(),
                  objects=object_mode(v['objects'].get()), out_dir=v['out'].get(), blender=st['blender'],
                  show_unchanged=bool(v['unchanged'].get()), object_collision=bool(v['objcol'].get()))
        if self.input:
            st['input'] = self.input['path']


def default_out():
    """The default output folder: Documents/STROOP Maps in the home folder."""
    return os.path.join(os.path.expanduser('~'), 'Documents', 'STROOP Maps')
