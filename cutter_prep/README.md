# Cutter Prep

Flattens a design so every path matches what is visible on screen, ready for a cutting machine such as a Cricut or xTool.

SVGs often draw with a mix of fills and strokes. Cutters only follow path geometry, so a thick stroke, or a shape with both a fill and a stroke, imports wrong. Cutter Prep converts strokes to outlines, removes parts hidden behind other objects, merges what is left, and outputs clean cut paths.

Requires Inkscape 1.4 or newer. Tested on macOS with Inkscape 1.4.

## Use

Extensions > Cutter Prep > Flatten for Cutter. The whole run is one undo step.

The output replaces the document's contents, so save it under a new name (Shift+Cmd+S / Shift+Ctrl+S) to keep the original.

## Options

| Option | Default | Notes |
| --- | --- | --- |
| Color handling | Per-color layers | Or one silhouette, or one silhouette with white areas as holes |
| Drop white in per-color mode | On | White = every RGB channel ≥ the threshold |
| White threshold | 240 | |
| Output style | Hairline stroke | Or solid fill |
| Hairline width | 0.1 mm | |
| Color tolerance | 40 per channel | Snaps near-identical colors together, e.g. #231f20 into #000000 |
| Min sliver width | 0.25 mm | Removes thin, elongated leftovers; round dots are kept |
| Min fragment area | 0.02 mm² | |
| Show report | On | Counts of what was removed and merged, plus warnings |

In silhouette mode, a white background that spans at least 90% of the page is dropped; white details inside the design stay filled.

## How it works

1. **Pre-clean:** unlock everything, unlink clones, remove hidden objects, raster images and objects with no fill and no stroke.
2. **Flatten:** Inkscape's Object to Path, Ungroup and Path > Flatten, run headless. Flatten turns strokes into outlines and removes hidden areas.
3. **Clean:** remove specks, resolve colors (gradients use their first stop), snap near-identical colors, drop white where the mode calls for it.
4. **Merge:** Path > Union per color, or on everything in silhouette modes, then remove tiny fragments and slivers from the merged paths.
5. **Output:** bake transforms, put paths in `Cut #rrggbb` layers (or one `Cut` layer), apply the output style.

## Known limits

- Clips, masks, markers and transparency are reported as warnings, not applied; semi-transparent objects are treated as opaque.
- Gradients become their first stop color.
- Groups nested deeper than 8 levels may survive ungrouping.
- Inkscape's Path > Flatten can occasionally nick a small dot (about 0.2 mm seen once).

## Samples

`samples/` holds two small test files: a star drawn as fill plus stroke with a near-black offset copy, and a black disc with a white detail on a white page.
