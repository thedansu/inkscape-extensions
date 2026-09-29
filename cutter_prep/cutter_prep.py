#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""
Cutter Prep: flatten artwork so the paths match what is visible on screen,
ready for Cricut Design Space or xTool Creative Space.

Pipeline
  1. Pre-clean (inkex): unlock, unlink clones, drop hidden/invisible/empty
     objects and raster images, collect warnings.
  2. Flatten (Inkscape headless): object-to-path, ungroup, path-flatten
     (1.4+ converts strokes to outlines and removes occluded areas).
  3. Clean (inkex): remove tiny fragments, resolve region colors.
  4. Merge (Inkscape headless): union same-color regions, or everything;
     then (inkex) remove tiny fragments and slivers from the merged paths.
  5. Output (inkex): bake transforms, sort into layers, apply cut styling.
"""
import math
import os
import shutil
import tempfile
from collections import OrderedDict, defaultdict

import inkex
from inkex import command
from inkex.paths import CubicSuperPath
from lxml import etree

NON_RENDER = {
    "defs", "clipPath", "mask", "marker", "symbol", "pattern",
    "linearGradient", "radialGradient", "metadata", "namedview",
    "style", "title", "desc", "script", "filter",
}
DRAWABLE = (
    inkex.PathElement, inkex.Rectangle, inkex.Circle, inkex.Ellipse,
    inkex.Polygon, inkex.Polyline, inkex.Line, inkex.TextElement,
)
REQUIRED_ACTIONS = ("path-flatten", "path-union", "select-by-id", "selection-ungroup")
UNGROUP_PASSES = 8
BEZIER_SAMPLES = 8
SLIVER_MAX_ROUNDNESS = 0.25
BACKGROUND_SPAN = 0.9


def localname(el):
    try:
        return etree.QName(el).localname
    except ValueError:
        return ""


def in_nonrender(el):
    node = el
    while node is not None:
        if localname(node) in NON_RENDER:
            return True
        node = node.getparent()
    return False


def is_hidden(el):
    node = el
    while node is not None and isinstance(node, inkex.BaseElement):
        style = node.style
        if style.get("display") == "none" or node.get("display") == "none":
            return True
        vis = style.get("visibility") or node.get("visibility")
        if vis in ("hidden", "collapse"):
            return True
        node = node.getparent()
    return False


def cumulative_opacity(el):
    total = 1.0
    node = el
    while node is not None and isinstance(node, inkex.BaseElement):
        try:
            total *= float(node.style.get("opacity", node.get("opacity", "1")))
        except ValueError:
            pass
        node = node.getparent()
    return total


def _float(val, default):
    try:
        return float(str(val).rstrip("px"))
    except (TypeError, ValueError):
        return default


# ---------- geometry helpers (area filtering) ----------

def _bezier_points(p0, c1, c2, p3, n=BEZIER_SAMPLES):
    pts = []
    for i in range(1, n + 1):
        t = i / n
        mt = 1 - t
        x = mt**3 * p0[0] + 3 * mt**2 * t * c1[0] + 3 * mt * t**2 * c2[0] + t**3 * p3[0]
        y = mt**3 * p0[1] + 3 * mt**2 * t * c1[1] + 3 * mt * t**2 * c2[1] + t**3 * p3[1]
        pts.append((x, y))
    return pts


def subpath_polygon(sub):
    if not sub:
        return []
    poly = [tuple(sub[0][1])]
    for a, b in zip(sub, sub[1:]):
        poly.extend(_bezier_points(a[1], a[2], b[0], b[1]))
    return poly


def signed_area(poly):
    area = 0.0
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        area += x1 * y2 - x2 * y1
    return area / 2.0


def perimeter(poly):
    return sum(
        ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1])
    )


def is_sliver(area, perim, min_width):
    """Thin (average width 2*area/perimeter below min_width) and elongated.
    The roundness test keeps small round dots, whose 2A/P is only their radius."""
    if min_width <= 0 or perim <= 0:
        return False
    roundness = 4 * math.pi * area / perim ** 2  # 1 for a circle, ~0 for a strip
    return 2 * area / perim < min_width and roundness < SLIVER_MAX_ROUNDNESS


def point_in_poly(pt, poly):
    x, y = pt
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def net_area(polys):
    """Even-odd style net area: nested subpaths alternate between add and subtract."""
    total = 0.0
    for i, poly in enumerate(polys):
        if len(poly) < 3:
            continue
        depth = sum(
            1 for j, other in enumerate(polys)
            if j != i and len(other) >= 3 and point_in_poly(poly[0], other)
        )
        area = abs(signed_area(poly))
        total += area if depth % 2 == 0 else -area
    return abs(total)


class CutterPrep(inkex.EffectExtension):

    def add_arguments(self, pars):
        pars.add_argument("--tab", default="options")
        pars.add_argument("--color_mode", default="per_color")
        pars.add_argument("--drop_white", type=inkex.Boolean, default=True)
        pars.add_argument("--white_threshold", type=int, default=240)
        pars.add_argument("--output_style", default="stroke")
        pars.add_argument("--hairline_mm", type=float, default=0.1)
        pars.add_argument("--color_tolerance", type=int, default=40)
        pars.add_argument("--min_width", type=float, default=0.25)
        pars.add_argument("--min_area", type=float, default=0.02)
        pars.add_argument("--show_report", type=inkex.Boolean, default=True)

    # ---------- main ----------

    def effect(self):
        self.report = OrderedDict()
        self.warnings = []

        self.check_inkscape()
        self.preclean()
        self.flatten()
        colors = self.clean_and_color()
        self.merge(colors)
        self.clean_merged()
        self.finalize()

        if self.options.show_report:
            self.print_report()

    # ---------- step 0: environment ----------

    def check_inkscape(self):
        try:
            out = command.inkscape("--action-list")
        except Exception as err:  # noqa: BLE001
            raise inkex.AbortExtension(f"Could not run Inkscape headless:\n{err}")
        missing = [a for a in REQUIRED_ACTIONS if a not in out]
        if missing:
            raise inkex.AbortExtension(
                "This Inkscape is missing required actions: "
                + ", ".join(missing)
                + "\nInkscape 1.4 or newer is required."
            )

    def run_actions(self, actions):
        """Save the document, run Inkscape actions on it headless, reload."""
        tmpdir = tempfile.mkdtemp(prefix="cutterprep_")
        path = os.path.join(tmpdir, "work.svg")
        docname_attr = inkex.addNS("docname", "sodipodi")
        docname = self.svg.get(docname_attr)
        try:
            self.document.write(path)
            command.inkscape(path, "--export-overwrite", actions=actions)
            self.document = inkex.load_svg(path)
            self.svg = self.document.getroot()
            # the headless save renames the document to the temp file
            if docname is None:
                self.svg.attrib.pop(docname_attr, None)
            else:
                self.svg.set(docname_attr, docname)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def renderable(self, types=None):
        types = types or (inkex.ShapeElement,)
        return [
            el for el in self.svg.descendants()
            if isinstance(el, types) and el is not self.svg and not in_nonrender(el)
        ]

    def bump(self, key, n=1):
        self.report[key] = self.report.get(key, 0) + n

    # ---------- step 1: pre-clean ----------

    def preclean(self):
        insensitive = inkex.addNS("insensitive", "sodipodi")
        for el in self.svg.iter(tag=etree.Element):
            if insensitive in el.attrib:
                del el.attrib[insensitive]

        for _ in range(5):
            uses = [u for u in self.svg.xpath("//svg:use") if not in_nonrender(u)]
            if not uses:
                break
            for use in uses:
                try:
                    use.unlink()
                    self.bump("Clones unlinked")
                except Exception:  # noqa: BLE001
                    use.delete()
                    self.warnings.append(f"Clone {use.get_id()} had no valid source; removed.")

        for el in self.renderable():
            if el.getparent() is None:
                continue  # already removed with an ancestor
            if isinstance(el, inkex.Layer):
                if is_hidden(el):
                    el.delete()
                    self.bump("Hidden layers removed")
                continue
            if isinstance(el, inkex.Image):
                el.delete()
                self.bump("Raster images removed")
                continue
            if is_hidden(el) or cumulative_opacity(el) <= 0.001:
                el.delete()
                self.bump("Hidden/invisible objects removed")
                continue

            if isinstance(el, DRAWABLE):
                style = el.specified_style()
                fill = style.get("fill", "#000000")
                stroke = style.get("stroke", "none")
                sw = _float(style.get("stroke-width", "1"), 1.0)
                fill_off = fill in ("none", "transparent") or _float(style.get("fill-opacity", "1"), 1) <= 0.001
                stroke_off = (
                    stroke in ("none", "transparent") or sw <= 0
                    or _float(style.get("stroke-opacity", "1"), 1) <= 0.001
                )
                if fill_off and stroke_off:
                    el.delete()
                    self.bump("Empty objects removed (no fill, no stroke)")
                    continue
                if any(style.get(m) not in (None, "none") for m in ("marker-start", "marker-mid", "marker-end")):
                    self.warnings.append(f"{el.get_id()}: has markers (arrowheads); check the result.")
                if cumulative_opacity(el) < 0.999 or _float(style.get("fill-opacity", "1"), 1) < 0.999:
                    self.warnings.append(f"{el.get_id()}: semi-transparent; treated as opaque.")

            if el.get("clip-path") or el.style.get("clip-path") or el.get("mask") or el.style.get("mask"):
                self.warnings.append(f"{el.get_id()}: uses a clip or mask; verify the edges after flattening.")

        for el in self.renderable(DRAWABLE):
            el.get_id()  # ensure every drawable has an id

    # ---------- step 2: flatten ----------

    def flatten(self):
        ungroup = "select-all:all;selection-ungroup;" * UNGROUP_PASSES
        actions = (
            "select-all:all;object-to-path;"
            + ungroup
            + "select-all:all;object-to-path;"
            + "select-all:all;path-flatten;"
        )
        self.run_actions(actions)
        leftover = [g for g in self.renderable((inkex.Group,)) if not isinstance(g, inkex.Layer)]
        if leftover:
            self.warnings.append(f"{len(leftover)} group(s) survived ungrouping (deep nesting?).")

    # ---------- step 3: clean + colors ----------

    def resolve_paint(self, raw, count=True):
        if not raw or raw in ("none", "transparent"):
            return None
        if raw.startswith("url("):
            ref = raw[raw.find("#") + 1: raw.find(")")]
            grad = self.svg.getElementById(ref)
            seen = set()
            while grad is not None and ref not in seen:
                seen.add(ref)
                stops = [c for c in grad if localname(c) == "stop"]
                if stops:
                    stop = stops[0]
                    col = stop.style.get("stop-color") or stop.get("stop-color") or "#000000"
                    if count:
                        self.bump("Gradients reduced to first stop color")
                    return self.resolve_paint(col, count)
                href = grad.get("xlink:href") or grad.get("href")
                if not href:
                    break
                ref = href.lstrip("#")
                grad = self.svg.getElementById(ref)
            return "#000000"
        try:
            return str(inkex.Color(raw).to_rgb())
        except Exception:  # noqa: BLE001
            return "#000000"

    def is_white(self, hexcol):
        rgb = inkex.Color(hexcol).to_rgb()
        t = self.options.white_threshold
        return all(c >= t for c in list(rgb)[:3])

    def is_background(self, el, page):
        """A region spanning at least 90% of the page in both directions."""
        try:
            box = el.path.transform(el.composed_transform()).bounding_box()
        except Exception:  # noqa: BLE001
            return False
        return (
            box is not None
            and box.width >= BACKGROUND_SPAN * page.width
            and box.height >= BACKGROUND_SPAN * page.height
        )

    def clean_and_color(self):
        uu_per_mm = self.svg.unittouu("1mm")
        min_area_uu = self.options.min_area * uu_per_mm * uu_per_mm
        colors = defaultdict(list)
        self.color_reps = []
        mode = self.options.color_mode
        drop_white = mode == "white_holes" or (mode == "per_color" and self.options.drop_white)
        page = self.svg.get_page_bbox()

        for el in self.renderable((inkex.PathElement,)):
            if el.getparent() is None:
                continue
            style = el.specified_style()
            color = self.resolve_paint(style.get("fill", "#000000"))
            if color is None:
                color = self.resolve_paint(style.get("stroke"))
                if color is None:
                    el.delete()
                    continue
                self.warnings.append(f"{el.get_id()}: stroke was not converted to an outline.")

            if drop_white and self.is_white(color):
                el.delete()
                self.bump("White regions removed")
                continue
            if mode == "silhouette" and self.is_white(color) and self.is_background(el, page):
                el.delete()
                self.bump("White background removed")
                continue

            # Only specks go before the merge (path-union fails on degenerate
            # input). Sliver filtering waits until after it: a thin piece, such as
            # the ring a stroke leaves around its own fill, is only a sliver if it
            # stays thin once unioned with its neighbours.
            if self.filter_fragments(el, min_area_uu, 0) is None:
                continue

            rep = self.cluster_color(color)
            if rep != color:
                self.bump("Near-duplicate colors merged")
            el.style["fill"] = rep
            if "fill" in el.attrib:
                del el.attrib["fill"]
            colors[rep].append(el.get_id())
        return colors

    def cluster_color(self, color):
        """Snap a color to an earlier one if every channel is within tolerance."""
        tol = self.options.color_tolerance
        rgb = list(inkex.Color(color).to_rgb())[:3]
        for rep, rep_rgb in self.color_reps:
            if all(abs(a - b) <= tol for a, b in zip(rgb, rep_rgb)):
                return rep
        self.color_reps.append((color, rgb))
        return color

    def filter_fragments(self, el, min_area_uu, min_width_uu):
        """Drop tiny and sliver subpaths, then the whole path if what is left is
        tiny or a sliver. Returns the remaining polygons (doc coords), or None if deleted."""
        try:
            local = el.path.to_superpath()
            doc = el.path.transform(el.composed_transform()).to_superpath()
        except Exception:  # noqa: BLE001
            return []
        polys = [subpath_polygon(s) for s in doc]
        keep = []
        tiny = slivers = 0
        for i, p in enumerate(polys):
            if len(p) < 3:
                continue
            area = abs(signed_area(p))
            if area < min_area_uu:
                tiny += 1
            elif is_sliver(area, perimeter(p), min_width_uu):
                slivers += 1
            else:
                keep.append(i)
        if not keep:
            el.delete()
            self.bump("Fragments removed")
            return None
        if len(keep) < len(polys):
            el.path = CubicSuperPath([local[i] for i in keep]).to_path()
            if tiny:
                self.bump("Tiny sub-shapes removed", tiny)
            if slivers:
                self.bump("Sliver sub-shapes removed", slivers)
            polys = [polys[i] for i in keep]
        area = net_area(polys)
        if area < min_area_uu:
            el.delete()
            self.bump("Fragments removed")
            return None
        if is_sliver(area, sum(perimeter(p) for p in polys), min_width_uu):
            el.delete()
            self.bump("Slivers removed")
            return None
        return polys

    def clean_merged(self):
        """Remove tiny fragments and slivers from the merged paths, including the
        seam slivers and specks union leaves where edges nearly coincide."""
        uu_per_mm = self.svg.unittouu("1mm")
        min_area_uu = self.options.min_area * uu_per_mm * uu_per_mm
        min_width_uu = self.options.min_width * uu_per_mm
        for el in self.renderable((inkex.PathElement,)):
            if el.getparent() is not None:
                self.filter_fragments(el, min_area_uu, min_width_uu)

    # ---------- step 4: merge ----------

    def merge(self, colors):
        mode = self.options.color_mode
        if mode == "per_color":
            steps = [
                "select-clear;select-by-id:" + ",".join(ids) + ";path-union;"
                for ids in colors.values() if len(ids) > 1
            ]
            if steps:
                self.run_actions("".join(steps))
        else:
            if sum(len(v) for v in colors.values()) > 1:
                self.run_actions("select-all:all;path-union;")

    # ---------- step 5: output ----------

    def finalize(self):
        mode = self.options.color_mode
        hair = self.svg.unittouu(f"{self.options.hairline_mm}mm")
        paths = [p for p in self.renderable((inkex.PathElement,)) if p.getparent() is not None]

        groups = OrderedDict()
        for el in paths:
            style = el.specified_style()
            color = (
                self.resolve_paint(style.get("fill"), False)
                or self.resolve_paint(style.get("stroke"), False)
                or "#000000"
            )
            fill_rule = style.get("fill-rule", "nonzero")
            el.path = el.path.transform(el.composed_transform())
            el.transform = None
            key = color if mode == "per_color" else "#000000"
            groups.setdefault(key, []).append((el, fill_rule))

        # lift paths to the root, then strip the old structure
        # (now-empty layers/groups, anything that failed to convert)
        keep = set()
        for items in groups.values():
            for el, _ in items:
                self.svg.append(el)
                keep.add(el)
        stray = 0
        for child in list(self.svg):
            if child in keep or not isinstance(child, inkex.ShapeElement) or in_nonrender(child):
                continue
            stray += sum(1 for d in child.descendants() if isinstance(d, DRAWABLE))
            child.delete()
        if stray:
            self.warnings.append(f"{stray} non-path object(s) could not be converted and were removed.")

        for color, items in groups.items():
            name = f"Cut {color}" if mode == "per_color" else "Cut"
            layer = self.svg.add(inkex.Layer.new(name))
            for el, fill_rule in items:
                for attr in ("fill", "stroke", "stroke-width", "class", "opacity", "clip-path", "mask"):
                    if attr in el.attrib:
                        del el.attrib[attr]
                if self.options.output_style == "stroke":
                    el.style = inkex.Style({
                        "fill": "none",
                        "stroke": color,
                        "stroke-width": f"{hair:.4f}",
                        "stroke-linejoin": "miter",
                        "stroke-linecap": "butt",
                    })
                else:
                    el.style = inkex.Style({"fill": color, "fill-rule": fill_rule, "stroke": "none"})
                layer.append(el)
            self.bump(f"Paths in '{name}'", len(items))

    def print_report(self):
        lines = ["Cutter Prep finished.", ""]
        lines += [f"  {k}: {v}" for k, v in self.report.items()]
        if self.warnings:
            lines += ["", f"Warnings ({len(self.warnings)}):"]
            lines += [f"  • {w}" for w in self.warnings[:40]]
            if len(self.warnings) > 40:
                lines.append(f"  … and {len(self.warnings) - 40} more")
        self.msg("\n".join(lines))


if __name__ == "__main__":
    CutterPrep().run()
