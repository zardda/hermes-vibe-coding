#!/usr/bin/env python3
"""Extract the track drawing area from an IGV/Batik SVG snapshot.

The script removes the chromosome overview, left track-label panel, axis/value
text, and outer panel borders. It keeps the main track drawing groups and writes
a cropped SVG that is easier to reuse in figures.
"""

from __future__ import annotations

import argparse
import copy
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET


SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
TRANSLATE_RE = re.compile(
    r"translate\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)"
)


def tag_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def parse_translate(value: str | None) -> tuple[float, float] | None:
    if not value:
        return None
    match = TRANSLATE_RE.fullmatch(value.strip())
    if not match:
        return None
    return float(match.group(1)), float(match.group(2))


def fmt_num(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:g}"


def has_tag(element: ET.Element, name: str) -> bool:
    return any(tag_name(child) == name for child in element.iter())


def has_silver_style(element: ET.Element) -> bool:
    return element.attrib.get("fill") == "silver" or element.attrib.get("stroke") == "silver"


def rect_size(element: ET.Element) -> tuple[float, float] | None:
    if tag_name(element) != "rect":
        return None
    try:
        return float(element.attrib["width"]), float(element.attrib["height"])
    except (KeyError, ValueError):
        return None


def find_main_group(root: ET.Element) -> ET.Element:
    groups = [child for child in list(root) if tag_name(child) == "g"]
    if not groups:
        raise ValueError("No top-level <g> group found in the SVG.")
    return groups[0]


def find_track_origin(children: list[ET.Element], requested: str | None) -> tuple[float, float]:
    if requested:
        match = TRANSLATE_RE.fullmatch(requested.strip())
        if not match:
            raise ValueError("--track-transform must look like 'translate(170,130)'.")
        return float(match.group(1)), float(match.group(2))

    counts: dict[tuple[float, float], int] = {}
    for child in children:
        parsed = parse_translate(child.attrib.get("transform"))
        if parsed:
            counts[parsed] = counts.get(parsed, 0) + 1

    if not counts:
        raise ValueError("No translated groups found; pass --track-transform explicitly.")

    # In IGV snapshots, the data-heavy track panel is represented by many groups
    # sharing the same translate(x,y), while labels and overview panels have few.
    return max(counts.items(), key=lambda item: item[1])[0]


def infer_canvas_size(children: list[ET.Element], origin: tuple[float, float]) -> tuple[float, float]:
    ox, oy = origin
    candidates: list[tuple[float, float]] = []
    for child in children:
        if parse_translate(child.attrib.get("transform")) != origin:
            continue
        for element in child.iter():
            size = rect_size(element)
            if not size:
                continue
            width, height = size
            x = float(element.attrib.get("x", "0"))
            y = float(element.attrib.get("y", "0"))
            if width > 100 and height > 100:
                candidates.append((x + width, y + height))

    if candidates:
        return max(candidates, key=lambda size: size[0] * size[1])

    root_width = float(children[0].attrib.get("width", "0")) if children else 0
    root_height = float(children[0].attrib.get("height", "0")) if children else 0
    if root_width and root_height:
        return max(root_width - ox, 1), max(root_height - oy, 1)

    raise ValueError("Could not infer track canvas size; pass --width and --height.")


def clone_defs_without_offsets(defs: ET.Element | None) -> ET.Element | None:
    if defs is None:
        return None
    cloned = copy.deepcopy(defs)
    for element in cloned.iter():
        if tag_name(element) == "path":
            d = element.attrib.get("d")
            if d and d.startswith("M-"):
                # IGV may include a clip path expanded leftward to cover the
                # label column. It is not needed after cropping to the track area.
                element.attrib["d"] = re.sub(r"M-\d+(?:\.\d+)?", "M0", d, count=1)
                element.attrib["d"] = re.sub(r"L-\d+(?:\.\d+)?", "L0", element.attrib["d"])
    return cloned


def build_clean_svg(
    input_svg: Path,
    output_svg: Path,
    track_transform: str | None,
    width: float | None,
    height: float | None,
    keep_text: bool,
    keep_silver: bool,
) -> dict[str, int | str]:
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", XLINK_NS)

    tree = ET.parse(input_svg)
    old_root = tree.getroot()
    main_group = find_main_group(old_root)
    children = list(main_group)
    if children and tag_name(children[0]) == "defs":
        defs = children[0]
        drawable_children = children[1:]
    else:
        defs = None
        drawable_children = children

    origin = find_track_origin(drawable_children, track_transform)
    inferred_width, inferred_height = infer_canvas_size(drawable_children, origin)
    out_width = width if width is not None else inferred_width
    out_height = height if height is not None else inferred_height

    root_attrib = {
        "width": fmt_num(out_width),
        "height": fmt_num(out_height),
        "viewBox": f"0 0 {fmt_num(out_width)} {fmt_num(out_height)}",
    }
    for key in (
        "fill-opacity",
        "color-rendering",
        "color-interpolation",
        "text-rendering",
        "stroke",
        "stroke-linecap",
        "stroke-miterlimit",
        "shape-rendering",
        "stroke-opacity",
        "fill",
        "stroke-dasharray",
        "font-weight",
        "stroke-width",
        "font-family",
        "font-style",
        "stroke-linejoin",
        "font-size",
        "stroke-dashoffset",
        "image-rendering",
    ):
        if key in old_root.attrib:
            root_attrib[key] = old_root.attrib[key]

    new_root = ET.Element(old_root.tag, root_attrib)
    cloned_defs = clone_defs_without_offsets(defs)
    if cloned_defs is not None:
        new_root.append(cloned_defs)

    track_group = ET.SubElement(new_root, "g", {"id": "tracks-only"})
    kept = skipped_nontrack = removed_text = removed_silver = 0
    for child in drawable_children:
        if parse_translate(child.attrib.get("transform")) != origin:
            skipped_nontrack += 1
            continue
        if not keep_text and has_tag(child, "text"):
            removed_text += 1
            continue
        if not keep_silver and has_silver_style(child):
            removed_silver += 1
            continue

        item = copy.deepcopy(child)
        item.attrib.pop("transform", None)
        track_group.append(item)
        kept += 1

    ET.ElementTree(new_root).write(output_svg, encoding="utf-8", xml_declaration=True)
    return {
        "output": str(output_svg),
        "origin": f"translate({fmt_num(origin[0])},{fmt_num(origin[1])})",
        "width": fmt_num(out_width),
        "height": fmt_num(out_height),
        "kept_groups": kept,
        "skipped_nontrack_groups": skipped_nontrack,
        "removed_text_groups": removed_text,
        "removed_silver_groups": removed_silver,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crop an IGV/Batik SVG snapshot to the main track drawing area."
    )
    parser.add_argument("input_svg", help="Input IGV SVG snapshot.")
    parser.add_argument(
        "-o",
        "--output",
        help="Output SVG path. Defaults to INPUT_stem + '_tracks_only.svg'.",
    )
    parser.add_argument(
        "--track-transform",
        help="Track panel transform, e.g. 'translate(170,130)'. Auto-detected by default.",
    )
    parser.add_argument("--width", type=float, help="Override output canvas width.")
    parser.add_argument("--height", type=float, help="Override output canvas height.")
    parser.add_argument("--keep-text", action="store_true", help="Keep text inside the track panel.")
    parser.add_argument(
        "--keep-silver",
        action="store_true",
        help="Keep silver IGV border/separator groups inside the track panel.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    input_svg = Path(args.input_svg)
    output_svg = Path(args.output) if args.output else input_svg.with_name(f"{input_svg.stem}_tracks_only.svg")

    try:
        stats = build_clean_svg(
            input_svg=input_svg,
            output_svg=output_svg,
            track_transform=args.track_transform,
            width=args.width,
            height=args.height,
            keep_text=args.keep_text,
            keep_silver=args.keep_silver,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote: {stats['output']}")
    print(f"Track transform: {stats['origin']}")
    print(f"Canvas: {stats['width']} x {stats['height']}")
    print(f"Kept groups: {stats['kept_groups']}")
    print(f"Skipped non-track groups: {stats['skipped_nontrack_groups']}")
    print(f"Removed text groups: {stats['removed_text_groups']}")
    print(f"Removed silver groups: {stats['removed_silver_groups']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
