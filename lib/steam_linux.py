#!/usr/bin/env python3
"""Steam/Proton helpers for TS2 Redux Linux Edition.

Stdlib only. Finds Homefront (app 223100), writes launch options, Proton
compat mappings, and a non-Steam TimeSplitters 2 shortcut.
"""

from __future__ import annotations

import argparse
import binascii
import json
import os
import re
import struct
import sys
from pathlib import Path

HOMEFRONT_APPID = 223100
SHORTCUT_NAME = "TimeSplitters 2"
LAUNCH_OPTIONS = "WINEDLLOVERRIDES=dinput8=n,b %command%"
EXE_STOCK_MD5 = "0326ea202fcd7ceb3760d14bc8d07f63"
PAK_STOCK_MD5 = "8a92c275b504c96092d2398a169c0433"
BACKUP_SUFFIX = ".ts2redux.bak"


def parse_binary_vdf(data: bytes, offset: int = 0) -> tuple[dict, int]:
    result: dict = {}
    while offset < len(data):
        type_byte = data[offset]
        if type_byte == 8:
            return result, offset + 1
        offset += 1
        key_end = data.find(b"\x00", offset)
        if key_end == -1:
            raise ValueError("corrupt binary VDF: unterminated key")
        key = data[offset:key_end].decode("utf-8", errors="replace")
        offset = key_end + 1
        if type_byte == 0:
            value, offset = parse_binary_vdf(data, offset)
            result[key] = value
        elif type_byte == 1:
            val_end = data.find(b"\x00", offset)
            if val_end == -1:
                raise ValueError("corrupt binary VDF: unterminated string")
            result[key] = data[offset:val_end].decode("utf-8", errors="replace")
            offset = val_end + 1
        elif type_byte == 2:
            result[key] = struct.unpack_from("<I", data, offset)[0]
            offset += 4
        elif type_byte == 7:
            result[key] = struct.unpack_from("<Q", data, offset)[0]
            offset += 8
        else:
            raise ValueError(f"unsupported binary VDF type {type_byte}")
    return result, offset


def serialize_binary_vdf(payload: dict) -> bytes:
    out = bytearray()
    for key, value in payload.items():
        key_bytes = key.encode("utf-8") + b"\x00"
        if isinstance(value, dict):
            out.append(0)
            out.extend(key_bytes)
            out.extend(serialize_binary_vdf(value))
        elif isinstance(value, str):
            out.append(1)
            out.extend(key_bytes)
            out.extend(value.encode("utf-8") + b"\x00")
        elif isinstance(value, bool):
            raise TypeError("refusing to serialize bool as VDF int")
        elif isinstance(value, int):
            out.append(2)
            out.extend(key_bytes)
            out.extend(struct.pack("<I", value & 0xFFFFFFFF))
        else:
            raise TypeError(f"unsupported VDF value type: {type(value)}")
    out.append(8)
    return bytes(out)


def load_shortcuts(path: Path) -> dict:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    data = path.read_bytes()
    if data[0] != 0:
        raise ValueError(f"{path} is not a binary VDF map")
    key_end = data.find(b"\x00", 1)
    root_key = data[1:key_end].decode("utf-8")
    if root_key != "shortcuts":
        raise ValueError(f"{path} root key is {root_key!r}, expected 'shortcuts'")
    shortcuts, _ = parse_binary_vdf(data, key_end + 1)
    return shortcuts


def save_shortcuts(path: Path, shortcuts: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(serialize_binary_vdf({"shortcuts": shortcuts}))


def shortcut_appid(exe_quoted: str, name: str) -> int:
    key = f"{exe_quoted}{name}".encode("utf-8")
    return (binascii.crc32(key) | 0x80000000) & 0xFFFFFFFF


def find_steam_roots() -> list[Path]:
    home = Path.home()
    candidates = [
        Path(os.environ["STEAM_DIR"]) if os.environ.get("STEAM_DIR") else None,
        home / ".steam" / "root",
        home / ".local" / "share" / "Steam",
        home / ".steam" / "steam",
        home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
    ]
    roots: list[Path] = []
    seen: set[Path] = set()
    for raw in candidates:
        if raw is None:
            continue
        path = raw.expanduser()
        if not path.exists():
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        if (resolved / "userdata").is_dir() or (resolved / "config").is_dir():
            seen.add(resolved)
            roots.append(resolved)
    return roots


def libraryfolders_vdf(steam_root: Path) -> Path | None:
    for candidate in (
        steam_root / "config" / "libraryfolders.vdf",
        steam_root / "steamapps" / "libraryfolders.vdf",
    ):
        if candidate.is_file():
            return candidate
    return None


def parse_libraryfolders(text: str) -> list[dict]:
    libraries: list[dict] = []
    for match in re.finditer(r'"(\d+)"\s*\{(.*?)\n\t\}', text, re.DOTALL):
        body = match.group(2)
        path_m = re.search(r'"path"\s+"([^"]+)"', body)
        if not path_m:
            continue
        cid_m = re.search(r'"contentid"\s+"([^"]+)"', body)
        apps = re.findall(r'"(\d+)"\s+"\d+"', body)
        libraries.append(
            {
                "index": int(match.group(1)),
                "path": path_m.group(1).replace("\\\\", "/"),
                "contentid": cid_m.group(1) if cid_m else "",
                "app_count": len(apps),
                "apps": apps,
            }
        )
    return libraries


def discover_libraries(steam_root: Path) -> list[dict]:
    vdf_path = libraryfolders_vdf(steam_root)
    raw: list[dict] = []
    if vdf_path:
        raw = parse_libraryfolders(vdf_path.read_text(encoding="utf-8", errors="replace"))
    if not raw:
        raw = [{"index": 0, "path": str(steam_root), "contentid": "", "app_count": 0, "apps": []}]
    libraries = []
    for entry in raw:
        path = Path(entry["path"]).expanduser()
        resolved = path.resolve() if path.exists() else path
        libraries.append(
            {
                **entry,
                "path": str(resolved),
                "exists": path.is_dir(),
                "is_root": resolved == steam_root.resolve() or entry["index"] == 0,
            }
        )
    return libraries


def userdata_dirs(steam_root: Path) -> list[Path]:
    users = steam_root / "userdata"
    if not users.is_dir():
        return []
    found = []
    for child in sorted(users.iterdir()):
        if child.is_dir() and child.name.isdigit() and (child / "config").is_dir():
            found.append(child)
    return found


def _compat_tool_name_from_vdf(text: str) -> str | None:
    match = re.search(r'"compat_tools"\s*\{\s*"([^"]+)"', text, re.DOTALL)
    return match.group(1) if match else None


def discover_proton_tools(steam_root: Path) -> list[dict]:
    tools: list[dict] = []
    seen: set[str] = set()

    def add(name: str, source: str, path: str = "") -> None:
        if name in seen:
            return
        seen.add(name)
        tools.append({"name": name, "source": source, "path": path})

    common = steam_root / "steamapps" / "common"
    mapping = (
        ("Proton - Experimental", "proton_experimental"),
        ("Proton 10.0", "proton_10"),
        ("Proton 9.0", "proton_9"),
        ("Proton 8.0", "proton_8"),
        ("Proton Hotfix", "proton_hotfix"),
    )
    if common.is_dir():
        for folder, internal in mapping:
            folder_path = common / folder
            if folder_path.is_dir():
                add(internal, "steam", str(folder_path))

    extra_roots = [
        steam_root / "compatibilitytools.d",
        Path("/usr/share/steam/compatibilitytools.d"),
        Path.home() / ".steam" / "root" / "compatibilitytools.d",
    ]
    for extra in extra_roots:
        if not extra.is_dir():
            continue
        for tool_dir in extra.iterdir():
            vdf_path = tool_dir / "compatibilitytool.vdf"
            if not vdf_path.is_file():
                continue
            name = _compat_tool_name_from_vdf(vdf_path.read_text(errors="replace"))
            if name:
                add(name, "compatibilitytools.d", str(tool_dir))
    return tools


def pick_proton(tools: list[dict], preferred: str | None, variant: str) -> str:
    names = [t["name"] for t in tools]
    if preferred:
        if preferred in names or preferred.startswith("proton"):
            return preferred
        raise SystemExit(f"proton tool {preferred!r} not found. have: {', '.join(names) or 'none'}")
    order = []
    if variant == "cachyos":
        order.extend(n for n in names if "cachyos" in n.lower())
    order.extend(["proton_10", "proton_experimental", "proton_hotfix", "proton_9", "proton_8"])
    for name in order:
        if name in names:
            return name
    return names[0] if names else "proton_experimental"


def parse_acf(text: str) -> dict[str, str]:
    fields = {}
    for key in ("appid", "installdir", "name", "StateFlags"):
        match = re.search(rf'"{key}"\s+"([^"]*)"', text)
        if match:
            fields[key] = match.group(1)
    return fields


def find_homefront(steam_root: Path | None = None) -> dict:
    roots = [steam_root] if steam_root else find_steam_roots()
    libraries: list[dict] = []
    for root in roots:
        libraries.extend(discover_libraries(root))

    hits = []
    for lib in libraries:
        steamapps = Path(lib["path"]) / "steamapps"
        acf = steamapps / f"appmanifest_{HOMEFRONT_APPID}.acf"
        game = None
        source = None
        if acf.is_file():
            fields = parse_acf(acf.read_text(encoding="utf-8", errors="replace"))
            installdir = fields.get("installdir") or "Homefront_The_Revolution"
            candidate = steamapps / "common" / installdir
            if candidate.is_dir():
                game = candidate
                source = "acf"
        if game is None:
            for name in ("Homefront_The_Revolution", "Homefront The Revolution"):
                candidate = steamapps / "common" / name
                if (candidate / "Bin64" / "Homefront2_Release.exe").is_file():
                    game = candidate
                    source = "common"
                    break
        if game is None:
            continue
        exe = game / "Bin64" / "Homefront2_Release.exe"
        pak = game / "gamehf2" / "lsao_cached.pak"
        hits.append(
            {
                "library": lib["path"],
                "game_dir": str(game.resolve()),
                "exe": str(exe) if exe.is_file() else "",
                "pak": str(pak) if pak.is_file() else "",
                "source": source,
                "steam_root": str(roots[0]) if roots else "",
            }
        )
    return {
        "steam_root": str(roots[0]) if roots else "",
        "found": bool(hits),
        "installs": hits,
        "libraries": libraries,
    }


def file_md5(path: Path) -> str:
    import hashlib

    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_file(path: Path) -> None:
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if path.exists() and not backup.exists():
        backup.write_bytes(path.read_bytes())


def _find_named_block(text: str, key: str, start: int = 0) -> tuple[int, int] | None:
    match = re.search(rf'"{re.escape(key)}"\s*\{{', text[start:])
    if not match:
        return None
    brace = start + match.end() - 1
    depth = 0
    i = brace
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return start + match.start(), i + 1
        i += 1
    raise SystemExit(f"unbalanced VDF block for {key}")


def _upsert_compat_block(block: str, appid: str, proton: str) -> str:
    entry_re = re.compile(rf'("{re.escape(appid)}"\s*\{{)(.*?)(\n\s*\}})', re.DOTALL)

    def replace_name(match: re.Match[str]) -> str:
        body = re.sub(r'"name"\s+"[^"]*"', f'"name"\t\t"{proton}"', match.group(2), count=1)
        if '"name"' not in body:
            body = (
                f'\n\t\t\t\t\t"name"\t\t"{proton}"\n'
                f'\t\t\t\t\t"config"\t\t""\n'
                f'\t\t\t\t\t"priority"\t\t"250"' + body
            )
        return f"{match.group(1)}{body}{match.group(3)}"

    updated, count = entry_re.subn(replace_name, block, count=1)
    if count:
        return updated
    entry = (
        f'\n\t\t\t\t\t"{appid}"\n'
        f"\t\t\t\t\t{{\n"
        f'\t\t\t\t\t\t"name"\t\t"{proton}"\n'
        f'\t\t\t\t\t\t"config"\t\t""\n'
        f'\t\t\t\t\t\t"priority"\t\t"250"\n'
        f"\t\t\t\t\t}}"
    )
    brace = block.find("{")
    if brace == -1:
        raise ValueError("CompatToolMapping has no opening brace")
    return block[: brace + 1] + entry + block[brace + 1 :]


def set_compat_mapping(config_path: Path, appid: int, proton: str) -> None:
    if not config_path.is_file():
        raise SystemExit(f"missing Steam config: {config_path}")
    text = config_path.read_text(encoding="utf-8", errors="replace")
    appid_s = str(appid)
    match = re.search(r'"CompatToolMapping"\s*\{', text)
    if not match:
        steam_match = re.search(r'"Steam"\s*\{', text)
        if not steam_match:
            raise SystemExit(f"could not find Steam section in {config_path}")
        insert_at = steam_match.end()
        block = (
            f'\n\t\t\t\t"CompatToolMapping"\n'
            f"\t\t\t\t{{\n"
            f'\t\t\t\t\t"{appid_s}"\n'
            f"\t\t\t\t\t{{\n"
            f'\t\t\t\t\t\t"name"\t\t"{proton}"\n'
            f'\t\t\t\t\t\t"config"\t\t""\n'
            f'\t\t\t\t\t\t"priority"\t\t"250"\n'
            f"\t\t\t\t\t}}\n"
            f"\t\t\t\t}}"
        )
        text = text[:insert_at] + block + text[insert_at:]
    else:
        span = _find_named_block(text, "CompatToolMapping")
        if span is None:
            raise SystemExit("CompatToolMapping block not found")
        start, end = span
        text = text[:start] + _upsert_compat_block(text[start:end], appid_s, proton) + text[end:]
    backup_file(config_path)
    config_path.write_text(text, encoding="utf-8")


def set_launch_options(localconfig: Path, appid: int, options: str) -> None:
    if not localconfig.is_file():
        raise SystemExit(f"missing localconfig: {localconfig}")
    text = localconfig.read_text(encoding="utf-8", errors="replace")
    appid_s = str(appid)
    apps = _find_named_block(text, "apps")
    if apps is None:
        raise SystemExit(f"no apps section in {localconfig}")
    apps_start, apps_end = apps
    apps_block = text[apps_start:apps_end]
    existing = _find_named_block(apps_block, appid_s)
    if existing:
        start, end = existing
        body = apps_block[start:end]
        if re.search(r'"LaunchOptions"\s+"[^"]*"', body):
            body = re.sub(
                r'"LaunchOptions"\s+"[^"]*"',
                f'"LaunchOptions"\t\t"{options}"',
                body,
                count=1,
            )
        else:
            body = re.sub(r"\{", f'{{\n\t\t\t\t\t\t"LaunchOptions"\t\t"{options}"', body, count=1)
        apps_block = apps_block[:start] + body + apps_block[end:]
    else:
        insert = apps_block.find("{") + 1
        entry = (
            f'\n\t\t\t\t\t"{appid_s}"\n'
            f"\t\t\t\t\t{{\n"
            f'\t\t\t\t\t\t"LaunchOptions"\t\t"{options}"\n'
            f"\t\t\t\t\t}}"
        )
        apps_block = apps_block[:insert] + entry + apps_block[insert:]
    text = text[:apps_start] + apps_block + text[apps_end:]
    backup_file(localconfig)
    localconfig.write_text(text, encoding="utf-8")


def read_launch_options(localconfig: Path, appid: int) -> str:
    if not localconfig.is_file():
        return ""
    text = localconfig.read_text(encoding="utf-8", errors="replace")
    apps = _find_named_block(text, "apps")
    if apps is None:
        return ""
    block = text[apps[0] : apps[1]]
    existing = _find_named_block(block, str(appid))
    if existing is None:
        return ""
    match = re.search(r'"LaunchOptions"\s+"([^"]*)"', block[existing[0] : existing[1]])
    return match.group(1) if match else ""


def upsert_shortcut(
    shortcuts: dict,
    *,
    name: str,
    exe: Path,
    start_dir: Path,
    icon: str,
    launch_options: str,
) -> tuple[str, int]:
    exe_quoted = f'"{exe}"'
    appid = shortcut_appid(exe_quoted, name)
    entry = {
        "appid": appid,
        "AppName": name,
        "Exe": exe_quoted,
        "StartDir": f'"{start_dir}/"',
        "icon": icon,
        "ShortcutPath": "",
        "LaunchOptions": launch_options,
        "IsHidden": 0,
        "AllowDesktopConfig": 1,
        "AllowOverlay": 1,
        "OpenVR": 0,
        "Devkit": 0,
        "DevkitGameID": "",
        "DevkitOverrideAppID": 0,
        "LastPlayTime": 0,
        "FlatpakAppID": "",
        "tags": {},
    }
    for key, existing in shortcuts.items():
        if existing.get("AppName") == name:
            existing.update(entry)
            return key, appid
    index = 0
    while str(index) in shortcuts:
        index += 1
    key = str(index)
    shortcuts[key] = entry
    return key, appid


def remove_named(shortcuts: dict, name: str) -> int:
    remove = [key for key, value in shortcuts.items() if value.get("AppName") == name]
    for key in remove:
        del shortcuts[key]
    return len(remove)


def fix_game_cfg(prefix: Path) -> dict:
    candidates = list(prefix.glob("drive_c/users/*/Saved Games/homefront2/game.cfg"))
    changed = []
    for cfg in candidates:
        text = cfg.read_text(encoding="utf-8", errors="replace")
        original = text
        text = text.replace("-- [Game-Configuration]", "[Game-Configuration]")
        if re.search(r"r_supersampling\s*=\s*([0-9.]+)", text):
            text = re.sub(r"r_supersampling\s*=\s*[0-9.]+", "r_supersampling = 1", text, count=1)
        else:
            text += "\nr_supersampling = 1\n"
        text = text.replace("[Game-Configuration]", "-- [Game-Configuration]")
        if text != original:
            backup_file(cfg)
            cfg.write_text(text, encoding="utf-8")
            changed.append(str(cfg))
    return {"changed": changed, "checked": [str(p) for p in candidates]}


def _roots(args: argparse.Namespace) -> list[Path]:
    if args.steam_root:
        return [Path(args.steam_root)]
    roots = find_steam_roots()
    if not roots:
        raise SystemExit("Steam install not found. Set STEAM_DIR or install Steam.")
    return roots


def cmd_find_app(args: argparse.Namespace) -> int:
    root = Path(args.steam_root) if args.steam_root else None
    print(json.dumps(find_homefront(root), indent=2))
    return 0


def cmd_wire(args: argparse.Namespace) -> int:
    exe = Path(args.exe).expanduser().resolve()
    if not exe.is_file():
        raise SystemExit(f"exe not found: {exe}")
    start_dir = Path(args.start_dir).expanduser().resolve() if args.start_dir else exe.parent.parent
    written = []
    for root in _roots(args):
        users = userdata_dirs(root)
        if args.user:
            users = [user for user in users if user.name == str(args.user)]
        if not users:
            continue
        tools = discover_proton_tools(root)
        proton = pick_proton(tools, args.proton, args.variant)
        set_compat_mapping(root / "config" / "config.vdf", HOMEFRONT_APPID, proton)
        for user in users:
            vdf_path = user / "config" / "shortcuts.vdf"
            if vdf_path.exists():
                backup_file(vdf_path)
            shortcuts = load_shortcuts(vdf_path)
            icon = args.icon or str(exe)
            _, appid = upsert_shortcut(
                shortcuts,
                name=args.name,
                exe=exe,
                start_dir=start_dir,
                icon=icon,
                launch_options=args.launch_options,
            )
            save_shortcuts(vdf_path, shortcuts)
            set_compat_mapping(root / "config" / "config.vdf", appid, proton)
            localconfig = user / "config" / "localconfig.vdf"
            if localconfig.is_file():
                set_launch_options(localconfig, HOMEFRONT_APPID, args.launch_options)
            written.append(
                {
                    "steam_root": str(root),
                    "user": user.name,
                    "shortcuts": str(vdf_path),
                    "appid": appid,
                    "proton": proton,
                    "exe": str(exe),
                    "name": args.name,
                    "homefront_appid": HOMEFRONT_APPID,
                }
            )
    if not written:
        raise SystemExit("no Steam userdata profiles found")
    print(json.dumps({"ok": True, "shortcuts": written}, indent=2))
    return 0


def cmd_unwire(args: argparse.Namespace) -> int:
    removed = 0
    for root in _roots(args):
        for user in userdata_dirs(root):
            vdf_path = user / "config" / "shortcuts.vdf"
            if not vdf_path.exists():
                continue
            shortcuts = load_shortcuts(vdf_path)
            count = remove_named(shortcuts, args.name)
            if count:
                save_shortcuts(vdf_path, shortcuts)
                removed += count
    print(json.dumps({"ok": True, "removed": removed}))
    return 0


def cmd_fix_cfg(args: argparse.Namespace) -> int:
    print(json.dumps(fix_game_cfg(Path(args.prefix).expanduser()), indent=2))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    root = Path(args.steam_root) if args.steam_root else None
    info = find_homefront(root)
    install = info["installs"][0] if info["installs"] else {}
    game = Path(install["game_dir"]) if install else None
    report: dict = {
        "steam_root": info["steam_root"],
        "homefront": install,
        "exe_md5": "",
        "exe_md5_ok": False,
        "pak_md5": "",
        "pak_md5_ok": False,
        "dinput8": False,
        "redux_dlls": [],
        "launcher": "",
        "launch_options": [],
        "proton": [],
    }
    if game:
        exe = game / "Bin64" / "Homefront2_Release.exe"
        pak = game / "gamehf2" / "lsao_cached.pak"
        if exe.is_file():
            report["exe_md5"] = file_md5(exe)
            report["exe_md5_ok"] = report["exe_md5"] == EXE_STOCK_MD5
        if pak.is_file():
            report["pak_md5"] = file_md5(pak)
            report["pak_md5_ok"] = report["pak_md5"] == PAK_STOCK_MD5
        report["dinput8"] = (game / "Bin64" / "dinput8.dll").is_file()
        dll_dir = game / "Bin64" / "TS2Redux"
        if dll_dir.is_dir():
            report["redux_dlls"] = sorted(p.name for p in dll_dir.glob("*.dll"))
        for name in ("timesplitters2.exe", "TimeSplitters2.exe"):
            if (game / "Bin64" / name).is_file():
                report["launcher"] = name
                break
    roots = [Path(info["steam_root"])] if info["steam_root"] else []
    for steam in roots:
        report["proton"] = discover_proton_tools(steam)
        for user in userdata_dirs(steam):
            opts = read_launch_options(user / "config" / "localconfig.vdf", HOMEFRONT_APPID)
            report["launch_options"].append({"user": user.name, "options": opts})
    print(json.dumps(report, indent=2))
    return 0 if info["found"] else 2


def cmd_selftest(_args: argparse.Namespace) -> int:
    name = SHORTCUT_NAME
    exe = Path("/tmp/timesplitters2.exe")
    exe_quoted = f'"{exe}"'
    appid = shortcut_appid(exe_quoted, name)
    shortcuts: dict = {}
    upsert_shortcut(
        shortcuts,
        name=name,
        exe=exe,
        start_dir=Path("/tmp/Homefront_The_Revolution"),
        icon=str(exe),
        launch_options=LAUNCH_OPTIONS,
    )
    blob = serialize_binary_vdf({"shortcuts": shortcuts})
    key_end = blob.find(b"\x00", 1)
    parsed, _ = parse_binary_vdf(blob, key_end + 1)
    entry = next(iter(parsed.values()))
    assert entry["AppName"] == name
    assert entry["appid"] == appid
    assert entry["LaunchOptions"] == LAUNCH_OPTIONS

    sample = """
"UserLocalConfigStore"
{
	"Software"
	{
		"valve"
		{
			"Steam"
			{
				"apps"
				{
					"480"
					{
						"LastPlayed"		"1"
					}
				}
			}
		}
	}
}
"""
    tmp = Path("/tmp/ts2redux-localconfig-selftest.vdf")
    tmp.write_text(sample, encoding="utf-8")
    set_launch_options(tmp, HOMEFRONT_APPID, LAUNCH_OPTIONS)
    assert LAUNCH_OPTIONS in tmp.read_text(encoding="utf-8")
    assert read_launch_options(tmp, HOMEFRONT_APPID) == LAUNCH_OPTIONS
    tmp.unlink(missing_ok=True)
    tmp.with_name(tmp.name + BACKUP_SUFFIX).unlink(missing_ok=True)
    print(json.dumps({"ok": True, "appid": appid}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="TS2 Redux Linux Steam helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    find = sub.add_parser("find-app")
    find.add_argument("--steam-root")
    find.set_defaults(func=cmd_find_app)

    wire = sub.add_parser("wire")
    wire.add_argument("--exe", required=True)
    wire.add_argument("--start-dir")
    wire.add_argument("--name", default=SHORTCUT_NAME)
    wire.add_argument("--icon")
    wire.add_argument("--proton")
    wire.add_argument("--variant", default="generic")
    wire.add_argument("--steam-root")
    wire.add_argument("--user")
    wire.add_argument("--launch-options", default=LAUNCH_OPTIONS)
    wire.set_defaults(func=cmd_wire)

    unwire = sub.add_parser("unwire")
    unwire.add_argument("--name", default=SHORTCUT_NAME)
    unwire.add_argument("--steam-root")
    unwire.set_defaults(func=cmd_unwire)

    cfg = sub.add_parser("fix-cfg")
    cfg.add_argument("--prefix", required=True)
    cfg.set_defaults(func=cmd_fix_cfg)

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--steam-root")
    doctor.set_defaults(func=cmd_doctor)

    test = sub.add_parser("selftest")
    test.set_defaults(func=cmd_selftest)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
