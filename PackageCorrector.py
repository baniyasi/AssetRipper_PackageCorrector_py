#!/usr/bin/env python3
"""
PackageCorrector - Fix broken script references in AssetRipper-exported Unity projects.

Two correction passes:

  Pass 1 (meta-based, original PackageCorrector logic):
      Matches .meta files by filename between Assets/Scripts and the real
      package cache, then replaces old GUIDs with the correct ones in all
      text assets (.unity / .prefab / .asset). .meta files are never touched,
      so no resource identity is ever accidentally rewritten.

  Pass 2 (DLL-based, new logic):
      For each DLL in Assets/Plugins:
        - read the DLL's GUID from its .meta
        - scan the project for m_Script references using that GUID
        - parse the DLL, compute each referenced class's fileID via Unity's
          MD4-based algorithm, and resolve the class's full name
        - find the same full class name in the source packages (PackageCache
          or Packages) and read the corresponding .cs.meta GUID
        - rewrite {fileID: <dll-class-id>, guid: <dll-guid>} to
          {fileID: 11500000, guid: <source-cs-guid>}

Requirements:
    pip install dnfile pycryptodome
"""

import os
import re
import sys
import shutil
import struct

# ---------- dependency check ----------
try:
    from Crypto.Hash import MD4
except ImportError:
    print("Missing dependency: pycryptodome. Install with: pip install pycryptodome")
    sys.exit(1)

try:
    import dnfile
except ImportError:
    print("Missing dependency: dnfile. Install with: pip install dnfile")
    sys.exit(1)


# ---------- constants ----------
SOURCE_SCRIPT_FILEID = 11500000  # Unity's fixed local file ID for source MonoScripts
TEXT_ASSET_EXTS = ('.unity', '.prefab', '.asset')

SCRIPT_REF_RE = re.compile(
    r'm_Script:\s*\{fileID:\s*(-?\d+),\s*guid:\s*([0-9a-fA-F]{32})'
    r'(?:\s*,\s*type:\s*(\d+))?\}'
)

NS_BLOCK_RE = re.compile(r'^\s*namespace\s+([\w\.]+)\s*\{', re.MULTILINE)
NS_FILE_RE  = re.compile(r'^\s*namespace\s+([\w\.]+)\s*;', re.MULTILINE)
CLASS_RE = re.compile(
    r'\b(?:public\s+|internal\s+|protected\s+|private\s+)?'
    r'(?:static\s+|sealed\s+|abstract\s+|partial\s+)*'
    r'class\s+(\w+)',
    re.MULTILINE
)


# ---------- helpers ----------
def read_guid_from_meta(meta_path):
    """Return the 32-char GUID from a .meta file, or None."""
    try:
        with open(meta_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except OSError:
        return None
    m = re.search(r'^guid:\s*([0-9a-fA-F]{32})\s*$', content, re.MULTILINE)
    return m.group(1).lower() if m else None


def unity_fileid(namespace, name):
    """Unity's DLL MonoScript fileID: MD4 of b's\\0\\0\\0' + namespace + class."""
    full = f"{namespace}{name}" if namespace else name
    data = b"s\x00\x00\x00" + full.encode('utf-8')
    h = MD4.new()
    h.update(data)
    return struct.unpack('<i', h.digest()[:4])[0]


def iter_files(root, exts=None):
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            if exts and not name.lower().endswith(exts):
                continue
            yield os.path.join(dirpath, name)


def parse_cs_file(cs_path):
    """Return (namespace, [class names]) parsed from a .cs file."""
    try:
        with open(cs_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except OSError:
        return None, []
    ns_match = NS_FILE_RE.search(content) or NS_BLOCK_RE.search(content)
    namespace = ns_match.group(1) if ns_match else ''
    classes = CLASS_RE.findall(content)
    return namespace, classes


def scan_package_sources(search_roots):
    """
    Build: full class name -> (cs_path, source_guid, is_primary).
    is_primary is True when the class name equals the .cs filename (the class
    Unity treats as that file's MonoScript).
    """
    result = {}
    for root in search_roots:
        if not os.path.isdir(root):
            continue
        for cs_path in iter_files(root, ('.cs',)):
            meta_path = cs_path + '.meta'
            if not os.path.exists(meta_path):
                continue
            guid = read_guid_from_meta(meta_path)
            if not guid:
                continue
            namespace, classes = parse_cs_file(cs_path)
            if namespace is None:
                continue
            stem = os.path.splitext(os.path.basename(cs_path))[0]
            for cls in classes:
                full = f"{namespace}.{cls}" if namespace else cls
                if full in result:
                    continue  # first match wins
                result[full] = (cs_path, guid, cls == stem)
    return result


def backup_file(abs_path, project_root, backup_root):
    rel = os.path.relpath(abs_path, project_root)
    dst = os.path.join(backup_root, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if not os.path.exists(dst):
        shutil.copy2(abs_path, dst)


def replace_script_refs(assets_dir, mapping, project_root, backup_root, label):
    """
    mapping: dict (file_id_str, dll_guid_lower) -> (new_file_id_str, new_guid_lower)
    Only .unity/.prefab/.asset files are scanned. .meta files are never touched.
    """
    modified = 0
    total_refs = 0
    for path in iter_files(assets_dir, TEXT_ASSET_EXTS):
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
        except OSError:
            continue

        local = [0]

        def _sub(m):
            fid, gid, typ = m.group(1), m.group(2).lower(), m.group(3)
            key = (fid, gid)
            if key in mapping:
                new_fid, new_gid = mapping[key]
                local[0] += 1
                suffix = f", type: {typ}" if typ is not None else ""
                return f"m_Script: {{fileID: {new_fid}, guid: {new_gid}{suffix}}}"
            return m.group(0)

        new_content = SCRIPT_REF_RE.sub(_sub, content)
        if local[0] > 0:
            backup_file(path, project_root, backup_root)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            modified += 1
            total_refs += local[0]
            print(f"  [{label}] {os.path.relpath(path, project_root)}: {local[0]} refs")
    return modified, total_refs


# ---------- Pass 1: meta-based correction ----------
def run_meta_corrector(project_root, assets_dir, source_roots, backup_root):
    scripts_dir = os.path.join(assets_dir, 'Scripts')
    if not os.path.isdir(scripts_dir):
        print("[meta] Assets/Scripts not found, skipping meta-based correction.")
        return

    print("[meta] Building GUID map from Assets/Scripts -> source packages ...")
    incorrect = {}  # filename -> guid
    for meta in iter_files(scripts_dir, ('.meta',)):
        g = read_guid_from_meta(meta)
        if g:
            incorrect[os.path.basename(meta)] = g

    if not incorrect:
        print("[meta] No .meta files found in Assets/Scripts.")
        return

    guid_map = {}
    for root in source_roots:
        for meta in iter_files(root, ('.meta',)):
            name = os.path.basename(meta)
            if name in incorrect:
                new_g = read_guid_from_meta(meta)
                if new_g and new_g != incorrect[name]:
                    guid_map[incorrect[name]] = new_g

    if not guid_map:
        print("[meta] No matching .meta filenames between Assets/Scripts and source packages.")
        return

    print(f"[meta] Mapped {len(guid_map)} GUIDs. Replacing in assets ...")

    modified = 0
    total_refs = 0
    for path in iter_files(assets_dir, TEXT_ASSET_EXTS):
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
        except OSError:
            continue
        new_content = content
        n = 0
        for old_g, new_g in guid_map.items():
            if old_g in new_content:
                n += new_content.count(old_g)
                new_content = new_content.replace(old_g, new_g)
        if n > 0:
            backup_file(path, project_root, backup_root)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            modified += 1
            total_refs += n
            print(f"  [meta] {os.path.relpath(path, project_root)}: {n} refs")
    print(f"[meta] Done. Modified {modified} files, replaced {total_refs} refs.")


# ---------- Pass 2: DLL-based correction ----------
def resolve_dll_paths(dll_names, plugins_dirs):
    resolved = []
    for name in dll_names:
        if not name.lower().endswith('.dll'):
            name = name + '.dll'
        found = None
        for pd in plugins_dirs:
            candidate = os.path.join(pd, name)
            if os.path.exists(candidate):
                found = candidate
                break
        if not found:
            print(f"[dll] Not found: {name} (searched: {plugins_dirs})")
            continue
        resolved.append(found)
    return resolved


def collect_referenced_fileids(assets_dir, dll_guid):
    fids = set()
    for path in iter_files(assets_dir, TEXT_ASSET_EXTS):
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
        except OSError:
            continue
        for m in SCRIPT_REF_RE.finditer(content):
            if m.group(2).lower() == dll_guid:
                fids.add(m.group(1))
    return fids


def process_dll(dll_path, assets_dir, source_index, project_root, backup_root):
    dll_guid = read_guid_from_meta(dll_path + '.meta')
    if not dll_guid:
        print(f"[dll] {os.path.basename(dll_path)}: .meta not found or has no GUID, skipping.")
        return None, None, None

    print(f"\n[dll] Processing {os.path.basename(dll_path)}")
    print(f"[dll] GUID = {dll_guid}")

    ref_fids = collect_referenced_fileids(assets_dir, dll_guid)
    if not ref_fids:
        print("[dll] No references to this DLL found in assets.")
        return dll_guid, {}, {"modified": 0, "refs": 0}

    print(f"[dll] {len(ref_fids)} distinct fileIDs referenced in assets.")

    # ---- parse DLL, always close the handle ----
    fid_to_name = {}
    pe = None
    try:
        pe = dnfile.dnPE(dll_path)
        for t in pe.net.mdtables.TypeDef:
            ns = str(t.TypeNamespace) if t.TypeNamespace else ''
            name = str(t.TypeName)
            fid = unity_fileid(ns, name)
            full = f"{ns}.{name}" if ns else name
            fid_to_name[fid] = full
    except Exception as e:
        print(f"[dll] Failed to parse: {e}")
        return dll_guid, {}, {"modified": 0, "refs": 0}
    finally:
        if pe is not None:
            try:
                pe.close()
            except Exception:
                pass

    mapping = {}
    missing = []
    non_primary = []
    for fid_str in sorted(ref_fids, key=int):
        fid = int(fid_str)
        full = fid_to_name.get(fid)
        if not full:
            missing.append((fid_str, "<not in DLL>"))
            continue
        entry = source_index.get(full)
        if not entry:
            missing.append((fid_str, full))
            continue
        cs_path, src_guid, is_primary = entry
        if not is_primary:
            non_primary.append((fid_str, full, cs_path))
        mapping[(fid_str, dll_guid)] = (str(SOURCE_SCRIPT_FILEID), src_guid)

    if missing:
        print(f"[dll] {len(missing)} fileIDs could not be resolved to a source script:")
        for fid_str, info in missing:
            print(f"       fileID {fid_str}: {info}")
    if non_primary:
        print(f"[dll] {len(non_primary)} fileIDs map to a non-primary class of their .cs file."
              f" Unity may not load these as components:")
        for fid_str, full, cs_path in non_primary:
            print(f"       fileID {fid_str}: {full}  (in {os.path.basename(cs_path)})")

    if not mapping:
        print("[dll] No replacements to apply for this DLL.")
        return dll_guid, mapping, {"modified": 0, "refs": 0}

    print(f"[dll] Applying {len(mapping)} replacements ...")
    modified, refs = replace_script_refs(
        assets_dir, mapping, project_root, backup_root, label=os.path.basename(dll_path)
    )
    return dll_guid, mapping, {"modified": modified, "refs": refs}


# ---------- main ----------
def main():
    project_name = input("Project folder (parent of ExportedProject): ").strip().strip('"')
    if os.path.basename(os.path.normpath(project_name)) == 'ExportedProject':
        exported_root = project_name
    else:
        exported_root = os.path.join(project_name, 'ExportedProject')

    assets_dir = os.path.join(exported_root, 'Assets')
    library_dir = os.path.join(exported_root, 'Library')
    package_cache = os.path.join(library_dir, 'PackageCache')
    packages_dir = os.path.join(exported_root, 'Packages')

    if not os.path.isdir(assets_dir):
        print(f"Assets folder not found: {assets_dir}")
        sys.exit(1)

    # Standard location is Assets/Plugins; fall back to ExportedProject/Plugins.
    plugins_dirs = [
        os.path.join(assets_dir, 'Plugins'),
        os.path.join(exported_root, 'Plugins'),
    ]

    backup_root = os.path.join(exported_root, '_backup_guid_fix')
    os.makedirs(backup_root, exist_ok=True)

    source_roots = [p for p in (package_cache, packages_dir) if os.path.isdir(p)]
    if not source_roots:
        print("Neither Library/PackageCache nor Packages exists. Cannot resolve source GUIDs.")
        sys.exit(1)

    # ---- Pass 1: meta-based ----
    print("=" * 60)
    print("Pass 1: meta-based GUID correction")
    print("=" * 60)
    run_meta_corrector(exported_root, assets_dir, source_roots, backup_root)

    # ---- Pass 2: DLL-based ----
    print("\n" + "=" * 60)
    print("Pass 2: DLL script reference correction")
    print("=" * 60)

    dll_input = input("DLL names (space-separated, e.g. 'UnityEngine.UI TextMeshPro'): ").strip()
    dll_names = dll_input.split()
    if not dll_names:
        print("No DLLs specified, skipping DLL correction.")
        return

    dll_paths = resolve_dll_paths(dll_names, plugins_dirs)
    if not dll_paths:
        print("None of the specified DLLs were found.")
        return

    print(f"\nBuilding source index from {source_roots} ...")
    source_index = scan_package_sources(source_roots)
    print(f"Indexed {len(source_index)} classes from source packages.")

    results = []
    for dll_path in dll_paths:
        dll_guid, mapping, stats = process_dll(
            dll_path, assets_dir, source_index, exported_root, backup_root
        )
        if stats is not None:
            results.append((dll_path, dll_guid, stats))

    # ---- summary ----
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    total_files = sum(s["modified"] for _, _, s in results)
    total_refs = sum(s["refs"] for _, _, s in results)
    for dll_path, _, stats in results:
        print(f"  {os.path.basename(dll_path)}: {stats['modified']} files, {stats['refs']} refs")
    print(f"  Total: {total_files} files modified, {total_refs} references replaced")
    print(f"  Backups: {backup_root}")

    # ---- optional DLL deletion ----
    if results:
        print("\nThe DLLs are no longer needed after this correction.")
        delete = input("Delete the DLLs and their .meta files? (y/n): ").strip().lower()
        if delete == 'y':
            for dll_path, _, _ in results:
                for p in (dll_path, dll_path + '.meta'):
                    if not os.path.exists(p):
                        continue
                    try:
                        os.remove(p)
                        print(f"  Deleted: {os.path.relpath(p, exported_root)}")
                    except PermissionError:
                        print(f"  [locked] Cannot delete {p} - close Unity / other processes and retry.")
        else:
            print("  DLLs kept. Unity will still load them until you delete or move them.")


if __name__ == '__main__':
    main()