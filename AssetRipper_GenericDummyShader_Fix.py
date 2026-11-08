#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generic Dummy Shader Fix (Assets + PackageCache)

For TMP shaders, In Unity: Window -> TextMeshPro -> Import TMP Essential Resources

What this script does:
  1. Scan shaders under Assets (candidates for deletion + pairing targets)
     and under Library/PackageCache / Packages (pairing targets only).
  2. For every Dummy shader in Assets, find a same-named real (non-Dummy)
     shader elsewhere.
  3. Rewrite every .mat whose m_Shader points at the old Dummy GUID so it
     points at the new real shader GUID (which may live in PackageCache).
  4. Verify no .mat still references an old Dummy GUID.
  5. Delete the old Dummy shader + .meta from Assets (only those with no
     residual refs). PackageCache is never touched.
"""
import os
import re
import shutil
from pathlib import Path

SHADER_GUID_RE = re.compile(r"^guid:\s*([0-9a-fA-F]{32})\s*$", re.MULTILINE)
SHADER_NAME_RE = re.compile(r'^\s*Shader\s+"([^"]+)"', re.MULTILINE)
MAT_SHADER_RE = re.compile(
    r"(m_Shader:\s*\{\s*fileID:\s*-?\d+,\s*guid:\s*)([0-9a-fA-F]{32})(,\s*type:\s*-?\d+\s*\})"
)
SKIP_DIRS = {".git", ".vs", "obj", "Temp", "Logs", "UserSettings", "Build", ".idea"}


def read_text(p):
    return p.read_text(encoding="utf-8", errors="replace")


def write_text(p, t):
    p.write_text(t, encoding="utf-8")


def _tk_root():
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    return root


def pick_folder():
    try:
        from tkinter import filedialog
        root = _tk_root()
        folder = filedialog.askdirectory(
            title="Select project folder (ProjectName / ExportedProject / Assets all OK)",
            initialdir=str(Path.cwd()),
        )
        root.destroy()
        return Path(folder) if folder else None
    except Exception as e:
        print(f"[!] tkinter folder dialog unavailable: {e}")
        return None


def confirm_dialog(message, title="Confirm"):
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
    raise SystemExit(f"[x] Cannot locate project root from: {start}")


def iter_files(root, suffix):
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(suffix):
                yield Path(dirpath) / fn


def parse_guid(meta_path):
    if not meta_path.is_file():
        return None
    m = SHADER_GUID_RE.search(read_text(meta_path))
    return m.group(1).lower() if m else None


def analyze_shader(shader_path, source):
    txt = read_text(shader_path)
    has_cg = "CGPROGRAM" in txt or "CGINCLUDE" in txt
    has_hlsl = "HLSLPROGRAM" in txt or "HLSLINCLUDE" in txt
    has_dummy = "DummyShaderTextExporter" in txt
    is_dummy = has_dummy or (not has_cg and not has_hlsl)
    m = SHADER_NAME_RE.search(txt)
    return {
        "path": shader_path,
        "source": source,
        "size": shader_path.stat().st_size,
        "shader_name": m.group(1) if m else None,
        "is_dummy": is_dummy,
        "guid": parse_guid(Path(str(shader_path) + ".meta")),
    }


def main():
    print("=" * 78)
    print(" Generic Dummy Shader Fix (Assets + PackageCache)")
    print("=" * 78)
    print("Prerequisites:")
    print("  - Unity is closed")
    print("  - Relevant packages are installed (e.g. com.unity.postprocessing)")
    print()
    picked = pick_folder()
    if picked is None:
        print("[x] No folder selected, exiting")
        return

    project = find_project_root(picked)
    assets = project / "Assets"
    print(f"[i] Project: {project}")
    print(f"[i] Assets:  {assets}")
    print()

    # --- Scan: Assets (Dummy candidates + possibly real), PackageCache, Packages ---
    print("-" * 78)
    print(" [1] Scanning shaders")
    print("-" * 78)
    all_shaders = []
    for sp in iter_files(assets, ".shader"):
        all_shaders.append(analyze_shader(sp, "Assets"))
    for root, tag in ((project / "Library" / "PackageCache", "PackageCache"),
                      (project / "Packages", "Packages")):
        if root.is_dir():
            for sp in iter_files(root, ".shader"):
                all_shaders.append(analyze_shader(sp, tag))

    by_source = {}
    for s in all_shaders:
        by_source.setdefault(s["source"], []).append(s)
    for src in sorted(by_source):
        grp = by_source[src]
        d = sum(1 for s in grp if s["is_dummy"])
        print(f"  {src:<14} total={len(grp):<3} dummy={d:<3} real={len(grp)-d}")
    print()

    # --- Index real (non-Dummy) shaders by name ---
    real_by_name = {}
    for s in all_shaders:
        if not s["is_dummy"] and s["shader_name"] and s["guid"]:
            real_by_name.setdefault(s["shader_name"], []).append(s)

    if not real_by_name:
        print("[x] No real (non-Dummy) shader found anywhere, exiting")
        return

    # --- Pair each Assets Dummy with a same-named real shader ---
    print("-" * 78)
    print(" [2] Pairing Assets Dummy -> real shader")
    print("-" * 78)
    matched = []
    unmatched = []
    for old in all_shaders:
        if old["source"] != "Assets":
            continue
        if not old["is_dummy"] or not old["guid"]:
            continue
        name = old["shader_name"]
        cands = real_by_name.get(name, [])
        if not cands:
            unmatched.append(old)
            continue
        # Prefer real shader from PackageCache / Packages (usually the canonical source)
        cands_sorted = sorted(cands, key=lambda c: (c["source"] == "Assets", 0))
        new = cands_sorted[0]
        if len(cands) > 1:
            print(f"[!] Multiple real shaders named '{name}', using {new['source']}")
        matched.append((old, new))

    replace_map = {}
    for old, new in matched:
        replace_map[old["guid"]] = new["guid"]

    print(f"  Matched:    {len(matched)}")
    print(f"  Unmatched:  {len(unmatched)}")
    print()

    if matched:
        print("  Pairs:")
        for old, new in matched:
            print(f"    [OK] {old['shader_name']}")
            try:
                old_rel = old["path"].relative_to(project)
                new_rel = new["path"].relative_to(project)
            except ValueError:
                old_rel, new_rel = old["path"], new["path"]
            print(f"         old {old_rel}  ({old['size']} B, guid={old['guid']})")
            print(f"         new {new_rel}  ({new['size']} B, guid={new['guid']}, {new['source']})")
        print()

    if unmatched:
        # Collapse by namespace for readability
        groups = {}
        for s in unmatched:
            name = s["shader_name"] or "(unnamed)"
            ns = name.rsplit("/", 1)[0] if "/" in name else name
            groups.setdefault(ns, []).append(name)
        print("  Unmatched (kept untouched), grouped by namespace:")
        for ns in sorted(groups):
            print(f"    {ns}  ({len(groups[ns])})")
            for n in groups[ns]:
                print(f"      - {n}")
        print()

    if not matched:
        print("[x] Nothing to replace, exiting")
        return

    # --- Confirmation ---
    print("-" * 78)
    print(f" [3] Will modify .mat refs and delete {len(matched)} old Dummy shader(s)")
    print(f"     PackageCache and Packages are never touched.")
    print("-" * 78)
    if not confirm_dialog(
        f"Proceed?\n\n"
        f"  Match & redirect: {len(matched)}\n"
        f"  Delete old Dummy: {len(matched)}\n"
        f"  Unmatched (keep): {len(unmatched)}",
        title="Generic Dummy Shader Fix",
    ):
        print("[i] Cancelled by user")
        return

    # --- Rewrite materials ---
    print()
    print("-" * 78)
    print(" [4] Rewriting .mat m_Shader refs")
    print("-" * 78)
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

    print(f"[+] Materials scanned: {mat_total}, modified: {mat_changed}")
    for mp, og, ng in changed:
        try:
            rel = mp.relative_to(project)
        except ValueError:
            rel = mp
        print(f"    {rel}")
        print(f"      {og} -> {ng}")

    # --- Verify no residual refs ---
    print()
    print("-" * 78)
    print(" [5] Verifying no residual old-GUID refs")
    print("-" * 78)
    still_ref = {}
    for mp in iter_files(assets, ".mat"):
        for m in MAT_SHADER_RE.finditer(read_text(mp)):
            g = m.group(2).lower()
            if g in replace_map:
                still_ref.setdefault(g, []).append(mp)
    if still_ref:
        print("[!] Still referenced:")
        for g, mps in still_ref.items():
            print(f"    guid={g}")
            for mp in mps:
                try:
                    rel = mp.relative_to(project)
                except ValueError:
                    rel = mp
                print(f"      {rel}")
    else:
        print("[+] No residual references")
    print()

    # --- Delete old Dummy shaders ---
    print("-" * 78)
    print(" [6] Deleting old Dummy shaders from Assets")
    print("-" * 78)
    deleted = 0
    for old, _new in matched:
        if old["guid"] in still_ref:
            try:
                rel = old["path"].relative_to(project)
            except ValueError:
                rel = old["path"]
            print(f"  skip {rel} (still referenced)")
            continue
        sp = old["path"]
        try:
            rel = sp.relative_to(project)
        except ValueError:
            rel = sp
        try:
            sp.unlink()
            deleted += 1
            print(f"  del  {rel}")
        except Exception as e:
            print(f"  [!] Delete failed {rel}: {e}")
            continue
        meta = Path(str(sp) + ".meta")
        if meta.is_file():
            try:
                meta.unlink()
                print(f"  del  {meta.relative_to(project)}")
            except Exception as e:
                print(f"  [!] Delete meta failed {meta}: {e}")

    print()
    print("=" * 78)
    print(f" Done: {mat_changed} material(s) modified, {deleted} shader(s) deleted")
    print("=" * 78)
    print("Next steps:")
    print("  1. Open Unity")
    print("  2. Add a PostProcessing effect (Bloom / AA / Vignette) in a scene")
    print("     and verify the visual effect appears")
    print("  3. If 'Shader name conflict' appears in Console, re-run this script")
    print("  4. Other shaders remain Dummy until their source is added")
    print("     (it auto-matches by shader name)")


if __name__ == "__main__":
    main()