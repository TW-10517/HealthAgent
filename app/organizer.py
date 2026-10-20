"""PC Health Agent — File organizer module. Categorizes and moves files into folders."""
import os
import shutil
import json
import urllib.request
from pathlib import Path
from datetime import datetime
from scanner import is_protected, fmt_size

# ── Category definitions ────────────────────────────────────────────────────

CATEGORIES = {
    "Documents": {
        "ext": {"pdf","doc","docx","txt","rtf","odt","epub","md","pages","tex","log"},
        "icon": "📄", "color": "#3B82F6",
    },
    "Spreadsheets": {
        "ext": {"xls","xlsx","csv","ods","tsv","numbers"},
        "icon": "📊", "color": "#34D399",
    },
    "Presentations": {
        "ext": {"ppt","pptx","odp","key"},
        "icon": "📽️", "color": "#F97316",
    },
    "Images": {
        "ext": {"jpg","jpeg","png","gif","bmp","svg","webp","ico","tiff","tif","heic","heif","raw","cr2","nef"},
        "icon": "🖼️", "color": "#A78BFA",
    },
    "Videos": {
        "ext": {"mp4","avi","mkv","mov","wmv","flv","webm","m4v","mpg","mpeg","3gp"},
        "icon": "🎬", "color": "#EC4899",
    },
    "Audio": {
        "ext": {"mp3","wav","flac","aac","ogg","wma","m4a","opus","mid","midi"},
        "icon": "🎵", "color": "#F472B6",
    },
    "Archives": {
        "ext": {"zip","rar","7z","tar","gz","bz2","xz","cab","iso","dmg"},
        "icon": "📦", "color": "#FBBF24",
    },
    "Installers": {
        "ext": {"exe","msi","appx","msix"},
        "icon": "💿", "color": "#FB923C",
    },
    "Code": {
        "ext": {"py","js","ts","jsx","tsx","html","css","scss","java","cpp","c","h","hpp",
                "go","rs","rb","php","sh","bat","ps1","sql","r","swift","kt","dart","vue","svelte"},
        "icon": "💻", "color": "#2DD4BF",
    },
    "Data": {
        "ext": {"json","xml","yaml","yml","toml","ini","cfg","conf","env","properties",
                "db","sqlite","sqlite3","accdb","sql","ndjson","parquet"},
        "icon": "🗃️", "color": "#60A5FA",
    },
    "Fonts": {
        "ext": {"ttf","otf","woff","woff2","eot"},
        "icon": "🔤", "color": "#8B5CF6",
    },
    "Design": {
        "ext": {"psd","ai","fig","sketch","xd","indd","blend","obj","fbx","stl"},
        "icon": "🎨", "color": "#E879F9",
    },
}

_EXT_MAP = {}
for cat_name, cat_info in CATEGORIES.items():
    for ext in cat_info["ext"]:
        _EXT_MAP[ext] = cat_name


def _get_category(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _EXT_MAP.get(ext, "Other")


# ── Scan folder ─────────────────────────────────────────────────────────────

def scan_folder(folder: str, max_files: int = 2000) -> dict:
    folder = os.path.normpath(folder)
    if not os.path.isdir(folder):
        return {"error": "Folder does not exist", "categories": {}}

    if is_protected(folder):
        return {"error": "Protected folder", "categories": {}}

    categories = {}
    total_files = 0
    total_size = 0
    skipped = 0

    try:
        entries = list(os.scandir(folder))
    except (PermissionError, OSError):
        return {"error": "Cannot access folder", "categories": {}}

    for entry in entries:
        if total_files >= max_files:
            break
        if not entry.is_file(follow_symlinks=False):
            continue
        if entry.name.startswith("."):
            skipped += 1
            continue

        try:
            stat = entry.stat()
        except (PermissionError, OSError):
            skipped += 1
            continue

        cat = _get_category(entry.name)
        if cat not in categories:
            categories[cat] = {
                "name": cat,
                "icon": CATEGORIES.get(cat, {}).get("icon", "📁"),
                "color": CATEGORIES.get(cat, {}).get("color", "#5A6478"),
                "files": [],
                "total_size": 0,
                "count": 0,
            }

        categories[cat]["files"].append({
            "name": entry.name,
            "path": entry.path,
            "size": stat.st_size,
            "fmt": fmt_size(stat.st_size),
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d"),
            "ext": entry.name.rsplit(".", 1)[-1].lower() if "." in entry.name else "",
        })
        categories[cat]["total_size"] += stat.st_size
        categories[cat]["count"] += 1
        total_files += 1
        total_size += stat.st_size

    for cat in categories.values():
        cat["total_fmt"] = fmt_size(cat["total_size"])
        cat["files"].sort(key=lambda f: f["size"], reverse=True)

    sorted_cats = dict(sorted(categories.items(), key=lambda x: x[1]["total_size"], reverse=True))

    already_organized = 0
    for entry in entries:
        if entry.is_dir(follow_symlinks=False) and entry.name in CATEGORIES:
            already_organized += 1

    return {
        "folder": folder,
        "categories": sorted_cats,
        "total_files": total_files,
        "total_size": total_size,
        "total_fmt": fmt_size(total_size),
        "category_count": len(sorted_cats),
        "skipped": skipped,
        "already_organized": already_organized,
    }


# ── Move files into category folders ────────────────────────────────────────

def organize_category(folder: str, category: str, file_paths: list) -> dict:
    folder = os.path.normpath(folder)
    if is_protected(folder):
        return {"moved": 0, "failed": 0, "errors": ["Protected folder"]}

    target_dir = os.path.join(folder, category)
    os.makedirs(target_dir, exist_ok=True)

    moved = 0
    failed = 0
    errors = []
    move_log = []

    for fp in file_paths:
        fp = os.path.normpath(fp)
        if not os.path.isfile(fp):
            failed += 1
            errors.append(f"Not found: {os.path.basename(fp)}")
            continue

        if is_protected(fp):
            failed += 1
            errors.append(f"Protected: {os.path.basename(fp)}")
            continue

        if os.path.dirname(fp) == target_dir:
            continue

        dest = os.path.join(target_dir, os.path.basename(fp))

        if os.path.exists(dest):
            base, ext = os.path.splitext(os.path.basename(fp))
            counter = 1
            while os.path.exists(dest):
                dest = os.path.join(target_dir, f"{base} ({counter}){ext}")
                counter += 1

        try:
            shutil.move(fp, dest)
            moved += 1
            move_log.append({"from": fp, "to": dest, "name": os.path.basename(fp)})
        except (PermissionError, OSError) as e:
            failed += 1
            errors.append(f"{os.path.basename(fp)}: {str(e)}")

    log_path = os.path.join(folder, ".organize-log.json")
    try:
        existing_log = []
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8") as f:
                existing_log = json.load(f)
        existing_log.append({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "category": category,
            "moves": move_log,
        })
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(existing_log, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return {
        "moved": moved,
        "failed": failed,
        "errors": errors[:5],
        "target_dir": target_dir,
    }


# ── AI summary of organization plan ────────────────────────────────────────

def get_organize_summary(scan_result: dict, lang: str = "en") -> dict:
    cats = scan_result.get("categories", {})
    if not cats:
        return {"summary": "", "model": ""}

    lines = []
    for name, info in list(cats.items())[:8]:
        lines.append(f"- {name}: {info['count']} files, {info['total_fmt']}")

    prompt_lang = "Japanese" if lang == "ja" else "English"
    prompt = (
        f"You are a file organization assistant. A user's folder has {scan_result['total_files']} files "
        f"({scan_result['total_fmt']}) across these categories:\n"
        + "\n".join(lines) +
        f"\n\nIn {prompt_lang}, write 2-3 short sentences recommending which categories to organize first "
        f"and why. Be specific about the biggest wins. No markdown, no bullets."
    )

    try:
        body = json.dumps({
            "model": "qwen2.5:3b",
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.3, "num_predict": 120},
        }).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return {
            "summary": data.get("response", "").strip(),
            "model": data.get("model", "qwen2.5:3b"),
        }
    except Exception:
        if lang == "ja":
            return {"summary": "最も大きなカテゴリから整理を始めることをお勧めします。", "model": ""}
        return {"summary": "Start with the largest categories to get the most impact.", "model": ""}
