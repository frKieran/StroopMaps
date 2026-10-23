# StroopMaps

This program generates top-down map renders for Super Mario 64-based ROMs, and sets them up in your [STROOP](https://github.com/FramePerfection/STROOP) instance.



## Simple Overview

By default, STROOP only ships vanilla levels. This program reads the collision data from a ROM or savestate and can generate the following:

- collision map - the map's geometry rendered without textures

- textured map - uses the in-game textures

- floor bands - can be derived or set manually; allows for seeing a "vertical slice" of the current level.

You can then patch your STROOP so it can load romhack areas automatically, and import the level maps into your configuration.



## Prerequisites

- Windows 11

- Python 3.14

- Blender 5.2

- FramePerfection's [STROOP](https://github.com/FramePerfection/STROOP) (latest release; 0.9.2 as of writing)

- Legal dumped ROM of vanilla SM64



## Installation

```
git clone https://github.com/frKieran/StroopMaps
cd StroopMaps
python -m pip install -r requirements.txt
```



Your STROOP folder and Blender installation can be selected directly within the GUI window, or set with the `STROOPMAPS_STROOP` and `STROOPMAPS_BLENDER` environment variables.


## Using the GUI

Run the application:

```
python app/main.py
```

Alternatively, if you built the standalone executable (`python app/build_dist.py`), run `StroopMaps.exe`.



Then:

1. **Select Source:** Choose either a ROM file or an active savestate (`.st`) of the level.

2. **Configure Options:**

   - **Kinds:** Choose between collision maps, textured maps, or both.

   - **Object Modes:** Control what objects are rendered into textured maps (`static`, `scenery`, `grabbable`, `collision`, `all`, or `none`).

   - **Lighting:** Select between STROOP lighting, ROM lighting, or add relief shading.

   - **Floor Bands:** Toggle automatic vertical slicing for multi-floor areas if desired.

3. **Export & Install:** The GUI handles exporting the map images, building the XML map associations, installing them into your STROOP directory, and configuring the "Recommended" patch so STROOP automatically picks up romhack maps

   

## ROM folders (when using savestates)

A savestate only records the MD5 of the ROM it was made on. StroopMaps looks for that ROM (and for the vanilla US ROM
it compares hacks with) in the folders you have opened ROMs or savestates from, and in the folders named by the
`STROOPMAPS_ROMS` environment variable (separated by `;`). Other settings can come from the environment too:
`STROOPMAPS_BLENDER` (blender.exe) and `STROOPMAPS_STROOP` (the STROOP folder).



## Limitations

- Hacks built from decomp move the game's code and modify it too heavily to be read.

- Objects not placed yet won't render when selected from ROM.

- F3DEX display lists are experimental and weren't tested on a real hack.



## License and credits

Relevant: [LICENSE](LICENSE), [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

- **STROOP** (FramePerfection's fork): the map format, the frame conventions and the code the patch changes.
- **STROOP Next** by hugou74130 (STROOP's `feature/stroop-next` branch): its romhack support showed the approach
  taken here for reading a hack's level table and scripts from the ROM and for decoding F3DEX / F3DEX2 display lists.
- **The SM64 decompilation** (n64decomp/sm64): the collision and level formats, the action names, the geo layout and
  display-list commands, all read from its source.
- **Blender** is used to render the textured maps.


