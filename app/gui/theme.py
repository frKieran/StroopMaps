"""The look: the navy palette, Segoe UI, the ttk 'clam' styles, pixel scaling for high-dpi screens, the tooltip and
card helpers."""
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

# --- palette -----------------------------------------------------------------------------------------------
NAVY = '#0b1a33'        # window
PANEL = '#13284a'       # cards
FIELD = '#0d1f3a'       # entries, the lanes' canvas
CELL = '#0f2243'        # table cells, empty lane
LINE = '#21406e'
EDGE = '#2a4c80'
BLUE = '#3d7fd6'
TEXT = '#e8f0ff'
SOFT = '#c9d8f0'
DIM = '#9fb6d9'
OK = '#6ee7a0'
BAD = '#ff8080'
WARN = '#ffd27a'
HOVER = '#7f95ba'
DISABLED_BG = '#1b3866'
DISABLED_FG = '#7f95ba'
TIP_BG = '#081326'


UI = 'Segoe UI'
MONO = 'Consolas'

# --- pixel scaling (draft C) ------------------------------------------------------------------------------------
_scale = [1.0]


def init_scale(root):
    """Once after Tk starts (and after SetProcessDpiAwareness): logical 96-dpi pixels -> real pixels."""
    _scale[0] = root.winfo_fpixels('1i') / 96.0


def px(n):
    """`n` pixels at 96 dpi, scaled to the screen."""
    return max(1, int(round(n * _scale[0])))


class Fonts:
    """Created after Tk starts; sizes in points follow the display scaling by themselves."""

    def __init__(self):
        mk = lambda size, weight='normal', family=UI: tkfont.Font(family=family, size=size, weight=weight)  # noqa: E731
        self.base = mk(9)
        self.bold = mk(9, 'bold')
        self.small = mk(8)
        self.small_bold = mk(8, 'bold')
        self.title = mk(10, 'bold')
        self.head = mk(15, 'bold')
        self.big = mk(28, 'bold')
        self.outcome = mk(20, 'bold')
        self.entry = mk(13, 'bold')
        self.mono = mk(9, family=MONO)
        self.mono_bold = mk(9, 'bold', family=MONO)


# --- ttk styles -------------------------------------------------------------------------------------------------
def apply_style(root, fonts):
    """The ttk styles and colours of the window."""
    root.configure(bg=NAVY)
    root.option_add('*TCombobox*Listbox.background', PANEL)
    root.option_add('*TCombobox*Listbox.foreground', TEXT)
    root.option_add('*TCombobox*Listbox.selectBackground', BLUE)
    s = ttk.Style(root)
    s.theme_use('clam')
    s.configure('.', background=NAVY, foreground=TEXT, fieldbackground=FIELD, bordercolor=EDGE, font=fonts.base,
                lightcolor=PANEL, darkcolor=PANEL, troughcolor=FIELD, focuscolor=BLUE, insertcolor=TEXT,
                selectbackground=BLUE, selectforeground='white')
    s.configure('TFrame', background=NAVY)
    s.configure('Card.TFrame', background=PANEL)
    s.configure('TLabel', background=NAVY, foreground=TEXT)
    s.configure('TButton', background=LINE, foreground=TEXT, borderwidth=0, padding=(px(10), px(4)),
                lightcolor=LINE, darkcolor=LINE, bordercolor=LINE)
    s.map('TButton', background=[('disabled', DISABLED_BG), ('pressed', EDGE), ('active', BLUE)],
          foreground=[('disabled', DISABLED_FG)], lightcolor=[('active', BLUE)], darkcolor=[('active', BLUE)])
    s.configure('Go.TButton', background=BLUE, foreground='white', font=fonts.title, padding=(px(18), px(5)),
                lightcolor=BLUE, darkcolor=BLUE, bordercolor=BLUE)
    s.map('Go.TButton', background=[('disabled', DISABLED_BG), ('active', '#5a95e0')],
          foreground=[('disabled', DISABLED_FG)], lightcolor=[('active', '#5a95e0')], darkcolor=[('active', '#5a95e0')])
    s.configure('Stop.TButton', background='#6b2433', foreground=TEXT, font=fonts.title, padding=(px(18), px(5)),
                lightcolor='#6b2433', darkcolor='#6b2433', bordercolor='#6b2433')
    s.map('Stop.TButton', background=[('disabled', DISABLED_BG), ('active', '#93303f')],
          foreground=[('disabled', DISABLED_FG)])
    s.configure('Tab.TButton', background=NAVY, foreground=DIM, font=fonts.title, padding=(px(16), px(6)),
                lightcolor=NAVY, darkcolor=NAVY, bordercolor=NAVY)
    s.map('Tab.TButton', background=[('active', PANEL)], foreground=[('active', TEXT)])
    s.configure('TabOn.TButton', background=PANEL, foreground=TEXT, font=fonts.title, padding=(px(16), px(6)),
                lightcolor=BLUE, darkcolor=PANEL, bordercolor=PANEL)
    s.map('TabOn.TButton', background=[('active', PANEL)])
    for name in ('TEntry', 'TSpinbox', 'TCombobox'):
        s.configure(name, fieldbackground=FIELD, foreground=TEXT, insertcolor=TEXT, bordercolor=EDGE,
                    lightcolor=FIELD, darkcolor=FIELD, background=LINE, arrowcolor=TEXT, padding=(px(5), px(3)))
        s.map(name, fieldbackground=[('readonly', FIELD), ('disabled', PANEL)], foreground=[('disabled', DIM)],
              bordercolor=[('focus', BLUE)], lightcolor=[('focus', BLUE)])
    s.configure('Frame.TEntry', font=fonts.entry, padding=(px(6), px(2)))
    s.configure('Bad.Frame.TEntry', fieldbackground='#3a1a22', foreground=BAD)
    s.configure('Bad.TEntry', fieldbackground='#3a1a22', foreground=BAD)
    s.configure('Treeview', background=CELL, fieldbackground=CELL, foreground=TEXT, rowheight=px(23),
                bordercolor=LINE, lightcolor=CELL, darkcolor=CELL, font=fonts.base)
    s.configure('Treeview.Heading', background=PANEL, foreground=DIM, font=fonts.small_bold, relief='flat',
                padding=(px(4), px(3)), bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL)
    s.map('Treeview.Heading', background=[('active', LINE)])
    s.map('Treeview', background=[('selected', BLUE)], foreground=[('selected', 'white')])
    s.configure('Vertical.TScrollbar', background=LINE, troughcolor=FIELD, bordercolor=FIELD, arrowcolor=TEXT,
                lightcolor=LINE, darkcolor=LINE, arrowsize=px(12))
    s.map('Vertical.TScrollbar', background=[('active', EDGE)])
    s.configure('Horizontal.TScrollbar', background=LINE, troughcolor=FIELD, bordercolor=FIELD, arrowcolor=TEXT,
                lightcolor=LINE, darkcolor=LINE, arrowsize=px(12))
    s.map('Horizontal.TScrollbar', background=[('active', EDGE)])
    s.configure('Horizontal.TProgressbar', background=BLUE, troughcolor=FIELD, bordercolor=FIELD, lightcolor=BLUE,
                darkcolor=BLUE)
    s.configure('TCheckbutton', background=PANEL, foreground=SOFT, indicatorbackground=FIELD,
                indicatorforeground=TEXT, focuscolor=PANEL)
    s.map('TCheckbutton', background=[('active', PANEL)], indicatorbackground=[('selected', BLUE)])
    s.configure('TRadiobutton', background=PANEL, foreground=SOFT, indicatorbackground=FIELD,
                indicatorforeground=TEXT, focuscolor=PANEL)
    s.map('TRadiobutton', background=[('active', PANEL)], indicatorbackground=[('selected', BLUE)])
    return s


# --- small widgets ------------------------------------------------------------------------------------------------
class Tip:
    """A tooltip; text may be a callable returning the text (or None for no tip) when the pointer arrives."""

    def __init__(self, widget, text):
        self.widget, self.text, self.top = widget, text, None
        widget.bind('<Enter>', self.show, add='+')
        widget.bind('<Leave>', self.hide, add='+')
        widget.bind('<ButtonPress>', self.hide, add='+')

    def show(self, _e=None):
        """Show the tooltip by the pointer."""
        text = self.text() if callable(self.text) else self.text
        if self.top or not text:
            return
        x = self.widget.winfo_rootx() + px(12)
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + px(4)
        self.top = tk.Toplevel(self.widget)
        self.top.wm_overrideredirect(True)
        self.top.geometry(f'+{x}+{y}')
        tk.Label(self.top, text=text, bg=TIP_BG, fg=TEXT, font=(UI, 9), justify='left', wraplength=px(460),
                 padx=px(8), pady=px(5), bd=1, relief='solid').pack()

    def hide(self, _e=None):
        """Hide the tooltip."""
        if self.top:
            self.top.destroy()
            self.top = None


def card(parent, **kw):
    """A panel with A's card colour and a hairline edge."""
    return tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1, **kw)


def label(parent, text='', font=None, fg=TEXT, bg=PANEL, **kw):
    """A tk.Label in the theme colours."""
    return tk.Label(parent, text=text, font=font, fg=fg, bg=bg, **kw)
