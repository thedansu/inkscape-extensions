# Inkscape extensions

Extensions for [Inkscape](https://inkscape.org/) 1.4 and newer, one folder each.

| Extension | What it does |
| --- | --- |
| [Cutter Prep](cutter_prep/) | Flattens a design so its paths match what is visible on screen, ready for a cutting machine |

## Install

Inkscape loads extensions from subfolders of its user extensions folder, so you can clone this whole repo into it:

```sh
# macOS
git clone https://github.com/thedansu/inkscape-extensions.git \
  "$HOME/Library/Application Support/org.inkscape.Inkscape/config/inkscape/extensions/inkscape-extensions"

# Linux
git clone https://github.com/thedansu/inkscape-extensions.git ~/.config/inkscape/extensions/inkscape-extensions
```

On Windows the folder is `%APPDATA%\inkscape\extensions`. Inkscape > Settings > System > User extensions shows the exact path on any system.

Restart Inkscape after installing or updating. A malformed `.inx` file is skipped silently; `xmllint --noout <file>.inx` checks it.

## License

GPL-2.0-or-later, the same as Inkscape's `inkex` library the extensions use. See [LICENSE](LICENSE).
