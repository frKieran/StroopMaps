"""The results list: a scrollable grid of thumbnails (double-click opens the image). Thumbnails are made in a worker
thread with Pillow when it is there (the full-size Island images are 6600 px wide), else with Tk's own PhotoImage."""
import tkinter as tk
from tkinter import ttk

from . import theme
from .theme import px

try:
    from PIL import Image, ImageTk
    Image.MAX_IMAGE_PIXELS = None           # our own images; the largest is 16384 px on a side
except ImportError:                          # pragma: no cover
    Image = ImageTk = None

THUMB = 150


def make_thumb(path):
    """A THUMB-sized PIL image of a map PNG on the cell colour (maps are transparent where there is no floor)."""
    im = Image.open(path)
    im.draft('RGBA', (THUMB * 2, THUMB * 2))
    im = im.convert('RGBA')
    im.thumbnail((THUMB, THUMB), Image.BILINEAR)
    bg = Image.new('RGBA', im.size, theme.CELL)
    bg.alpha_composite(im)
    return bg.convert('RGB')


class ThumbGrid(tk.Frame):
    """The results pane: a scrolling grid of the exported images' thumbnails."""
    def __init__(self, master, app):
        super().__init__(master, bg=theme.PANEL)
        self.app, self.fonts = app, app.fonts
        self.tiles = []              # dicts: frame, img_label, photo, path
        self.canvas = tk.Canvas(self, bg=theme.FIELD, highlightthickness=0, height=px(150))
        sb = ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        self.inner = tk.Frame(self.canvas, bg=theme.FIELD)
        self.win = self.canvas.create_window(0, 0, window=self.inner, anchor='nw')
        self.empty = tk.Label(self.inner, text='No maps yet.', font=self.fonts.base, fg=theme.DIM, bg=theme.FIELD)
        self.empty.grid(row=0, column=0, padx=px(12), pady=px(12))
        self.inner.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', self._resize)
        self.canvas.bind('<Enter>', lambda e: self.canvas.bind_all('<MouseWheel>', self._wheel))
        self.canvas.bind('<Leave>', lambda e: self.canvas.unbind_all('<MouseWheel>'))
        self.task = None
        self._cols = 0

    def _wheel(self, e):
        self.canvas.yview_scroll(-1 if e.delta > 0 else 1, 'units')

    def _resize(self, e):
        self.canvas.itemconfigure(self.win, width=e.width)
        self._layout()

    def _layout(self):
        w = self.canvas.winfo_width()
        cols = max(1, w // (px(THUMB) + px(16)))
        if cols == self._cols and all(t['frame'].winfo_manager() for t in self.tiles):
            return
        self._cols = cols
        for i, t in enumerate(self.tiles):
            t['frame'].grid(row=i // cols, column=i % cols, padx=px(6), pady=px(6), sticky='n')

    def clear(self):
        """Remove every thumbnail."""
        if self.task is not None and self.task.running:
            self.task.cancel()
        for t in self.tiles:
            t['frame'].destroy()
        self.tiles = []
        self._cols = 0
        self.empty.grid(row=0, column=0, padx=px(12), pady=px(12))
        self.canvas.yview_moveto(0)

    def set_images(self, images):
        """images: backend.ImageInfo list."""
        self.clear()
        if not images:
            self.empty.configure(text='The export made no images.')
            return
        self.empty.grid_forget()
        for im in images:
            f = tk.Frame(self.inner, bg=theme.CELL, highlightbackground=theme.LINE, highlightthickness=1)
            lab = tk.Label(f, text='...', font=self.fonts.small, fg=theme.DIM, bg=theme.CELL, width=int(THUMB / 7),
                           height=int(THUMB / 16))
            lab.pack()
            cap = tk.Label(f, text=im.name, font=self.fonts.small, fg=theme.SOFT, bg=theme.CELL, wraplength=px(THUMB))
            cap.pack()
            tag = tk.Label(f, text=im.kind, font=self.fonts.small, fg=theme.DIM, bg=theme.CELL)
            tag.pack()
            for w in (f, lab, cap, tag):
                w.bind('<Double-Button-1>', lambda e, p=im.path: self.open_path(p))
            self.tiles.append({'frame': f, 'label': lab, 'photo': None, 'path': im.path})
        self._layout()
        self.task = self.app.tasks.start('thumbs', self._work, [im.path for im in images],
                                         on_progress=self._got, on_error=lambda t, e, tb: None)

    def open_path(self, p):
        """Open an image with its Windows program."""
        self.app.open_path(p)

    # -- thumbnail work -----------------------------------------------------------------------------------------
    def _work(self, task, paths):
        for i, p in enumerate(paths):
            if task.cancelled:
                return
            try:
                if Image is None:
                    task.progress((i, p))
                else:
                    task.progress((i, make_thumb(p)))
            except Exception:                    # noqa: BLE001 - a broken image only loses its preview
                task.progress((i, None))

    def _got(self, task, val):
        i, obj = val
        if i >= len(self.tiles):
            return
        t = self.tiles[i]
        lab = t['label']
        try:
            if obj is None:
                lab.configure(text='(no preview)')
                return
            if isinstance(obj, str):             # no Pillow: Tk's own PNG reader, shrunk by subsampling
                ph = tk.PhotoImage(file=obj)
                k = max(1, max(ph.width(), ph.height()) // THUMB)
                ph = ph.subsample(k, k)
            else:
                ph = ImageTk.PhotoImage(obj)
            t['photo'] = ph
            lab.configure(image=ph, text='', width=ph.width(), height=ph.height())
        except tk.TclError:
            lab.configure(text='(no preview)')
