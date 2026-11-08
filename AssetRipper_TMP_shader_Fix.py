#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TMP Shader Redirect Fix
Only fixes TextMeshPro/* shaders. Other Dummy shaders are left untouched.

Prerequisites:
  1. In Unity: Window -> TextMeshPro -> Import TMP Essential Resources
  2. Unity is closed
  3. Run this file from VSCode ("Run Python File")
"""
import os
import re
from pathlib import Path

SHADER_GUID_RE = re.compile(r"^guid:\s*([0-9a-fA-F]{32})\s*$", re.MULTILINE)
SHADER_NAME_RE = re.compile(r'^\s*Shader\s+"([^"]+)"', re.MULTILINE)
MAT_SHADER_RE = re.compile(
    r"(m_Shader:\s*\{\s*fileID:\s*-?\d+,\s*guid:\s*)([0-9a-fA-F]{32})(,\s*type:\s*-?\d+\s*\})"
)
TMP_SETTINGS_FONT_RE = re.compile(
    r"(m_defaultFontAsset:\s*\{\s*fileID:\s*-?\d+,\s*guid:\s*)([0-9a-fA-F]{32})(,\s*type:\s*-?\d+\s*\})"
)

# GUID of built-in LiberationSans SDF shipped with TMP Essential Resources
LIBERATION_GUID = "8f586378b4e144a9851e7b34d9b748ee"
SKIP_DIRS = {".git", ".vs", "obj", "Temp", "Logs", "UserSettings", "Build", ".idea"}


# ---------- Basic helpers ----------

def read_text(p):
    return p.read_text(encoding="utf-8", errors="replace")


def write_text(p, t):
    p.write_text(t, encoding="utf-8")


def _tk_root():
    """Create a hidden topmost Tk root for dialogs."""
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    return root


def pick_folder():
    """Show a folder picker dialog; default directory is the current working dir."""
    try:
        from tkinter import filedialog
        root = _tk_root()
        folder = filedialog.askdirectory(
            title="Select project folder (ProjectName / ExportedProject / Assets are all OK)",
            initialdir=str(Path.cwd()),
        )
        root.destroy()
        return Path(folder) if folder else None
    except Exception as e:
        print(f"[!] tkinter folder dialog unavailable: {e}")
        return None


def confirm_dialog(message, title="Confirm"):
    """Show a yes/no dialog. Falls back to CLI if tkinter is unavailable."""
    try:
        from tkinter import messagebox
        root = _tk_root()
        result = messagebox.askyesno(title, message)
        root.destroy()
        return bool(result)
    except Exception as e:
        print(f"[!] tkinter messagebox unavailable: {e}")
        try:
            return input(f"{message} (y/N): ").strip().lower() == "y"
        except EOFError:
            return False


def find_assets_root(start, max_depth=2):
    """Resolve the Assets directory from any ancestor/descendant of the start path."""
    start = start.resolve()
    cur = start
    while True:
        for sub in (cur / "Assets", cur / "ExportedProject" / "Assets"):
            if sub.is_dir():
                return sub
        if cur.parent == cur:
            break
        cur = cur.parent
    queue = [(start, 0)]
    while queue:
        p, d = queue.pop(0)
        for sub in (p / "Assets", p / "ExportedProject" / "Assets"):
            if sub.is_dir():
                return sub
        if d < max_depth:
            for child in p.iterdir():
                if child.is_dir() and child.name not in SKIP_DIRS:
                    queue.append((child, d + 1))
    raise SystemExit(f"[x] Cannot find Assets directory from: {start}")


def parse_guid(meta_path):
    if not meta_path.is_file():
        return None
    m = SHADER_GUID_RE.search(read_text(meta_path))
    return m.group(1).lower() if m else None


def analyze_shader(shader_path):
    """Classify a shader as Dummy or real based on program block presence."""
    txt = read_text(shader_path)
    has_cg = "CGPROGRAM" in txt or "CGINCLUDE" in txt
    has_hlsl = "HLSLPROGRAM" in txt or "HLSLINCLUDE" in txt
    has_dummy = "DummyShaderTextExporter" in txt
    is_dummy = has_dummy or (not has_cg and not has_hlsl)
    m = SHADER_NAME_RE.search(txt)
    return {
        "path": shader_path,
        "size": shader_path.stat().st_size,
        "shader_name": m.group(1) if m else None,
        "is_dummy": is_dummy,
        "guid": parse_guid(Path(str(shader_path) + ".meta")),
    }


def iter_files(root, suffix):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(suffix):
                yield Path(dirpath) / fn


# ---------- Main ----------

def main():
    print("=" * 72)
    print(" TMP Shader Redirect Fix (only TextMeshPro/* is touched)")
    print("=" * 72)
    print("Prerequisites:")
    print("  1. TMP Essential Resources already imported in Unity")
    print("  2. Unity is closed")
    print()
    print(f"[i] Default path: {Path.cwd()}")
    picked = pick_folder()
    if picked is None:
        print("[x] No folder selected, exiting")
        return

    assets = find_assets_root(picked)
    project = assets.parent
    print(f"[i] Assets:  {assets}")
    print(f"[i] Project: {project}")
    print()

    # 1. Scan all shaders under Assets
    shaders = [analyze_shader(p) for p in iter_files(assets, ".shader")]
    print(f"[i] Scanned .shader files: {len(shaders)}")

    # 2. Build an index of real (non-Dummy) shaders keyed by declared shader name
    new_by_name = {}
    for s in shaders:
        if not s["is_dummy"] and s["shader_name"] and s["guid"]:
            new_by_name.setdefault(s["shader_name"], []).append(s)

    if not new_by_name:
        print()
        print("[x] No real (non-Dummy) shader found")
        print("[x] Import TMP Essential Resources in Unity first, then re-run")
        return

    print(f"[i] Real shaders found: {len(new_by_name)}")
    for name in sorted(new_by_name):
        for s in new_by_name[name]:
            rel = s["path"].relative_to(project)
            print(f"      {name}  ({s['size']} B)  {rel}")
    print()

    # 3. Pair each old Dummy with a same-named real shader
    replace_map = {}
    matched = []
    unmatched = []
    for old in shaders:
        if not old["is_dummy"] or not old["guid"]:
            continue
        name = old["shader_name"]
        cands = [c for c in new_by_name.get(name, []) if c["guid"] != old["guid"]]
        if not cands:
            unmatched.append(old)
            continue
        if len(cands) > 1:
            print(f"[!] Multiple real shaders named '{name}', using the first one")
        new = cands[0]
        replace_map[old["guid"]] = new["guid"]
        matched.append((old, new))

    print("=" * 72)
    print(" Pairing result")
    print("=" * 72)
    print(f"  Replaceable (TMP):   {len(matched)}")
    print(f"  Unmatched (kept):    {len(unmatched)}")
    print()
    for old, new in matched:
        print(f"  [OK] {old['shader_name']}")
        print(f"       old {old['path'].relative_to(project)}  ({old['size']} B, guid={old['guid']})")
        print(f"       new {new['path'].relative_to(project)}  ({new['size']} B, guid={new['guid']})")
    if unmatched:
        print()
        print("  [--] Unmatched (kept untouched):")
        for old in unmatched:
            print(f"       {old['shader_name'] or '(unnamed)'}  ({old['size']} B)")
            print(f"       {old['path'].relative_to(project)}  guid={old['guid']}")

    if not replace_map:
        print()
        print("[x] Nothing to replace, exiting")
        return

    # 4. Report TMP Settings default font state
    print()
    print("=" * 72)
    print(" TMP Settings default font check")
    print("=" * 72)
    for sp in iter_files(assets, ".asset"):
        if sp.name != "TMP Settings.asset":
            continue
        txt = read_text(sp)
        m = TMP_SETTINGS_FONT_RE.search(txt)
        if not m:
            continue
        g = m.group(2).lower()
        print(f"  {sp.relative_to(project)}")
        print(f"    m_defaultFontAsset = {g}")
        if g == LIBERATION_GUID:
            print("    [!] Points to built-in LiberationSans SDF")
            print("    [!] If the project used another font, change it back in TMP Settings")
            print("    FontAsset candidates (contain m_GlyphTable):")
            for fp in iter_files(assets, ".asset"):
                if fp == sp:
                    continue
                ftxt = read_text(fp)
                if "m_GlyphTable:" not in ftxt:
                    continue
                fg = parse_guid(Path(str(fp) + ".meta"))
                print(f"      {fp.relative_to(project)}  guid={fg or '?'}")

    # 5. Final confirmation (GUI dialog)
    print()
    print("=" * 72)
    print(f" Will redirect material refs and delete {len(matched)} old TMP Dummy shaders")
    print(f" (other {len(unmatched)} Dummy shaders are kept untouched)")
    print("=" * 72)
    if not confirm_dialog(
        f"Redirect material shader refs and delete {len(matched)} old TMP Dummy shader(s)?\n\n"
        f"({len(unmatched)} other Dummy shader(s) will be kept untouched.)",
        title="TMP Shader Fix",
    ):
        print("[i] Cancelled by user")
        return

    # 6. Rewrite materials
    print()
    print("=" * 72)
    print(" Updating materials")
    print("=" * 72)
    mat_total = 0
    mat_changed = 0
    changed = []
    for mp in iter_files(assets, ".mat"):
        txt = read_text(mp)
        mat_total += 1
        hits = []

        def repl(m):
            old_g = m.group(2).lower()
            if old_g in replace_map:
                hits.append((old_g, replace_map[old_g]))
                return m.group(1) + replace_map[old_g] + m.group(3)
            return m.group(0)

        new_txt = MAT_SHADER_RE.sub(repl, txt)
        if hits:
            write_text(mp, new_txt)
            mat_changed += 1
            for og, ng in hits:
                changed.append((mp, og, ng))
    print(f"[+] Materials: scanned {mat_total}, modified {mat_changed}")
    for mp, og, ng in changed:
        print(f"    {mp.relative_to(project)}")
        print(f"      {og} -> {ng}")

    # 7. Verify no residual references to old GUIDs
    print()
    print("=" * 72)
    print(" Verify residual references")
    print("=" * 72)
    still_ref = {}
    for mp in iter_files(assets, ".mat"):
        for m in MAT_SHADER_RE.finditer(read_text(mp)):
            g = m.group(2).lower()
            if g in replace_map:
                still_ref.setdefault(g, []).append(mp)
    if still_ref:
        print("[!] Old GUIDs still referenced, those shaders will not be deleted:")
        for g, mps in still_ref.items():
            print(f"    guid={g}")
            for mp in mps:
                print(f"      {mp.relative_to(project)}")
    else:
        print("[+] No residual references")

    # 8. Delete replaced Dummy shaders
    print()
    print("=" * 72)
    print(" Deleting old TMP Dummy shaders")
    print("=" * 72)
    deleted = 0
    for old, _new in matched:
        if old["guid"] in still_ref:
            print(f"  skip {old['path'].relative_to(project)} (still referenced)")
            continue
        sp = old["path"]
        try:
            sp.unlink()
            deleted += 1
            print(f"  del  {sp.relative_to(project)}")
        except Exception as e:
            print(f"  [!] Failed to delete {sp}: {e}")
            continue
        meta = Path(str(sp) + ".meta")
        if meta.is_file():
            try:
                meta.unlink()
                print(f"  del  {meta.relative_to(project)}")
            except Exception as e:
                print(f"  [!] Failed to delete meta {meta}: {e}")

    print()
    print("=" * 72)
    print(f" Done: {mat_changed} material(s) modified, {deleted} shader(s) deleted")
    print("=" * 72)
    print("Next steps:")
    print("  1. Open Unity")
    print("  2. Check TMP text renders correctly (outline/shadow/face color should work)")
    print("  3. If Console shows 'Shader name conflict', re-run this script")
    print("  4. If TMP default font became LiberationSans, fix it in TMP Settings")
    print("  5. Other Shaders stay Dummy")


if __name__ == "__main__":
    main()