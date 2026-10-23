# STROOP patch: "Recommended" picks a romhack's maps

STROOP's Map tab chooses its map and background with `MapAssociations.GetBestMap()`. When the word at `0x80402000` is
nonzero (every ROM Manager hack) it returns a blank map with a blue background, so a hack's maps have to be picked by
hand. This patch makes "Recommended" pick the maps of the running hack.

- **Base:** FramePerfection's STROOP 0.9.2 (the `v0.9.2` tag). The patch is `hack-maps-0.9.2.patch` (4 files, +89
  lines): `MapAssociations.cs` (the hack lookup), `MapLayout.cs` (two fields), `XmlConfigParser.cs` (reading
  `<MapHack>` and the map tags) and `MapTab.cs` (the map list opens as wide as its longest entry: romhack map names
  are longer than the 183-pixel box).
- **Build:** `python stroop_patch/build.py` clones STROOP (or uses `--src`, a clone you have), checks out the base
  (`--base`, default `v0.9.2`) in a temporary worktree, applies the patch and runs `dotnet build`. It writes
  `bin/STROOP.dll`, `bin/STROOP.pdb` and `bin/patched.json`. Needs git and the .NET SDK. Only `STROOP.dll` differs from
  the stock build; a release of StroopMaps carries a built `STROOP.dll` for those without the SDK.
- **Switch:** `stroop_install.set_patch(stroop_dir, True/False)` (the window's "Recommended" switch) swaps `STROOP.dll`
  and `STROOP.pdb`; both builds are kept in `<STROOP>\STROOP Archive\stroopmaps-patch\{stock,patched}`.
  `patch_status(stroop_dir)` reads the dll (`'patched'` = it contains the text `stroopmaps-hack-patch`). The swap
  refuses a STROOP whose dll lacks the version text `Refactor 0.9.2`: the patch is for that version only. STROOP must be
  closed (a running STROOP locks its dll). On a STROOP with an older patched build, `set_patch(.., True)` puts the new
  one in.

## What it reads (Config\MapAssociations.xml)
Written by `stroop_install.install`; stock STROOP ignores all of it (its parser reads the named elements and
attributes only).

```xml
<MapHack name="SM64 Island" prefer="textured" tag="Island">  <!-- one per hack -->
  <Signature address="0x80248328" value="0x2B21FFF9" />      <!-- optional mask="0xFFFFFFFF" -->
  ...                                                        <!-- ALL words must match for the hack to be running -->
</MapHack>
<Map name="Island Castle" id="IT061A" level="6" area="1" hack="SM64 Island" kind="textured"> ... </Map>
<Config> ... <HackOverride>auto | off | SM64 Island</HackOverride> </Config>
```

- **Detection:** `GetActiveHack()` returns the first `<MapHack>` whose signature words all match RAM. The signature
  (made by `exporter.py`) is eight words of the game code (`0x80246000..0x802A6000`) that differ from the vanilla US
  ROM: a hack's patches to the code, static while the game runs. A hack that changes no game code has no signature
  (the exporter says so in its log) and needs the override.
- **Override** (`<HackOverride>`, set with `stroop_install.set_hack_override`, read when STROOP starts): `auto` or
  absent = detect; `off` = never use a hack's maps (stock behaviour); a hack's name = always use that hack.
- **Choice:** with an active hack, `GetBestMap()` takes that hack's maps for the current level / area / loading point
  / mission layout whose `y` is at most Mario's Y, the highest `y` wins (as for stock maps), preferring the `prefer`
  kind when the area has it. If the hack has no map there, or no hack is active, it falls through to the stock code
  (a blank map for a ROM hack, the vanilla lookup otherwise). The map's own `BackgroundImage` is the recommended
  background.
- **Side effect:** the vanilla lookup skips maps tagged with a hack, so installed hack maps don't show up in vanilla
  games that share a level and area number (vanilla BoB and SM64 Island's Bob-omb Mountain are both 9/1). The
  dropdown still lists every map, and choosing a map by hand works as before.
- `tag` is the short name StroopMaps put before the hack's map names; the patch doesn't read it.
