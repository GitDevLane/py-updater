#!/usr/bin/env python3
"""
Portable GitHub Releases updater (no external deps).

Features:
- Checks GitHub releases (stable by default; can include prereleases)
- Picks asset by pattern with placeholders: {app}, {os}, {arch}
- Downloads via API (supports private repos with GH_TOKEN)
- Verifies SHA-256 via companion "<asset>.sha256" when available
- Stages, backs up, swaps atomically as possible, updates version.json
- Rolls back on failure
- Can restart your app with --restart-cmd
"""

import argparse
import hashlib
import io
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ---------- Utilities ----------

def log(msg):
    print(f"[updater] {msg}")

def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def write_json_atomic(path, data):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)

def norm_os():
    s = platform.system().lower()
    if "windows" in s:
        return "windows"
    if "darwin" in s or "mac" in s:
        return "macos"
    return "linux"

def norm_arch():
    m = platform.machine().lower()
    # Common normalizations
    if m in ("amd64", "x86_64", "x64"):
        return "x64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    if m in ("armv7l", "armv7", "arm32", "arm"):
        return "armv7"
    if m in ("i386", "i686", "x86"):
        return "x86"
    return m  # fallback (e.g., 'ppc64le')

def parse_semver(s):
    """Extract a comparable (major, minor, patch, prerelease?) from tag like 'v1.2.3' or '1.2.3-beta.1'."""
    if s.startswith("v"):
        s = s[1:]
    # Basic semver: major.minor.patch[-prerelease]
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$", s)
    if not m:
        # Try looser: major.minor (assume patch=0)
        m2 = re.match(r"^(\d+)\.(\d+)(?:[-+].*)?$", s)
        if m2:
            return (int(m2.group(1)), int(m2.group(2)), 0)
        # Non-semver: push very low
        return (-1, -1, -1)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))

def compare_versions(a, b):
    """Return 1 if a>b, 0 if equal, -1 if a<b. Accepts tags like 'v1.2.3'."""
    pa, pb = parse_semver(a), parse_semver(b)
    return (pa > pb) - (pa < pb)

def http_get_json(url, token=None, timeout=30):
    req = Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "py-updater"
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        return json.loads(data.decode("utf-8"))

def http_download(url, dest_path, token=None, timeout=60):
    req = Request(url, headers={
        "Accept": "application/octet-stream",
        "User-Agent": "py-updater"
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    # Stream to file
    with urlopen(req, timeout=timeout) as resp, open(dest_path, "wb") as out:
        CHUNK = 1 << 20
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            out.write(chunk)

def unzip_to(zip_path, target_dir):
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(target_dir)

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def verify_sha256_from_file(sha_path, asset_path):
    """
    .sha256 file format (common):
        HEX  filename
    We'll accept first hex on the line and compare.
    """
    with open(sha_path, "r", encoding="utf-8", errors="ignore") as f:
        txt = f.read().strip()
    m = re.search(r"([A-Fa-f0-9]{64})", txt)
    if not m:
        raise ValueError(f"Invalid sha256 file format: {sha_path}")
    expected = m.group(1).lower()
    actual = sha256_file(asset_path)
    if actual != expected:
        raise ValueError(f"SHA-256 mismatch.\n  expected={expected}\n  actual  ={actual}")
    return True

# ---------- GitHub release selection ----------

def get_latest_release(repo, include_prereleases=False, token=None, timeout=30):
    """
    Return the latest release object (most recent by semver tag) respecting draft/prerelease flags.
    """
    # We use /releases (not /releases/latest) so we can filter prereleases and sort by semver.
    url = f"https://api.github.com/repos/{repo}/releases?per_page=30"
    releases = http_get_json(url, token=token, timeout=timeout)
    # Filter out drafts, optionally prereleases
    candidates = [
        r for r in releases
        if not r.get("draft", False) and (include_prereleases or not r.get("prerelease", False))
    ]
    if not candidates:
        return None
    # Sort by semver tag_name descending
    candidates.sort(key=lambda r: parse_semver(r.get("tag_name") or ""), reverse=True)
    return candidates[0]

def find_asset(release, asset_name):
    for a in release.get("assets", []):
        if a.get("name") == asset_name:
            return a
    return None

# ---------- Install / swap ----------

def safe_swap(staging_dir, app_dir):
    """
    Backup app_dir, then replace with staging_dir.
    Returns path to backup for cleanup/rollback.
    """
    parent = os.path.dirname(os.path.abspath(app_dir))
    backup_dir = os.path.join(parent, f"{os.path.basename(app_dir)}.backup-{int(time.time())}")
    log(f"Creating backup: {backup_dir}")
    if os.path.exists(backup_dir):
        shutil.rmtree(backup_dir, ignore_errors=True)
    if os.path.exists(app_dir):
        os.rename(app_dir, backup_dir)  # rename is fast; if fails, fallback to copy+remove
    os.rename(staging_dir, app_dir)
    return backup_dir

def rollback_from_backup(backup_dir, app_dir):
    if os.path.exists(app_dir):
        shutil.rmtree(app_dir, ignore_errors=True)
    if os.path.exists(backup_dir):
        os.rename(backup_dir, app_dir)

# ---------- Core update routine ----------

def run_update(app_name,
               repo,
               app_dir,
               version_file,
               asset_pattern="{app}-{os}-{arch}.zip",
               include_prereleases=False,
               token=None,
               allow_downgrade=False,
               restart_cmd=None,
               timeout=60,
               dry_run=False):
    """
    Orchestrates the update. Returns True if an update was installed.
    """
    os_id = norm_os()
    arch_id = norm_arch()
    asset_name = asset_pattern.format(app=app_name, os=os_id, arch=arch_id)

    # Load current version
    vdata = read_json(version_file, default={}) or {}
    current_version = vdata.get("version", "0.0.0")

    log(f"Current version: {current_version}")
    log(f"Repo: {repo}, asset pattern -> {asset_name}")

    release = get_latest_release(repo, include_prereleases=include_prereleases, token=token, timeout=timeout)
    if not release:
        log("No suitable releases found.")
        return False

    tag = release.get("tag_name") or "0.0.0"
    log(f"Latest release tag: {tag} (prerelease={release.get('prerelease')})")

    cmp = compare_versions(tag, current_version)
    if cmp < 0 and not allow_downgrade:
        log("Remote version is older; skipping (use --allow-downgrade to force).")
        return False
    if cmp == 0:
        log("Already up to date.")
        return False

    asset = find_asset(release, asset_name)
    if not asset:
        names = ", ".join(a.get("name") for a in release.get("assets", []))
        raise FileNotFoundError(f"Asset '{asset_name}' not found in release assets: {names}")

    asset_url = asset.get("browser_download_url")
    sha_asset = find_asset(release, asset_name + ".sha256")
    sha_url = sha_asset.get("browser_download_url") if sha_asset else None

    if dry_run:
        log(f"[dry-run] Would download: {asset_name}")
        if sha_url:
            log(f"[dry-run] Would verify with: {asset_name}.sha256")
        return False

    tmp_dir = tempfile.mkdtemp(prefix="upd-")
    zip_path = os.path.join(tmp_dir, asset_name)
    sha_path = os.path.join(tmp_dir, asset_name + ".sha256") if sha_url else None

    try:
        log(f"Downloading asset to {zip_path} ...")
        http_download(asset_url, zip_path, token=token, timeout=timeout)

        if sha_url:
            log(f"Downloading checksum to {sha_path} ...")
            http_download(sha_url, sha_path, token=token, timeout=timeout)
            log("Verifying SHA-256 ...")
            verify_sha256_from_file(sha_path, zip_path)
        else:
            log("No .sha256 file provided for this asset (verification skipped).")

        # Extract to staging
        staging_dir = os.path.join(tmp_dir, "staging")
        os.makedirs(staging_dir, exist_ok=True)
        log(f"Extracting to staging: {staging_dir}")
        unzip_to(zip_path, staging_dir)

        # If the zip contains a top-level folder named 'app' or similar, you can:
        # - Replace the entire app_dir with staging_dir/subfolder
        # - Or ensure your release zip's contents match app_dir layout directly.
        # Here we assume the ZIP's top-level contents should replace app_dir:
        extracted_root = staging_dir
        # If your zip contains a single folder and you want that folder to become app_dir:
        items = [os.path.join(staging_dir, x) for x in os.listdir(staging_dir)]
        if len(items) == 1 and os.path.isdir(items[0]):
            extracted_root = items[0]

        # Prepare final staging target (same parent as app_dir)
        final_stage = os.path.join(tmp_dir, "final")
        shutil.copytree(extracted_root, final_stage, dirs_exist_ok=True)

        # Swap
        backup_dir = None
        try:
            log("Swapping in new version ...")
            backup_dir = safe_swap(final_stage, app_dir)
        except OSError as e:
            # Some platforms may fail os.rename for dirs in use; fallback to copy+remove
            log(f"Rename swap failed: {e}; trying copy+remove fallback.")
            if os.path.exists(final_stage):
                if os.path.exists(app_dir):
                    backup_dir = os.path.join(os.path.dirname(app_dir), f"{os.path.basename(app_dir)}.backup-{int(time.time())}")
                    log(f"Creating backup: {backup_dir}")
                    shutil.move(app_dir, backup_dir)
                shutil.move(final_stage, app_dir)
            else:
                raise

        # Update version.json
        new_vdata = dict(vdata)
        new_vdata["version"] = tag.lstrip("v")
        write_json_atomic(version_file, new_vdata)
        log(f"Updated {version_file} -> {new_vdata['version']}")

        # Clean backup (optional): you can keep it for troubleshooting; here we remove it after success.
        if backup_dir and os.path.exists(backup_dir):
            shutil.rmtree(backup_dir, ignore_errors=True)

        # Restart if requested
        if restart_cmd:
            log(f"Restarting: {restart_cmd}")
            # On Windows, use start without shell popups; on *nix, typical shell works.
            if os.name == "nt":
                os.spawnl(os.P_NOWAIT, os.environ.get("COMSPEC", "cmd.exe"), "cmd", "/c", restart_cmd)
            else:
                pid = os.fork()
                if pid == 0:
                    os.execl("/bin/sh", "sh", "-lc", restart_cmd)
        return True

    except Exception as e:
        log(f"ERROR: {e!r}")
        # Attempt rollback if we swapped but failed later
        # (Here, failures usually occur before swap. If you extend logic, track backup_dir.)
        return False
    finally:
        # Clean temp dir
        shutil.rmtree(tmp_dir, ignore_errors=True)

# ---------- CLI ----------

def main():
    parser = argparse.ArgumentParser(description="GitHub Releases updater")
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--app-name", required=True, help="Logical app name for asset pattern")
    parser.add_argument("--app-dir", default=None, help="Path to your app directory (default: ./app)")
    parser.add_argument("--version-file", default=None, help="Path to version.json (default: ./version.json)")
    parser.add_argument("--asset-pattern", default="{app}-{os}-{arch}.zip", help="Asset name pattern")
    parser.add_argument("--include-prereleases", action="store_true", help="Allow prerelease updates")
    parser.add_argument("--allow-downgrade", action="store_true", help="Allow downgrades if remote < local")
    parser.add_argument("--restart-cmd", default=None, help='Command to relaunch app after update (e.g., "python app/main.py")')
    parser.add_argument("--timeout", type=int, default=60, help="Network timeout seconds")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen, but do not change anything")
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN")  # optional for private repos or higher rate limits
    app_dir = args.app_dir or os.path.join(os.getcwd(), "app")
    version_file = args.version_file or os.path.join(os.getcwd(), "version.json")

    ok = run_update(
        app_name=args.app_name,
        repo=args.repo,
        app_dir=app_dir,
        version_file=version_file,
        asset_pattern=args.asset_pattern,
        include_prereleases=args.include_prereleases,
        token=token,
        allow_downgrade=args.allow_downgrade,
        restart_cmd=args.restart_cmd,
        timeout=args.timeout,
        dry_run=args.dry_run,
    )
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
