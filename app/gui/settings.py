"""Settings remembered between runs: a small JSON file, %APPDATA%\\StroopMaps\\settings.json (the environment variable
STROOPMAPS_SETTINGS names another file; --settings does too). Unknown keys are kept, a damaged file is ignored."""
import json
import os

DEFAULTS = {
    'input': '',               # the last ROM or savestate
    'input_dir': '',           # where the file dialog opens
    'kinds': 'collision',      # collision | textured | both
    'scale_mode': 'auto',      # auto | custom
    'scale': '4',
    'bands_mode': 'none',      # none | auto | custom (none: one top view per area, the default since 2026-10-23)
    'bands': '',
    'light': 'stroop',         # stroop | rom | relief
    'objects': 'static',     # geodecode.OBJECT_MODES key (old settings: True / False)
    'show_unchanged': False,   # list a hack's levels that are identical to vanilla
    'object_collision': False, # collision maps include collision objects (ROM input)
    'out_dir': '',
    'stroop_dir': '',
    'blender': '',
    'tab': 'maps',
    'geometry': '',
    'settings_version': 2,
    'tags': {},                # hack name -> the short name last used before its map names
}


def default_path():
    """The settings file: STROOPMAPS_SETTINGS, else %APPDATA%/StroopMaps/settings.json."""
    p = os.environ.get('STROOPMAPS_SETTINGS')
    if p:
        return p
    base = os.environ.get('APPDATA') or os.path.expanduser('~')
    return os.path.join(base, 'StroopMaps', 'settings.json')


class Settings(dict):
    """The remembered settings: DEFAULTS, then whatever the file holds."""
    def __init__(self, path=None):
        super().__init__(DEFAULTS)
        self['tags'] = {}                                  # not DEFAULTS' own dict
        self.path = path or default_path()
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                if data.get('settings_version', 1) < 2 and data.get('bands_mode') == 'auto':
                    data['bands_mode'] = 'none'           # files before v2 saved the old default, not a choice
                self.update(data)
                self['settings_version'] = DEFAULTS['settings_version']
        except (OSError, ValueError):
            pass

    def save(self):
        """Write the file (through a temporary file); False when it cannot be written."""
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(dict(self), f, indent=1)
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False

    @property
    def folder(self):
        """The folder the settings file is in."""
        return os.path.dirname(self.path)
