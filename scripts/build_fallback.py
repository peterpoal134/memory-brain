"""Generate a deterministic CC BY-SA cortical SVG; stdlib only, no model/network.

Input: the shipped Anderson Winkler MRI-derived lh.bin/rh.bin surfaces.
Output: an original Memory Brain contributors projection and decorative stage.
The resulting image is CC BY-SA 3.0, separately from this AGPL source code.
"""
from pathlib import Path
import hashlib
import json
import math
import struct
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "xiaojiu_brain" / "static"
SVG = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG)


def element(parent, tag, **attributes):
    return ET.SubElement(parent, "{" + SVG + "}" + tag,
                         {key.replace("_", "-"): str(value) for key, value in attributes.items()})


def read_surface(filename):
    data = (STATIC / "cortex" / filename).read_bytes()
    vertices, triangles = struct.unpack_from("<II", data)
    if len(data) != 8 + vertices * 12 + triangles * 12:
        raise ValueError("Invalid cortical surface length: " + filename)
    positions = list(struct.iter_unpack("<fff", data[8:8 + vertices * 12]))
    faces = list(struct.iter_unpack("<III", data[8 + vertices * 12:]))
    projected = []
    for x, y, z in positions:
        # Same artistic proportions as the interactive scene, viewed from +Z/+Y.
        x, y = x * 1.15, y * .94
        projected.append((500 + x * 185, 440 - (y * .981 - z * .196) * 185,
                          y * .196 + z * .981))
    return {"file": filename, "sha256": hashlib.sha256(data).hexdigest(),
            "vertices": vertices, "triangles": triangles, "points": projected, "faces": faces}


def build():
    surfaces = [read_surface("lh.bin"), read_surface("rh.bin")]
    depth = {}
    cell = lambda p: (int(p[0] // 4), int(p[1] // 4))
    for surface in surfaces:
        for point in surface["points"]:
            key = cell(point)
            depth[key] = max(depth.get(key, -math.inf), point[2])
    visible = lambda p: p[2] >= depth.get(cell(p), math.inf) - .07
    image = ET.Element("{" + SVG + "}svg", {"viewBox": "0 0 1000 1000", "width": "1000",
                       "height": "1000", "role": "img", "aria-labelledby": "fallback-title fallback-desc"})
    element(image, "title", id="fallback-title").text = "忆核大脑 · 皮层静态降级图"
    element(image, "desc", id="fallback-desc").text = (
        "由 Anderson Winkler 的真实皮层网格生成的金紫点线静态投影；青色光环为装饰。"
        "不是实时活动、用户记忆点或神经测量。CC BY-SA 3.0。")
    element(image, "metadata").text = json.dumps({
        "title": "Memory Brain cortical fallback", "generator": "scripts/build_fallback.py v1",
        "original_geometry_author": "Anderson Winkler",
        "original_geometry_source": "https://brainder.org/research/brain-for-blender/",
        "projection_and_artwork_author": "Memory Brain contributors",
        "license": "CC BY-SA 3.0 Unported",
        "license_url": "https://creativecommons.org/licenses/by-sa/3.0/",
        "changes": "Fixed orthographic projection, vertex depth sampling, gold/violet point and edge rendering, cyan decorative rings; no model generation.",
        "inputs": [{key: value for key, value in surface.items() if key not in ("points", "faces")} for surface in surfaces]
    }, ensure_ascii=False, separators=(",", ":"))
    defs = element(image, "defs")
    gradient = element(defs, "radialGradient", id="ambient")
    for offset, color, opacity in [("0%", "#133044", ".65"), ("70%", "#061522", ".3"), ("100%", "#020910", "0")]:
        element(gradient, "stop", offset=offset, stop_color=color, stop_opacity=opacity)
    element(image, "rect", width="1000", height="1000", fill="#020910")
    element(image, "ellipse", cx="500", cy="460", rx="430", ry="410", fill="url(#ambient)")
    stage = element(image, "g", fill="none", stroke="#43ddea")
    for rx, ry, cy, opacity in [(330, 72, 850, ".22"), (290, 60, 850, ".35"), (245, 48, 850, ".7"),
                                (210, 38, 850, ".3"), (235, 42, 137, ".38"), (205, 34, 137, ".65")]:
        element(stage, "ellipse", cx="500", cy=str(cy), rx=str(rx), ry=str(ry), opacity=opacity, stroke_width="1.2")
    element(stage, "path", d="M 500 110 L 500 875", opacity=".12", stroke_width="1")
    counts = {"points": 0, "edges": 0}
    for hemisphere, surface in enumerate(surfaces):
        color = "#ffc15c" if hemisphere == 0 else "#d283ff"
        lines = element(image, "g", fill="none", stroke=color, stroke_width=".72", opacity=".3")
        paths = []
        for index in range(0, len(surface["faces"]), 53):
            a, b, _ = surface["faces"][index]
            first, second = surface["points"][a], surface["points"][b]
            if visible(first) and visible(second):
                paths.append(f"M{first[0]:.1f},{first[1]:.1f}L{second[0]:.1f},{second[1]:.1f}")
        element(lines, "path", d="".join(paths)); counts["edges"] += len(paths)
        points = element(image, "g", fill=color)
        used = set()
        for index in range(0, len(surface["points"]), 4):
            point = surface["points"][index]; key = cell(point)
            if key in used or not visible(point):
                continue
            used.add(key); seed = (index * 2654435761) & 0xffffffff
            element(points, "circle", cx=f"{point[0]:.1f}", cy=f"{point[1]:.1f}",
                    r="1.15" if seed % 7 else "1.65", opacity=".62" if seed % 5 else ".95",
                    **({"fill": "#8eeaf2"} if seed % 37 == 0 else {}))
            counts["points"] += 1
    element(image, "text", x="500", y="937", text_anchor="middle", fill="#86acbb",
            font_family="sans-serif", font_size="16").text = "静态皮层投影 · 非实时记忆或神经活动"
    target = STATIC / "brain-fallback.svg"
    ET.ElementTree(image).write(target, encoding="utf-8", xml_declaration=True)
    # Reject executable/external-loading SVG constructs before shipping.
    parsed = ET.parse(target)
    forbidden = {"script", "foreignObject", "image", "use", "a", "animate", "set"}
    for node in parsed.iter():
        if node.tag.rsplit("}", 1)[-1] in forbidden:
            raise ValueError("Unexpected active or external SVG element")
        if any(key.lower().startswith("on") or key.rsplit("}", 1)[-1] == "href" for key in node.attrib):
            raise ValueError("Unexpected SVG handler or external reference")
    print(json.dumps({"file": str(target), "bytes": target.stat().st_size, **counts,
                      "xml_valid": True, "external_loads": False, "model_calls": 0}))


if __name__ == "__main__":
    build()
