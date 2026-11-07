"""Report images and embedded image payloads, on a copy of the Desktop-made fixture:

- pull hides embedded images: a base64 image in a custom theme, an SVG image URL in a theme, a base64
  image inside a text box and a base64 logo inside a DAX SVG measure become «payload:image#n» tokens; the
  readable parts stay; pushes restore every byte;
- `pbz image add` checks the file type from its header (PNG, JPEG, GIF, BMP, SVG; SVG without scripts or
  DTDs), refuses other files, reuses identical bytes, stages the file and registers it in report.json;
- `pbz style --canvas` (background / wallpaper color, transparency, image, scaling), image properties of
  visuals (plotArea.image, button fill.image, a web image URL), `pbz add visual image`;
- the integrity checks (flat image property, unregistered reference, missing file, bad item name,
  package type), push installing staged files and Microsoft's validator, `pbz image remove` (in use,
  then removed by push with a backup), and a deleted visual's folder leaving no empty directory.

Run: python tests/test_images.py
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
import zlib

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz import author, common, images, mirror, pbircheck, style, validate  # noqa: E402
from pbz.common import PbzError  # noqa: E402

B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
MARKS = {"theme": "CANARYIMGTHEME", "svg": "CANARYSVGTHEME", "text": "CANARYIMGTEXT", "dax": "CANARYIMGDAX"}


def png(w: int, h: int, rgb=(30, 80, 160)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def jpeg(w: int, h: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def gif(w: int, h: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", w, h) + b"\x00\x00\x00;"


def bmp(w: int, h: int) -> bytes:
    return b"BM" + struct.pack("<IHHI", 0, 0, 0, 54) + struct.pack("<Iii", 40, w, -h) + b"\x00" * 28


def load(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def save(path, doc):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")


def text_of(path) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def sha(path) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return None


def plant(root: str) -> None:
    """Embedded images in the real fixture copy: custom theme, text box, DAX SVG measure."""
    rep = os.path.join(root, "Canary.Report")
    rj_path = os.path.join(rep, "definition", "report.json")
    rj = load(rj_path)
    rj["themeCollection"]["customTheme"] = {"name": "Corp.json", "type": "RegisteredResources",
                                            "reportVersionAtImport": rj["themeCollection"]["baseTheme"].get("reportVersionAtImport")}
    rj["resourcePackages"].append({"name": "RegisteredResources", "type": "RegisteredResources",
                                   "items": [{"name": "Corp.json", "path": "Corp.json", "type": "CustomTheme"}]})
    save(rj_path, rj)
    theme = {"name": "Corp.json", "dataColors": ["#1F4E79", "#2E75B6"],
             "visualStyles": {
                 "page": {"*": {"background": [{"image": {"name": "bg", "scaling": "Fit",
                                                          "url": f"data:image/png;base64,{MARKS['theme']}{B64}"}}]}},
                 "actionButton": {"*": {"icon": [{"image": {"name": "icon", "url":
                                         "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg'><text>"
                                         f"{MARKS['svg']}</text></svg>"}}]}}}}
    os.makedirs(os.path.join(rep, "StaticResources", "RegisteredResources"))
    save(os.path.join(rep, "StaticResources", "RegisteredResources", "Corp.json"), theme)
    tb = os.path.join(rep, "definition", "pages", "530d248092b44ccf9450", "visuals", "e9a4b1c7d3f8260e1b7a", "visual.json")
    doc = load(tb)
    runs = doc["visual"]["objects"]["general"][0]["properties"]["paragraphs"][0]["textRuns"]
    runs.append({"value": f"Logo data:image/png;base64,{MARKS['text']}{B64} end"})
    runs.append({"value": "Data: quarterly view"})  # plain words, not a data: URI
    save(tb, doc)
    hours = os.path.join(root, "Canary.SemanticModel", "definition", "tables", "Hours.tmdl")
    with open(hours, encoding="utf-8") as f:
        t = f.read()
    svg = ("\tmeasure 'Logo SVG' = \"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg'>"
           f"<image href='data:image/png;base64,{MARKS['dax']}{B64}'/></svg>\"\n"
           "\t\tlineageTag: 5d1c2a77-0b8e-4c7e-9a3f-2f6f1c0e9b11\n\n")
    t = t.replace("\tmeasure 'Active Hours'", svg + "\tmeasure 'Active Hours'", 1)
    with open(hours, "w", encoding="utf-8", newline="\n") as f:
        f.write(t)


def main() -> int:
    fails: list[str] = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    def refused(fn, *args, code, **kw):
        try:
            fn(*args, **kw)
            check(False, f"{fn.__name__} {args[2:]} {kw} should fail with {code}")
        except PbzError as e:
            check(e.code == code, f"{fn.__name__}: {e.code} {e.detail} (want {code})")

    tmp = tempfile.mkdtemp(prefix="pbzimages-")
    root = os.path.join(tmp, "ws")
    shutil.copytree(os.path.join(SKILL, "tests", "fixture"), root)
    plant(root)
    ws = common.Workspace(root)
    ws.private = os.path.join(tmp, "private")
    project = ws.project()
    pull = mirror.pull(ws, project)
    check(not pull.get("lossy_files"), f"lossy files after pull: {pull.get('lossy_files')}")
    rep = os.path.join(root, "Canary.Report")
    mrep = project.mirror_report

    # ---------------------------------------------------------------- embedded images are hidden
    blob = ""
    for cur, _dirs, files in os.walk(ws.mirror):
        for f in files:
            blob += text_of(os.path.join(cur, f))
    for k, mark in MARKS.items():
        check(mark not in blob, f"embedded image ({k}) visible in the mirror")
    mtheme = text_of(os.path.join(mrep, "StaticResources", "RegisteredResources", "Corp.json"))
    check(mtheme.count("«payload:image#") == 2, f"theme images not tokenized: {mtheme[:300]}")
    check('"#1F4E79"' in mtheme and '"scaling": "Fit"' in mtheme, "theme colors / scaling should stay readable")
    json.loads(mtheme)
    mtb = text_of(os.path.join(mrep, "definition", "pages", "530d248092b44ccf9450", "visuals", "e9a4b1c7d3f8260e1b7a", "visual.json"))
    check("Logo «payload:image#" in mtb and "» end" in mtb, "text box: the image should be cut out, the words kept")
    check('"Data: quarterly view"' in mtb, "plain text starting with 'Data:' must stay readable")
    mhours = text_of(os.path.join(project.mirror_model, "definition", "tables", "Hours.tmdl"))
    check("<image href='«payload:image#" in mhours and "data:image/svg+xml;utf8,<svg" in mhours,
          "DAX SVG measure: the template should stay readable, the logo hidden")

    # round trip: edits next to the tokens restore every byte of the payloads
    t = json.loads(mtheme)
    t["dataColors"][1] = "#C55A11"
    save(os.path.join(mrep, "StaticResources", "RegisteredResources", "Corp.json"), t)
    doc = json.loads(mtb)
    doc["position"]["x"] = doc["position"]["x"] + 10
    save(os.path.join(mrep, "definition", "pages", "530d248092b44ccf9450", "visuals", "e9a4b1c7d3f8260e1b7a", "visual.json"), doc)
    with open(os.path.join(project.mirror_model, "definition", "tables", "Hours.tmdl"), "w", encoding="utf-8", newline="\n") as f:
        f.write(mhours.replace("\t\tlineageTag: 5d1c2a77-0b8e-4c7e-9a3f-2f6f1c0e9b11",
                               "\t\tdisplayFolder: Images\n\t\tlineageTag: 5d1c2a77-0b8e-4c7e-9a3f-2f6f1c0e9b11"))
    r = mirror.push(ws, project)
    check(r.get("result") == "PUSHED", f"push of edits next to payload tokens: {r.get('result')} {r.get('findings')}")
    rt = load(os.path.join(rep, "StaticResources", "RegisteredResources", "Corp.json"))
    check(rt["visualStyles"]["page"]["*"]["background"][0]["image"]["url"] == f"data:image/png;base64,{MARKS['theme']}{B64}"
          and MARKS["svg"] in rt["visualStyles"]["actionButton"]["*"]["icon"][0]["image"]["url"]
          and rt["dataColors"][1] == "#C55A11", "theme payloads not restored exactly")
    check(f"Logo data:image/png;base64,{MARKS['text']}{B64} end" in text_of(
        os.path.join(rep, "definition", "pages", "530d248092b44ccf9450", "visuals", "e9a4b1c7d3f8260e1b7a", "visual.json")),
        "text box payload not restored")
    real_hours = text_of(os.path.join(root, "Canary.SemanticModel", "definition", "tables", "Hours.tmdl"))
    check(f"base64,{MARKS['dax']}{B64}'/></svg>\"" in real_hours and "displayFolder: Images" in real_hours,
          "DAX payload not restored")

    # ---------------------------------------------------------------- file checks
    assets = os.path.join(tmp, "assets")
    os.makedirs(assets)

    def asset(name, data):
        p = os.path.join(assets, name)
        with open(p, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        return p

    logo = asset("Company Logo.png", png(400, 100))
    bgp = asset("bg.png", png(64, 36, (240, 240, 240)))
    for name, data, w, h in (("photo.jpg", jpeg(800, 600), 800, 600), ("anim.gif", gif(48, 24), 48, 24),
                             ("old.bmp", bmp(30, 20), 30, 20)):
        k, iw, ih = images.sniff(data)
        check((iw, ih) == (w, h), f"{name}: pixels {(iw, ih)} (want {(w, h)})")
    check(images.sniff(b'<svg xmlns="http://www.w3.org/2000/svg" width="120px" height="40"/>'.replace(b"/>", b"></svg>"))[1:] == (120, 40),
          "svg width/height")
    check(images.sniff(b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 150"></svg>')[1:] == (300, 150),
          "svg viewBox")
    refused(images.add, ws, project, asset("data.png", "Name,Amount\nx,1\n"), code="BAD_IMAGE")
    refused(images.add, ws, project, asset("wrong.jpg", png(4, 4)), code="BAD_IMAGE")
    refused(images.add, ws, project, asset("notes.txt", png(4, 4)), code="BAD_IMAGE")
    refused(images.add, ws, project, asset("evil.svg", '<svg xmlns="http://www.w3.org/2000/svg"><script>x()</script></svg>'),
            code="BAD_IMAGE")
    refused(images.add, ws, project, asset("onload.svg", '<svg xmlns="http://www.w3.org/2000/svg" onload="x()"></svg>'),
            code="BAD_IMAGE")
    refused(images.add, ws, project, asset("dtd.svg", '<!DOCTYPE svg [<!ENTITY a "b">]><svg xmlns="http://www.w3.org/2000/svg"></svg>'),
            code="BAD_IMAGE")
    refused(images.add, ws, project, os.path.join(assets, "missing.png"), code="NOT_FOUND")
    old_max = images.MAX_BYTES
    images.MAX_BYTES = 100
    refused(images.add, ws, project, logo, code="TOO_LARGE")
    images.MAX_BYTES = old_max

    # ---------------------------------------------------------------- add, list
    a = images.add(ws, project, logo)
    b = images.add(ws, project, bgp, name="Canvas Background")
    again = images.add(ws, project, bgp)
    li, bi = a["item"], b["item"]
    check(a["result"] == "STAGED" and a["pixels"] == [400, 100] and a["kind"] == "png", f"add: {a}")
    check(li.startswith("Company_Logo") and li.endswith(".png") and images.display_name(li) == "Company_Logo.png", li)
    check(bi.startswith("Canvas_Background"), bi)
    check(again["result"] == "PRESENT" and again["item"] == bi, f"same bytes should reuse the item: {again}")
    reg = images.registered(text_of(os.path.join(mrep, "definition", "report.json")))
    check(set(reg) == {li, bi} and all(reg[i] == {"name": i, "path": i, "type": "Image"} for i in reg), f"registry: {reg}")
    check(load(os.path.join(mrep, "definition", "report.json"))["themeCollection"]["customTheme"]["name"] == "Corp.json",
          "custom theme registration must stay")
    lst = images.list_(ws, project)
    check([r["status"] for r in lst["images"]] == ["staged", "staged"], f"list: {lst}")

    # ---------------------------------------------------------------- canvas
    pg = "Canary Page"
    c = style.run_canvas(ws, project, pg, sets=[f"background.image={bi}", "background.scaling=fill",
                                                "wallpaper.color=#1F2A44", f"wallpaper.image={images.display_name(bi)}"])
    pj = os.path.join(mrep, "definition", "pages", "530d248092b44ccf9450", "page.json")
    objs = load(pj)["objects"]
    bimg = objs["background"][0]["properties"]["image"]["image"]
    check(bimg["url"] == images.ref_expr(bi) and bimg["scaling"]["expr"]["Literal"]["Value"] == "'Fill'"
          and bimg["name"]["expr"]["Literal"]["Value"] == "'Canvas_Background.png'", f"background image: {bimg}")
    check(objs["background"][0]["properties"]["transparency"]["expr"]["Literal"]["Value"] == "0D", "transparency 0 with an image")
    check(objs["outspace"][0]["properties"]["color"]["solid"]["color"]["expr"]["Literal"]["Value"] == "'#1F2A44'", "wallpaper color")
    check(objs["outspace"][0]["properties"]["image"]["image"]["scaling"]["expr"]["Literal"]["Value"] == "'Fit'", "default fit")
    check(c.get("notes"), "transparency note")
    shown = style.run_canvas(ws, project, pg, show=True)["formatting"][0]["properties"]
    check(f"background.image = image:{bi} (fill)" in shown and "wallpaper.color = #1F2A44" in shown, f"show: {shown}")
    style.run_canvas(ws, project, pg, unsets=["wallpaper.image", "background.scaling"])
    objs = load(pj)["objects"]
    check("image" not in objs["outspace"][0]["properties"] and "scaling" not in objs["background"][0]["properties"]["image"]["image"],
          "unset image / scaling")
    refused(style.run_canvas, ws, project, pg, sets=["background.color=measure:[Total Hours]"], code="UNSUPPORTED")
    refused(style.run_canvas, ws, project, pg, sets=["background.transparency=140"], code="BAD_REQUEST")
    refused(style.run_canvas, ws, project, pg, sets=["wallpaper.scaling=fit"], code="BAD_REQUEST")
    refused(style.run_canvas, ws, project, pg, sets=["background.scaling=tile"], code="BAD_REQUEST")
    refused(style.run_canvas, ws, project, pg, sets=["background.show=true"], code="BAD_REQUEST")
    refused(style.run_canvas, ws, project, pg, sets=["background.image=nope.png"], code="NOT_FOUND")
    refused(style.run_canvas, ws, project, None, sets=["background.color=#fff"], code="BAD_REQUEST")
    allp = style.run_canvas(ws, project, None, sets=["background.color=theme:0"], all_=True)
    check(len(allp["changed"]) >= 1, "canvas --all")

    # ---------------------------------------------------------------- visuals
    refused(author.add_visual, ws, project, "image", pg, [], code="BAD_REQUEST")
    refused(author.add_visual, ws, project, "card", pg, ["[Total Hours]"], image=li, code="BAD_REQUEST")
    refused(author.add_visual, ws, project, "image", pg, [], image="nope.png", code="NOT_FOUND")
    refused(author.add_visual, ws, project, "image", pg, [], image=li, fit="tile", code="BAD_REQUEST")
    v = author.add_visual(ws, project, "image", pg, [], image=li, fit="fill", alt="Company logo")
    vdoc = load(os.path.join(ws.mirror, v["files"][0]))
    vo = vdoc["visual"]
    check(vo["objects"]["general"][0]["properties"]["imageUrl"] == images.ref_expr(li)
          and vo["objects"]["imageScaling"][0]["properties"]["imageScalingType"]["expr"]["Literal"]["Value"] == "'Fill'",
          f"image visual objects: {vo.get('objects')}")
    check(vo["visualContainerObjects"]["general"][0]["properties"]["altText"]["expr"]["Literal"]["Value"] == "'Company logo'",
          "alt text")
    check(v["box"][2:] == [320, 80], f"image visual keeps the image's proportions: {v['box']}")
    bar = "8b3e2c7f1a9d4e6b0c52"
    refused(style.run, ws, project, pg, bar, sets=[f"plotArea.image={bi}"], code="UNSUPPORTED")  # plotArea.url clashes
    style.run(ws, project, pg, v["id"], sets=["image.sourceUrl=https://example.com/logo.png"])
    vdoc = load(os.path.join(ws.mirror, v["files"][0]))
    check(vdoc["visual"]["objects"]["image"][0]["properties"]["sourceUrl"]["expr"]["Literal"]["Value"]
          == "'https://example.com/logo.png'", "web image URL")
    style.run(ws, project, pg, v["id"], unsets=["image.sourceUrl"])
    btn = author.add_visual(ws, project, "actionButton", pg, [])
    style.run(ws, project, pg, btn["id"], sets=[f"fill.image={li}"], state="default")
    shown = style.run(ws, project, pg, btn["id"], show=True)["formatting"][0]["properties"]
    check(any(r.startswith("fill.image [id=default] = image:" + li) for r in shown), f"button fill image: {shown}")
    used = images.uses(mrep)
    check(len(used.get(li, [])) == 2 and len(used.get(bi, [])) == 1, f"uses: {used}")

    # ---------------------------------------------------------------- integrity checks
    def errors(**kw):
        return {f["code"] for f in pbircheck.run(mrep, has_image=lambda n: images.present(ws, project, n), **kw)
                if f["severity"] == "error"}

    check(not {c for c in errors() if c.startswith("PBIR_IMAGE")}, f"clean mirror: {errors()}")
    good_pj = text_of(pj)
    pd = load(pj)
    pd["objects"]["background"][0]["properties"]["image"] = pd["objects"]["background"][0]["properties"]["image"]["image"]
    save(pj, pd)
    check("PBIR_IMAGE_FLAT" in errors(), "flat image property")
    pd = json.loads(good_pj)
    pd["objects"]["background"][0]["properties"]["image"]["image"]["url"] = images.ref_expr("ghost123456789.png")
    pd["objects"]["background"][0]["properties"]["image"]["image"]["url"]["expr"]["ResourcePackageItem"]["PackageType"] = 0
    save(pj, pd)
    ec = errors()
    check({"PBIR_IMAGE_NOT_REGISTERED", "PBIR_IMAGE_PACKAGE_TYPE"} <= ec, f"unregistered / package type: {ec}")
    with open(pj, "w", encoding="utf-8") as f:
        f.write(good_pj)
    rjp = os.path.join(mrep, "definition", "report.json")
    good_rj = text_of(rjp)
    rd = json.loads(good_rj)
    rr = next(p for p in rd["resourcePackages"] if p["name"] == "RegisteredResources")
    rr["items"] += [{"name": "..\\..\\evil.png", "path": "..\\..\\evil.png", "type": "Image"},
                    {"name": "lost123456789.png", "path": "lost123456789.png", "type": "Image"}]
    save(rjp, rd)
    ec = errors()
    check({"PBIR_IMAGE_NAME", "PBIR_IMAGE_MISSING"} <= ec, f"bad name / missing file: {ec}")
    check(set(images.staged(ws, project, text_of(rjp))) == {li, bi}, "staged() must ignore unsafe names")
    r = mirror.push(ws, project)
    check(r.get("result") == "INVALID", f"push with a bad image registration: {r.get('result')}")
    with open(rjp, "w", encoding="utf-8") as f:
        f.write(good_rj)

    # ---------------------------------------------------------------- push installs
    r = mirror.push(ws, project)
    check(r.get("result") == "PUSHED", f"push: {r.get('result')} {[f for f in r.get('findings', []) if f['severity'] == 'error']}")
    check(sorted(r.get("images_installed") or []) == sorted([li, bi]), f"installed: {r.get('images_installed')}")
    rrd = os.path.join(rep, "StaticResources", "RegisteredResources")
    check(sha(os.path.join(rrd, li)) == sha(logo) and sha(os.path.join(rrd, bi)) == sha(bgp), "installed bytes differ")
    check(not os.listdir(os.path.join(ws.private, images.PENDING, project.name)), "pending files should be cleared")
    check([x["status"] for x in images.list_(ws, project)["images"]] == ["present", "present"], "list after push")

    # ---------------------------------------------------------------- remove
    refused(images.remove, ws, project, li, code="IN_USE")
    shutil.rmtree(os.path.join(mrep, "definition", "pages", "530d248092b44ccf9450", "visuals", v["id"]))
    shutil.rmtree(os.path.join(mrep, "definition", "pages", "530d248092b44ccf9450", "visuals", btn["id"]))
    out = images.remove(ws, project, images.display_name(li))
    check(out["item"] == li and li not in images.registered(text_of(rjp)), f"remove: {out}")
    check(images.list_(ws, project).get("removed_on_push") == [li], "removed_on_push")
    r = mirror.push(ws, project)
    check(r.get("result") == "PUSHED" and r.get("images_removed") == [li], f"push remove: {r.get('result')} {r.get('images_removed')}")
    check(not os.path.exists(os.path.join(rrd, li)) and os.path.exists(os.path.join(rrd, bi)), "real image files after remove")
    check(not os.path.exists(os.path.join(rep, "definition", "pages", "530d248092b44ccf9450", "visuals", v["id"])),
          "a deleted visual leaves no empty folder")
    backup = os.path.join(ws.private, "backups", r["backup"], "Canary.Report", "StaticResources", "RegisteredResources", li)
    check(os.path.isfile(backup) and sha(backup) == sha(logo), "removed image backed up")
    staged_only = images.add(ws, project, asset("temp.gif", gif(10, 10)))
    images.remove(ws, project, staged_only["item"])
    check(not os.listdir(os.path.join(ws.private, images.PENDING, project.name)), "removing a staged image drops the file")
    check(mirror.push(ws, project).get("result") in ("NOTHING_TO_PUSH", "PUSHED"), "nothing left to push")
    check(validate.run_mirror(ws, project) is not None, "validate")

    if fails:
        print("\n".join("FAIL " + f for f in fails))
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
