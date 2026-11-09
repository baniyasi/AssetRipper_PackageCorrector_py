#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
All-in-one AssetRipper shader fix.

Fixes vs previous version:
  * candidates_by_name entry for unitypackage shaders now carries
    is_shader=True (was missing -> KeyError in [3])
  * extraction loop uses .get() so a missing key degrades to False

Run from VSCode. Unity must be closed.
"""
import os
import re
import tarfile
from pathlib import Path

SHADER_GUID_RE = re.compile(r"^guid:\s*([0-9a-fA-F]{32})\s*$", re.MULTILINE)
SHADER_NAME_RE = re.compile(r'^\s*Shader\s+"([^"]+)"', re.MULTILINE)
PROPS_BLOCK_RE = re.compile(r"Properties\s*\{([^}]*)\}", re.DOTALL)
PROP_NAME_RE = re.compile(
    r"^\s*(?:\[[^\]]+\]\s*)*([A-Za-z_][A-Za-z0-9_]*)\s*[\(\=]", re.MULTILINE
)
INCLUDE_RE = re.compile(r'#include\s+["<]([^">]+)[">]')
MAT_SHADER_RE = re.compile(
    r"(m_Shader:\s*\{\s*fileID:\s*-?\d+,\s*guid:\s*)([0-9a-fA-F]{32})(,\s*type:\s*-?\d+\s*\})"
)
PP_FIELD_RE = re.compile(
    r"^(\s+)([A-Za-z_]\w*):\s*\{fileID:\s*-?\d+,\s*guid:\s*([0-9a-fA-F]{32}),\s*type:\s*3\}\s*$"
)

SKIP_DIRS = {".git", ".vs", "obj", "Temp", "Logs", "UserSettings", "Build", ".idea"}
INCLUDE_EXTS = (".cginc", ".hlsl")
UNITY_BUILTIN_INCLUDES = {
    "UnityCG.cginc", "HLSLSupport.cginc", "UnityShaderVariables.cginc",
    "Lighting.cginc", "AutoLight.cginc", "UnityStandardCore.cginc",
    "UnityStandardUtils.cginc", "UnityUI.cginc", "UnityPBSLighting.cginc",
    "UnityStandardInput.cginc",
}
PP_FIELD_ALIASES = {
    "smaa":          "SubpixelMorphologicalAntialiasing",
    "textures":      "Texture2DLerp",
    "waveforms":     "Waveform",
    "debugOverlays": "Overlays",
}
TARGET_SUBDIR = "ImportedShaders"


def read_text(p):
    return p.read_text(encoding="utf-8-sig", errors="replace")


def write_text(p, t):
    p.write_text(t, encoding="utf-8")


def decode_asset_bytes(b):
    try:
        return b.decode("utf-8-sig")
    except UnicodeDecodeError:
        return b.decode("latin-1", errors="replace")


def normalize_crlf(data):
    if isinstance(data, bytes):
        return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return data.replace("\r\n", "\n").replace("\r", "\n")


def write_normalized_bytes(p, data):
    fixed = normalize_crlf(data)
    p.write_bytes(fixed)
    return fixed != data


def clean_pathname(raw):
    if not raw:
        return ""
    first = raw.split("\n")[0]
    return first.rstrip("\x00 \r\n\t")


def _tk_root():
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    return root


def pick_folder(title, initial=None):
    try:
        from tkinter import filedialog
        root = _tk_root()
        folder = filedialog.askdirectory(title=title, initialdir=str(initial or Path.cwd()))
        root.destroy()
        return Path(folder) if folder else None
    except Exception as e:
        print(f"[!] tkinter unavailable: {e}")
        return None


def confirm(message, title="Confirm"):
    try:
        from tkinter import messagebox
        root = _tk_root()
        r = messagebox.askyesno(title, message)
        root.destroy()
        return bool(r)
    except Exception as e:
        print(f"[!] tkinter unavailable: {e}")
        return False


def find_project_root(start):
    start = start.resolve()
    cur = start
    while True:
        for cand in (cur, cur / "ExportedProject"):
            if (cand / "Assets").is_dir() and (cand / "ProjectSettings").is_dir():
                return cand
        if cur.parent == cur:
            break
        cur = cur.parent
    raise SystemExit(f"[x] Project root not found from {start}")


def iter_files(root, suffixes):
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if any(fn.endswith(s) for s in suffixes):
                yield Path(dirpath) / fn


def parse_guid(meta_path):
    if not meta_path.is_file():
        return None
    m = SHADER_GUID_RE.search(read_text(meta_path))
    return m.group(1).lower() if m else None


def extract_props(text):
    props = set()
    pm = PROPS_BLOCK_RE.search(text)
    if pm:
        for x in PROP_NAME_RE.finditer(pm.group(1)):
            props.add(x.group(1))
    return props


def parse_shader_info(shader_path):
    text = read_text(shader_path)
    m = SHADER_NAME_RE.search(text)
    has_cg = "CGPROGRAM" in text or "CGINCLUDE" in text
    has_hlsl = "HLSLPROGRAM" in text or "HLSLINCLUDE" in text
    has_dummy = "DummyShaderTextExporter" in text
    is_dummy = has_dummy or (not has_cg and not has_hlsl)
    return {
        "path": shader_path,
        "size": shader_path.stat().st_size,
        "shader_name": m.group(1) if m else None,
        "is_dummy": is_dummy,
        "guid": parse_guid(Path(str(shader_path) + ".meta")),
        "props": extract_props(text),
        "includes": INCLUDE_RE.findall(text),
    }


def jaccard(a, b):
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def scan_package(pkg_path):
    out = []
    try:
        tf = tarfile.open(str(pkg_path), "r:*")
    except Exception as e:
        print(f"[!] Cannot open {pkg_path}: {e}")
        return out
    with tf:
        current = {}
        for member in tf:
            parts = member.name.split("/", 1)
            if len(parts) != 2:
                continue
            guid_dir, fname = parts
            if len(guid_dir) != 32 or not all(c in "0123456789abcdef" for c in guid_dir.lower()):
                continue
            if fname not in ("asset", "asset.meta", "pathname"):
                continue
            entry = current.setdefault(guid_dir, {})
            f = tf.extractfile(member)
            if f is None:
                continue
            data = f.read()
            if fname == "pathname":
                entry["pathname"] = clean_pathname(data.decode("utf-8", errors="replace"))
            elif fname == "asset":
                entry["asset_bytes"] = data
            elif fname == "asset.meta":
                entry["meta_text"] = decode_asset_bytes(data)

        for guid_dir, entry in current.items():
            pathname = entry.get("pathname", "")
            if not pathname:
                continue
            low = pathname.lower()
            if not (low.endswith(".shader") or any(low.endswith(e) for e in INCLUDE_EXTS)):
                continue
            asset_bytes = entry.get("asset_bytes", b"")
            meta_text = entry.get("meta_text", "")
            meta_guid = SHADER_GUID_RE.search(meta_text)
            text = decode_asset_bytes(asset_bytes)
            if low.endswith(".shader"):
                m = SHADER_NAME_RE.search(text)
                name = m.group(1) if m else None
                props = extract_props(text)
                includes = INCLUDE_RE.findall(text)
            else:
                name, props, includes = None, set(), INCLUDE_RE.findall(text)
            out.append({
                "pathname": pathname,
                "filename": pathname.rsplit("/", 1)[-1],
                "asset_bytes": asset_bytes,
                "meta_text": meta_text,
                "size": len(asset_bytes),
                "name": name,
                "props": props,
                "includes": includes,
                "is_shader": low.endswith(".shader"),
                "guid": (meta_guid.group(1).lower() if meta_guid else guid_dir.lower()),
            })
    return out


def resolve_target(project_root, pathname):
    assets_root = project_root / "Assets"
    if pathname.startswith("Assets/Shader/"):
        return assets_root / pathname[len("Assets/"):]
    if pathname.startswith("Assets/"):
        return assets_root / "Shader" / TARGET_SUBDIR / pathname[len("Assets/"):]
    return assets_root / "Shader" / TARGET_SUBDIR / pathname


def field_to_pascal(field):
    parts = re.split(r"[_\s]+", field)
    return "".join(p[0].upper() + p[1:] if p else "" for p in parts)


def build_pp_index(pp_pc_root):
    by_decl, by_file = {}, {}
    for sp in iter_files(pp_pc_root, (".shader",)):
        text = read_text(sp)
        m = SHADER_NAME_RE.search(text)
        guid = parse_guid(Path(str(sp) + ".meta"))
        if not guid:
            continue
        if m:
            by_decl[m.group(1)] = guid
        by_file[sp.stem] = guid
    return by_decl, by_file


def build_assets_guid_to_name(assets_root):
    mp = {}
    for sp in iter_files(assets_root, (".shader",)):
        guid = parse_guid(Path(str(sp) + ".meta"))
        m = SHADER_NAME_RE.search(read_text(sp))
        if guid and m:
            mp[guid] = m.group(1)
    return mp


def find_pp_resources(assets_root):
    hits = []
    for p in iter_files(assets_root, (".asset",)):
        if p.name == "PostProcessResources.asset":
            hits.append(p)
            continue
        try:
            if p.stat().st_size > 2 * 1024 * 1024:
                continue
            text = read_text(p)
        except Exception:
            continue
        if "shaders:" in text and "copyStd:" in text and "uber:" in text:
            hits.append(p)
    return hits


def _resolve_pp_field(field, old_guid, by_decl, by_file, guid_to_name):
    shader_name = guid_to_name.get(old_guid)
    if shader_name and shader_name in by_decl:
        return by_decl[shader_name], f"reverse:{shader_name}"
    pascal = field_to_pascal(field)
    if pascal in by_file:
        return by_file[pascal], f"file:{pascal}"
    for dname, dguid in by_decl.items():
        if dname.rsplit("/", 1)[-1] == pascal:
            return dguid, f"decl:{dname}"
    alias = PP_FIELD_ALIASES.get(field)
    if alias:
        for dname, dguid in by_decl.items():
            if dname.rsplit("/", 1)[-1] == alias:
                return dguid, f"alias:{alias}"
        if alias in by_file:
            return by_file[alias], f"alias-file:{alias}"
    return None, "unmatched"


def fix_one_pp_resources(res_asset, by_decl, by_file, guid_to_name):
    text = read_text(res_asset)
    lines = text.splitlines()
    out = []
    changes = []
    warnings = []
    in_shaders = False
    shaders_indent = -1
    for ln in lines:
        m_enter = re.match(r"^(\s*)shaders:\s*$", ln)
        if m_enter:
            in_shaders = True
            shaders_indent = len(m_enter.group(1))
            out.append(ln)
            continue
        if in_shaders and ln.strip():
            cur_indent = len(ln) - len(ln.lstrip())
            if cur_indent <= shaders_indent:
                in_shaders = False
        if in_shaders:
            fm = PP_FIELD_RE.match(ln)
            if fm:
                indent, field, old_guid = fm.group(1), fm.group(2), fm.group(3).lower()
                new_guid, strategy = _resolve_pp_field(
                    field, old_guid, by_decl, by_file, guid_to_name
                )
                if new_guid and new_guid != old_guid:
                    out.append(
                        f"{indent}{field}: {{fileID: 4800000, guid: {new_guid}, type: 3}}"
                    )
                    changes.append((field, old_guid, new_guid, strategy))
                    continue
                if not new_guid:
                    warnings.append((field, old_guid))
        out.append(ln)
    if changes:
        write_text(res_asset, "\n".join(out))
    return changes, warnings


def normalize_imported_shaders(assets_root):
    target = assets_root / "Shader" / TARGET_SUBDIR
    if not target.is_dir():
        return []
    changed = []
    for p in iter_files(target, (".shader", ".meta", ".cginc", ".hlsl")):
        try:
            raw = p.read_bytes()
        except Exception:
            continue
        if write_normalized_bytes(p, raw):
            changed.append(p)
    return changed


def main():
    print("=" * 78)
    print(" All-in-one AssetRipper shader fix")
    print("=" * 78)
    print()

    print("[Step 1] Select project folder")
    proj_dir = pick_folder("Select project folder (PpSs / ExportedProject / Assets)")
    if proj_dir is None:
        print("[x] No project folder selected")
        return
    project = find_project_root(proj_dir)
    assets_root = project / "Assets"
    print(f"[i] Project: {project}")
    print(f"[i] Assets:  {assets_root}")
    print()

    print("[Step 2] Select folder with .unitypackage files (Cancel to skip)")
    pkg_dir = pick_folder("Select folder with .unitypackage files")
    pkg_files = []
    if pkg_dir is not None:
        pkg_files = sorted(
            p for p in pkg_dir.iterdir()
            if p.is_file() and p.suffix == ".unitypackage"
        )
        if not pkg_files:
            pkg_files = list(iter_files(pkg_dir, (".unitypackage",)))
        if not pkg_files:
            print(f"[!] No .unitypackage under {pkg_dir}, continuing without")
    else:
        print("[i] Skipping .unitypackage folder")
    print()

    print("-" * 78)
    print(" [1] Scan")
    print("-" * 78)
    assets_shaders = []
    for sp in iter_files(assets_root, (".shader",)):
        info = parse_shader_info(sp)
        info["source"] = "Assets"
        assets_shaders.append(info)

    packaged_shaders = []
    for root, tag in ((project / "Library" / "PackageCache", "PackageCache"),
                      (project / "Packages", "Packages")):
        if root.is_dir():
            for sp in iter_files(root, (".shader",)):
                info = parse_shader_info(sp)
                info["source"] = tag
                packaged_shaders.append(info)

    print(f"  Assets shaders      : {len(assets_shaders)} "
          f"(dummy {sum(1 for s in assets_shaders if s['is_dummy'])}, "
          f"real {sum(1 for s in assets_shaders if not s['is_dummy'])})")
    print(f"  PackageCache shaders: "
          f"{sum(1 for s in packaged_shaders if s['source']=='PackageCache')}")
    print(f"  Packages shaders    : "
          f"{sum(1 for s in packaged_shaders if s['source']=='Packages')}")

    pkg_assets = []
    if pkg_files:
        for p in pkg_files:
            got = scan_package(p)
            nsh = sum(1 for a in got if a["is_shader"])
            print(f"  {p.name}: {len(got)} shader-related, {nsh} shader(s)")
            pkg_assets.extend(got)
    print()

    print("-" * 78)
    print(" [2] Match Dummy -> Real")
    print("-" * 78)
    candidates_by_name = {}

    for s in packaged_shaders:
        if not s["is_dummy"] and s["shader_name"] and s["guid"]:
            candidates_by_name.setdefault(s["shader_name"], []).append({
                "name": s["shader_name"],
                "guid": s["guid"],
                "source": s["source"],
                "path": str(s["path"]),
                "pathname": None,
                "size": s["size"],
                "props": s["props"],
                "includes": s["includes"],
                "is_external": True,
                "is_shader": True,
            })

    for a in pkg_assets:
        if a["is_shader"] and a["name"]:
            candidates_by_name.setdefault(a["name"], []).append({
                "name": a["name"],
                "guid": a["guid"],
                "source": "unitypackage",
                "path": None,
                "pathname": a["pathname"],
                "size": a["size"],
                "props": a["props"],
                "includes": a["includes"],
                "is_external": False,
                "is_shader": True,  # <- FIX: previously missing
                "asset_bytes": a["asset_bytes"],
                "meta_text": a["meta_text"],
            })

    pairs = []
    unmatched = []
    for d in assets_shaders:
        if not d["is_dummy"] or not d["guid"]:
            continue
        cands = candidates_by_name.get(d["shader_name"], [])
        if not cands:
            unmatched.append(d)
            continue
        cands_sorted = sorted(cands, key=lambda c: (not c["is_external"],))
        best = cands_sorted[0]
        best_j = max((jaccard(d["props"], c["props"]) for c in cands), default=0.0)
        pairs.append((d, best, f"name+jaccard({best_j:.2f})"))

    print(f"  Matched  : {len(pairs)}")
    print(f"  Unmatched: {len(unmatched)}")
    print()
    for d, c, strat in pairs:
        try:
            drel = d["path"].relative_to(project)
        except ValueError:
            drel = d["path"]
        srcpath = c.get("pathname") or c.get("path") or "(package)"
        print(f"  [OK] {d['shader_name']}")
        print(f"       dummy: {drel}  (guid={d['guid']})")
        print(f"       real : {srcpath}  ({c['source']}, guid={c['guid']})")
        print(f"       strategy: {strat}")

    if unmatched:
        print()
        print("  Unmatched (kept as-is):")
        for d in unmatched:
            try:
                drel = d["path"].relative_to(project)
            except ValueError:
                drel = d["path"]
            print(f"    {d['shader_name'] or '(unnamed)'}  ({drel})")
        # group PostProcessing missing case as a hint
        pp_missing = [d for d in unmatched if (d["shader_name"] or "").startswith("Hidden/PostProcessing/")]
        if pp_missing:
            print()
            print(f"  [!] {len(pp_missing)} PostProcessing Dummy shader(s) unmatched.")
            print(f"      com.unity.postprocessing has no real shaders in PackageCache.")
            print(f"      Install it via Package Manager, then re-run this script.")
    print()

    pkg_pairs = [(d, c, s) for d, c, s in pairs if not c["is_external"]]
    if pkg_pairs:
        print("-" * 78)
        print(f" [3] Extract {len(pkg_pairs)} shader(s) from .unitypackage")
        print(f"     (CRLF normalized on write)")
        print("-" * 78)
        if confirm(f"Extract {len(pkg_pairs)} shader(s) + include deps?",
                   title="Extract shaders"):
            to_extract = []
            seen = set()
            for d, c, _ in pkg_pairs:
                if c["guid"] not in seen:
                    seen.add(c["guid"])
                    to_extract.append(c)

            pkg_by_filename = {}
            for a in pkg_assets:
                if not a["is_shader"]:
                    pkg_by_filename.setdefault(a["filename"], []).append(a)
            includes_to_fetch = set()
            for a in list(to_extract):
                for inc in a.get("includes", []):
                    if inc in UNITY_BUILTIN_INCLUDES:
                        continue
                    includes_to_fetch.add(inc.rsplit("/", 1)[-1])
            for fname in includes_to_fetch:
                for c in pkg_by_filename.get(fname, []):
                    if c["guid"] not in seen:
                        seen.add(c["guid"])
                        to_extract.append({
                            "pathname": c["pathname"],
                            "asset_bytes": c["asset_bytes"],
                            "meta_text": c["meta_text"],
                            "guid": c["guid"],
                            "is_shader": False,
                        })

            for a in to_extract:
                dest = resolve_target(project, a["pathname"])
                dest.parent.mkdir(parents=True, exist_ok=True)
                if a.get("is_shader", False):  # <- defensive
                    write_normalized_bytes(dest, a["asset_bytes"])
                else:
                    dest.write_bytes(a["asset_bytes"])
                if a["meta_text"]:
                    write_normalized_bytes(
                        Path(str(dest) + ".meta"),
                        a["meta_text"].encode("utf-8"),
                    )
                print(f"  + {dest.relative_to(project)}  (guid={a['guid']})")
        else:
            print("[i] Skipped extraction")
        print()

    print("-" * 78)
    print(" [4] CRLF-normalize existing Assets/Shader/ImportedShaders/ files")
    print("-" * 78)
    existing_bad = []
    target = assets_root / "Shader" / TARGET_SUBDIR
    if target.is_dir():
        for p in iter_files(target, (".shader", ".meta", ".cginc", ".hlsl")):
            try:
                raw = p.read_bytes()
            except Exception:
                continue
            if normalize_crlf(raw) != raw:
                existing_bad.append(p)
    if not existing_bad:
        print("  (no files require normalization)")
        print()
    else:
        print(f"  {len(existing_bad)} file(s) contain CRLF variants:")
        for p in existing_bad:
            print(f"    {p.relative_to(project)}")
        print()
        if confirm(
            f"Normalize line endings on {len(existing_bad)} file(s)?\n"
            f"(\\r\\r\\n, \\r\\n, lone \\r -> \\n)",
            title="CRLF normalize",
        ):
            changed = normalize_imported_shaders(assets_root)
            print(f"[+] Rewrote {len(changed)} file(s)")
        else:
            print("[i] Skipped CRLF normalization")
        print()

    pp_resources = find_pp_resources(assets_root)
    if pp_resources:
        print("-" * 78)
        print(f" [5] Repair PostProcessResources.asset ({len(pp_resources)} file)")
        print("-" * 78)
        pp_pc = None
        pc = project / "Library" / "PackageCache"
        if pc.is_dir():
            for d in pc.iterdir():
                if d.is_dir() and d.name.lower().startswith("com.unity.postprocessing@"):
                    pp_pc = d
                    break
        if pp_pc is None:
            print("[!] com.unity.postprocessing not in PackageCache, skipping")
            print("    Install it via Package Manager before repairing.")
        else:
            by_decl, by_file = build_pp_index(pp_pc)
            guid_to_name = build_assets_guid_to_name(assets_root)
            print(f"  PackageCache shaders: {len(by_decl)} decl-names")
            print(f"  Assets guid->name   : {len(guid_to_name)}")
            print()
            if confirm(f"Repair shader GUIDs in {len(pp_resources)} file(s)?",
                       title="PostProcessResources repair"):
                total_changes = 0
                total_warnings = 0
                for res in pp_resources:
                    try:
                        rel = res.relative_to(project)
                    except ValueError:
                        rel = res
                    changes, warnings = fix_one_pp_resources(
                        res, by_decl, by_file, guid_to_name
                    )
                    if changes:
                        total_changes += len(changes)
                        print(f"  {rel}:")
                        for field, old_g, new_g, strat in changes:
                            print(f"    [CHG] {field:<24} {old_g} -> {new_g}")
                            print(f"          ({strat})")
                    if warnings:
                        total_warnings += len(warnings)
                        print(f"  {rel} unmatched fields:")
                        for field, old_g in warnings:
                            print(f"    {field:<24} guid={old_g}")
                print()
                print(f"[+] Changes: {total_changes}, Warnings: {total_warnings}")
            else:
                print("[i] Skipped PP repair")
        print()

    if pairs:
        print("-" * 78)
        print(f" [6] Rewrite .mat refs + delete {len(pairs)} old Dummy shader(s)")
        print("-" * 78)
        if not confirm(
            f"Rewrite materials and delete {len(pairs)} old Dummy shader(s)?",
            title="Materials + delete",
        ):
            print("[i] Skipped")
            return

        redirect = {}
        for d, c, _ in pairs:
            if d["guid"] and c["guid"]:
                redirect[d["guid"]] = c["guid"]

        mat_total = 0
        mat_changed = 0
        for mp in iter_files(assets_root, (".mat",)):
            text = read_text(mp)
            mat_total += 1
            hits = []

            def repl(m):
                og = m.group(2).lower()
                if og in redirect:
                    hits.append(og)
                    return m.group(1) + redirect[og] + m.group(3)
                return m.group(0)

            new_text = MAT_SHADER_RE.sub(repl, text)
            if hits:
                write_text(mp, new_text)
                mat_changed += 1
                print(f"  + {mp.relative_to(project)}")
                for og in hits:
                    print(f"      {og} -> {redirect[og]}")
        print(f"[+] Materials: {mat_total} scanned, {mat_changed} modified")

        still_ref = {}
        for mp in iter_files(assets_root, (".mat",)):
            for m in MAT_SHADER_RE.finditer(read_text(mp)):
                g = m.group(2).lower()
                if g in redirect:
                    still_ref.setdefault(g, []).append(mp)
        if still_ref:
            print("[!] Residual refs, keeping Dummy files:")
            for g, mps in still_ref.items():
                print(f"    guid={g}")
                for mp in mps:
                    print(f"      {mp.relative_to(project)}")

        deleted = 0
        for d, c, _ in pairs:
            if d["guid"] in still_ref:
                print(f"  skip {d['path'].relative_to(project)} (still referenced)")
                continue
            try:
                d["path"].unlink()
                deleted += 1
                print(f"  del  {d['path'].relative_to(project)}")
            except Exception as e:
                print(f"  [!] Failed {d['path']}: {e}")
                continue
            meta = Path(str(d["path"]) + ".meta")
            if meta.is_file():
                try:
                    meta.unlink()
                    print(f"  del  {meta.relative_to(project)}")
                except Exception as e:
                    print(f"  [!] Failed meta {meta}: {e}")
        print()
        print(f"[+] Deleted {deleted} Dummy shader(s)")

    print()
    print("=" * 78)
    print(" Done")
    print("=" * 78)


if __name__ == "__main__":
    main()