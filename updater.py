#!/usr/bin/env python3
"""
Portable GitHub Releases updater (no external deps).

This script is the "engine" of your updater system.

High-level idea:
- Your app ships with a local version file (version.json) and an installed folder (app_dir).
- This script checks GitHub Releases for a newer version.
- If it finds one, it downloads the correct ZIP asset for the current OS+CPU.
- It optionally verifies the ZIP using a companion checksum file (.sha256).
- It extracts the ZIP into a staging folder, then swaps it into place:
      old app_dir -> backup folder
      new app     -> app_dir
- Then it updates version.json.
- If anything fails after the swap, it rolls back from the backup.
- Optionally restarts your app.

No third-party dependencies:
- Uses only Python standard library modules (urllib, zipfile, etc.)
"""

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import time
import zipfile
from urllib.request import Request, urlopen

# ================================================================
# Utilities
# ================================================================

def log(msg):
    """Printed output is captured by your GUI wrapper and shown in the log window."""
    print(f"[updater] {msg}")

def read_json(path, default=None):
    """Read a JSON file. Returns default if missing/invalid."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def write_json_atomic(path, data):
    """
    Write JSON atomically:
    - write to path.tmp
    - os.replace() swaps it into place (minimizes file corruption risk)
    """
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)

def norm_os():
    """Normalize OS name used in your GitHub asset naming."""
    s = platform.system().lower()
    if "windows" in s:
        return "windows"
    if "darwin" in s or "mac" in s:
        return "macos"
    return "linux"

def norm_arch():
    """Normalize CPU architecture used in your GitHub asset naming."""
    m = platform.machine().lower()
    if m in ("amd64", "x86_64", "x64"):
        return "x64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    if m in ("armv7l", "armv7", "arm32", "arm"):
        return "armv7"
    if m in ("i386", "i686", "x86"):
        return "x86"
    return m

def parse_semver(s):
    """
    Parse tags like 'v1.2.3' -> (1,2,3) for basic semver sorting.
    Non-semver returns (-1,-1,-1) so it sorts low.
    """
    if s.startswith("v"):
        s = s[1:]
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$", s)
    if not m:
        m2 = re.match(r"^(\d+)\.(\d+)(?:[-+].*)?$", s)
        if m2:
            return (int(m2.group(1)), int(m2.group(2)), 0)
        return (-1, -1, -1)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))

def compare_versions(a, b):
    """Return 1 if a>b, 0 if equal, -1 if a<b."""
    pa, pb = parse_semver(a), parse_semver(b)
    return (pa > pb) - (pa < pb)

def http_get_json(url, token=None, timeout=30):
    """GET JSON from GitHub API (supports optional Bearer token)."""
    req = Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "py-updater"
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def http_download(url, dest_path, token=None, timeout=60):
    """Stream-download a file to disk (large assets friendly)."""
    req = Request(url, headers={
        "Accept": "application/octet-stream",
        "User-Agent": "py-updater"
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urlopen(req, timeout=timeout) as resp, open(dest_path, "wb") as out:
        CHUNK = 1 << 20
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            out.write(chunk)

def unzip_to(zip_path, target_dir):
    """Extract ZIP to a directory."""
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(target_dir)

def sha256_file(path):
    """Compute SHA-256 of a file (chunked)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def verify_sha256_from_file(sha_path, asset_path):
    """
    Verify a ZIP with a companion .sha256 file.

    Accepts common format:
      <64hex>  filename.zip
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

# ================================================================
# GitHub release selection
# ================================================================

def get_latest_release(repo, include_prereleases=False, token=None, timeout=30):
    """
    Fetch /releases and pick the newest by semver, filtering drafts and prereleases.
    """
    url = f"https://api.github.com/repos/{repo}/releases?per_page=30"
    releases = http_get_json(url, token=token, timeout=timeout)

    candidates = [
        r for r in releases
        if not r.get("draft", False) and (include_prereleases or not r.get("prerelease", False))
    ]
    if not candidates:
        return None

    candidates.sort(key=lambda r: parse_semver(r.get("tag_name") or ""), reverse=True)
    return candidates[0]

def find_asset(release, asset_name):
    """Find a release asset dict by its exact name."""
    for a in release.get("assets", []):
        if a.get("name") == asset_name:
            return a
    return None

# ================================================================
# Install / swap + rollback helpers
# ================================================================

def safe_swap(staging_dir, app_dir):
    """
    Backup app_dir and swap staging_dir into place.

    Returns:
      backup_dir (path) so caller can delete it after success
      or use it to rollback after failure.
    """
    parent = os.path.dirname(os.path.abspath(app_dir))
    backup_dir = os.path.join(parent, f"{os.path.basename(app_dir)}.backup-{int(time.time())}")

    log(f"Creating backup: {backup_dir}")

    if os.path.exists(backup_dir):
        shutil.rmtree(backup_dir, ignore_errors=True)

    # Rename old -> backup (fast)
    if os.path.exists(app_dir):
        os.rename(app_dir, backup_dir)

    # Rename new -> app_dir (fast)
    os.rename(staging_dir, app_dir)

    return backup_dir

def rollback_from_backup(backup_dir, app_dir):
    """
    Rollback after a failed update:
    - remove the possibly broken app_dir
    - restore backup_dir back to app_dir
    """
    log("Rolling back to previous version ...")

    if os.path.exists(app_dir):
        shutil.rmtree(app_dir, ignore_errors=True)

    if backup_dir and os.path.exists(backup_dir):
        os.rename(backup_dir, app_dir)
        log("Rollback complete.")
    else:
        log("Rollback skipped (no backup found).")

# ================================================================
# Core update routine
# ================================================================

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
    Orchestrates the update.

    Returns a status string:
      - "UPDATED"   -> installed successfully
      - "NO_UPDATE" -> nothing installed (already latest, skipped, dry-run)
      - "ERROR"     -> failed (and rollback attempted if swap occurred)
    """
    os_id = norm_os()
    arch_id = norm_arch()
    asset_name = asset_pattern.format(app=app_name, os=os_id, arch=arch_id)

    vdata = read_json(version_file, default={}) or {}
    current_version = vdata.get("version", "0.0.0")

    log(f"Current version: {current_version}")
    log(f"Repo: {repo}, asset pattern -> {asset_name}")

    release = get_latest_release(repo, include_prereleases=include_prereleases, token=token, timeout=timeout)
    if not release:
        log("No suitable releases found.")
        return "NO_UPDATE"

    tag = release.get("tag_name") or "0.0.0"
    log(f"Latest release tag: {tag} (prerelease={release.get('prerelease')})")

    cmp = compare_versions(tag, current_version)
    if cmp < 0 and not allow_downgrade:
        log("Remote version is older; skipping (use --allow-downgrade to force).")
        return "NO_UPDATE"
    if cmp == 0:
        log("Already up to date.")
        return "NO_UPDATE"

    asset = find_asset(release, asset_name)
    if not asset:
        names = ", ".join(a.get("name") for a in release.get("assets", []))
        log(f"ERROR: Asset '{asset_name}' not found in release assets: {names}")
        return "ERROR"

    asset_url = asset.get("browser_download_url")
    sha_asset = find_asset(release, asset_name + ".sha256")
    sha_url = sha_asset.get("browser_download_url") if sha_asset else None

    if dry_run:
        log(f"[dry-run] Would download: {asset_name}")
        if sha_url:
            log(f"[dry-run] Would verify with: {asset_name}.sha256")
        return "NO_UPDATE"

    # Create temp work dir
    tmp_dir = None

    # Track whether we actually swapped (backup_dir only becomes non-None after swap)
    backup_dir = None

    try:
        tmp_dir = tempfile.mkdtemp(prefix="upd-")
        zip_path = os.path.join(tmp_dir, asset_name)
        sha_path = os.path.join(tmp_dir, asset_name + ".sha256") if sha_url else None

        # ---- Download asset ----
        log(f"Downloading asset to {zip_path} ...")
        http_download(asset_url, zip_path, token=token, timeout=timeout)

        # ---- Optional checksum verification ----
        if sha_url:
            log(f"Downloading checksum to {sha_path} ...")
            http_download(sha_url, sha_path, token=token, timeout=timeout)
            log("Verifying SHA-256 ...")
            verify_sha256_from_file(sha_path, zip_path)
        else:
            log("No .sha256 file provided for this asset (verification skipped).")

        # ---- Extract to staging ----
        staging_dir = os.path.join(tmp_dir, "staging")
        os.makedirs(staging_dir, exist_ok=True)
        log(f"Extracting to staging: {staging_dir}")
        unzip_to(zip_path, staging_dir)

        # If zip has a single top-level folder, use that folder as extracted_root
        extracted_root = staging_dir
        items = [os.path.join(staging_dir, x) for x in os.listdir(staging_dir)]
        if len(items) == 1 and os.path.isdir(items[0]):
            extracted_root = items[0]

        # Copy extracted content into a folder named "final"
        # (so we can rename/move final -> app_dir)
        final_stage = os.path.join(tmp_dir, "final")
        shutil.copytree(extracted_root, final_stage, dirs_exist_ok=True)

        # ---- Swap into place ----
        try:
            log("Swapping in new version ...")
            backup_dir = safe_swap(final_stage, app_dir)
        except OSError as e:
            # Fallback for platforms/filesystems where rename fails (often Windows locks)
            log(f"Rename swap failed: {e}; trying copy+remove fallback.")

            if os.path.exists(final_stage):
                if os.path.exists(app_dir):
                    backup_dir = os.path.join(
                        os.path.dirname(app_dir),
                        f"{os.path.basename(app_dir)}.backup-{int(time.time())}"
                    )
                    log(f"Creating backup: {backup_dir}")
                    shutil.move(app_dir, backup_dir)

                shutil.move(final_stage, app_dir)
            else:
                raise

        # ---- Update version file (after swap) ----
        new_vdata = dict(vdata)
        new_vdata["version"] = tag.lstrip("v")
        write_json_atomic(version_file, new_vdata)
        log(f"Updated {version_file} -> {new_vdata['version']}")

        # ---- Remove backup after success (optional) ----
        if backup_dir and os.path.exists(backup_dir):
            shutil.rmtree(backup_dir, ignore_errors=True)

        # ---- Optional restart ----
        if restart_cmd:
            log(f"Restarting: {restart_cmd}")
            if os.name == "nt":
                os.spawnl(os.P_NOWAIT, os.environ.get("COMSPEC", "cmd.exe"), "cmd", "/c", restart_cmd)
            else:
                pid = os.fork()
                if pid == 0:
                    os.execl("/bin/sh", "sh", "-lc", restart_cmd)

        return "UPDATED"

    except Exception as e:
        log(f"ERROR: {e!r}")

        # If we already swapped (backup_dir exists), attempt rollback.
        if backup_dir:
            try:
                rollback_from_backup(backup_dir, app_dir)
            except Exception as rb_e:
                log(f"ERROR: rollback failed: {rb_e!r}")

        return "ERROR"

    finally:
        # Clean up temp work dir
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

# ================================================================
# CLI
# ================================================================

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

    status = run_update(
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

    # Exit codes:
    # 0 = success (updated OR no update needed)
    # 1 = error (and rollback attempted if needed)
    sys.exit(1 if status == "ERROR" else 0)

if __name__ == "__main__":
    main()