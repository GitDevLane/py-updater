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
- Optionally restarts your app.

No third-party dependencies:
- Uses only Python standard library modules (urllib, zipfile, etc.)
"""

# ----------------------------
# Imports (why each exists)
# ----------------------------
import argparse   # parse command-line arguments like --repo, --app-name, --dry-run
import hashlib    # compute SHA-256 for integrity checks
import io         # (currently unused in this version; safe to remove unless you plan stream work)
import json       # read/write version.json and GitHub API responses
import os         # paths, environment vars, renames, process operations
import platform   # detect OS + CPU architecture
import re         # regex parsing of semver tags + sha256 formats
import shutil     # file/folder copy/move/remove utilities
import sys        # exit codes via sys.exit(...)
import tempfile   # create temp folder for downloads + staging
import time       # timestamps for backup folder naming
import zipfile    # unzip release asset
from datetime import datetime  # (currently unused; safe to remove unless you want timestamp logs)
from urllib.request import Request, urlopen  # HTTP requests (GitHub API + asset downloads)
from urllib.error import HTTPError, URLError # (currently unused; useful if you want nicer error messages)


# ================================================================
# Utilities
# ================================================================

def log(msg):
    """
    Simple logging helper.

    Why this matters:
    - The GUI wrapper captures stdout from this script.
    - Anything you print here becomes visible in the GUI log window.
    """
    print(f"[updater] {msg}")


def read_json(path, default=None):
    """
    Read JSON file from disk safely.

    Returns:
      - parsed dict/list if successful
      - 'default' if file missing, invalid JSON, permission error, etc.

    This prevents crashes if version.json doesn't exist yet.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json_atomic(path, data):
    """
    Write JSON "atomically" to reduce risk of corruption.

    Strategy:
    - write to a temporary file in the same folder (path.tmp)
    - then os.replace() swaps the temp file into place in a single operation

    Why this matters:
    - If power is lost / program crashes mid-write, you might otherwise corrupt version.json.
    - Atomic replace ensures you either have the old file or the new one, not half-written.
    """
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # os.replace is atomic on most platforms when staying on the same filesystem
    os.replace(tmp, path)


def norm_os():
    """
    Normalize the OS name to a stable value used in asset filenames.

    platform.system() examples:
      - Windows
      - Darwin (macOS)
      - Linux

    We convert these to:
      - "windows"
      - "macos"
      - "linux"
    """
    s = platform.system().lower()
    if "windows" in s:
        return "windows"
    if "darwin" in s or "mac" in s:
        return "macos"
    return "linux"


def norm_arch():
    """
    Normalize CPU architecture to a stable value used in asset filenames.

    platform.machine() examples:
      - AMD64, x86_64, arm64, aarch64, i686, etc.

    We normalize into a smaller set:
      - x64
      - arm64
      - armv7
      - x86
      - otherwise: return raw value as fallback (ppc64le, etc.)
    """
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

    # If architecture is something uncommon, we use what platform.machine() gave us.
    return m


def parse_semver(s):
    """
    Convert a release tag string into something comparable.

    Goal:
    - GitHub release tags are strings (ex: "v1.2.3").
    - To determine "newer", we parse into numeric tuples (major, minor, patch).

    Supported formats:
      - "v1.2.3" -> (1,2,3)
      - "1.2.3"  -> (1,2,3)
      - "1.2"    -> (1,2,0)
      - "1.2.3-beta.1" -> (1,2,3)  (prerelease ignored in this basic parser)

    If it doesn't look like semver:
      - return (-1,-1,-1) so it sorts very low.
    """
    if s.startswith("v"):
        s = s[1:]

    # Basic semver: major.minor.patch with optional suffix like -beta or +build
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$", s)
    if not m:
        # Looser semver: major.minor (assume patch=0)
        m2 = re.match(r"^(\d+)\.(\d+)(?:[-+].*)?$", s)
        if m2:
            return (int(m2.group(1)), int(m2.group(2)), 0)

        # If it's not parseable, treat as very old
        return (-1, -1, -1)

    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def compare_versions(a, b):
    """
    Compare version tags a and b.

    Returns:
      1  if a > b
      0  if a == b
     -1  if a < b

    Note:
    - This compares only numeric major/minor/patch (ignores prerelease).
    - If you ever want strict semver with prerelease ordering,
      you'd expand parse_semver to include prerelease weight.
    """
    pa, pb = parse_semver(a), parse_semver(b)
    return (pa > pb) - (pa < pb)


def http_get_json(url, token=None, timeout=30):
    """
    HTTP GET expecting JSON response.

    Used for GitHub API calls like:
      https://api.github.com/repos/{repo}/releases

    token:
      Optional GitHub token (GH_TOKEN env var). Useful for:
      - private repos
      - higher API rate limits

    Important headers:
      - Accept: GitHub API content type
      - User-Agent: GitHub API requires one
      - Authorization: Bearer <token> when provided
    """
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
    """
    Download a file from a URL to disk by streaming in chunks.

    Why stream?
    - Avoid loading the whole zip into memory.
    - Works for large assets.

    GitHub release assets:
    - Setting Accept: application/octet-stream ensures we get the raw file bytes.
    """
    req = Request(url, headers={
        "Accept": "application/octet-stream",
        "User-Agent": "py-updater"
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    # Stream response to file on disk
    with urlopen(req, timeout=timeout) as resp, open(dest_path, "wb") as out:
        CHUNK = 1 << 20  # 1 MiB
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            out.write(chunk)


def unzip_to(zip_path, target_dir):
    """
    Extract a zip file to target_dir.
    """
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(target_dir)


def sha256_file(path):
    """
    Compute SHA-256 hash of a file.

    Reads in chunks so it works on large files without high RAM use.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):  # 1 MiB chunks
            h.update(chunk)
    return h.hexdigest()


def verify_sha256_from_file(sha_path, asset_path):
    """
    Verify the downloaded asset by comparing SHA-256 hashes.

    Expected format in the .sha256 file (common style):
        <64hex>  filename.zip

    We don't strictly require filename matching; we just extract the first 64 hex string.
    """
    with open(sha_path, "r", encoding="utf-8", errors="ignore") as f:
        txt = f.read().strip()

    # Find any 64-char hex sequence in the file
    m = re.search(r"([A-Fa-f0-9]{64})", txt)
    if not m:
        raise ValueError(f"Invalid sha256 file format: {sha_path}")

    expected = m.group(1).lower()
    actual = sha256_file(asset_path)

    if actual != expected:
        raise ValueError(
            f"SHA-256 mismatch.\n"
            f"  expected={expected}\n"
            f"  actual  ={actual}"
        )

    return True


# ================================================================
# GitHub release selection
# ================================================================

def get_latest_release(repo, include_prereleases=False, token=None, timeout=30):
    """
    Fetch releases and choose the "latest" based on semver tag.

    Why not use /releases/latest?
    - /latest ignores prerelease filtering choices and doesn't always represent semver order
    - Here we want:
      * exclude drafts always
      * exclude prereleases unless include_prereleases=True
      * sort by semver ourselves
    """
    url = f"https://api.github.com/repos/{repo}/releases?per_page=30"

    # "releases" becomes a list of dicts, each representing a release object
    releases = http_get_json(url, token=token, timeout=timeout)

    # Filter out drafts; optionally filter prereleases
    candidates = [
        r for r in releases
        if not r.get("draft", False)
        and (include_prereleases or not r.get("prerelease", False))
    ]

    if not candidates:
        return None

    # Sort candidates by parsed semver tuple (major, minor, patch), newest first
    candidates.sort(key=lambda r: parse_semver(r.get("tag_name") or ""), reverse=True)

    return candidates[0]


def find_asset(release, asset_name):
    """
    Given a release object, find a specific asset by its "name".

    release["assets"] is a list of dicts with keys like:
      - name
      - browser_download_url
      - size, etc.
    """
    for a in release.get("assets", []):
        if a.get("name") == asset_name:
            return a
    return None


# ================================================================
# Install / swap operations
# ================================================================

def safe_swap(staging_dir, app_dir):
    """
    Replace the installed app directory with the new staged one.

    Approach:
    1) Create a backup folder name next to app_dir
    2) Rename app_dir -> backup_dir  (fast and usually atomic on same drive)
    3) Rename staging_dir -> app_dir (fast and usually atomic)

    Returns:
      backup_dir path, so caller can delete it after success or use it for rollback.

    Notes:
    - os.rename is fast because it typically just updates filesystem pointers.
    - rename can fail on Windows if files are locked/in use.
      (Caller has a fallback path when rename fails.)
    """
    parent = os.path.dirname(os.path.abspath(app_dir))
    backup_dir = os.path.join(
        parent,
        f"{os.path.basename(app_dir)}.backup-{int(time.time())}"
    )

    log(f"Creating backup: {backup_dir}")

    # If a backup with same name exists (rare), remove it
    if os.path.exists(backup_dir):
        shutil.rmtree(backup_dir, ignore_errors=True)

    # Move the old app out of the way
    if os.path.exists(app_dir):
        os.rename(app_dir, backup_dir)

    # Move new version into place
    os.rename(staging_dir, app_dir)

    return backup_dir


def rollback_from_backup(backup_dir, app_dir):
    """
    Restore old version from backup directory.

    Strategy:
    - delete current (possibly broken) app_dir
    - rename backup_dir back to app_dir
    """
    if os.path.exists(app_dir):
        shutil.rmtree(app_dir, ignore_errors=True)
    if os.path.exists(backup_dir):
        os.rename(backup_dir, app_dir)


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
    This is the main orchestration function.

    Returns:
      True  -> an update was installed successfully
      False -> no update installed (already latest, skipped, dry-run, error, etc.)

    Parameters (most important):
      app_name:
        used for asset naming (pattern formatting)
      repo:
        "owner/repo" GitHub repository
      app_dir:
        folder where your app is installed (the folder that will be replaced)
      version_file:
        JSON file storing current version (ex: {"version": "1.2.3"})
      dry_run:
        if True, do checks and print actions but do not change disk.
    """
    # Determine OS+arch so we can choose the right asset
    os_id = norm_os()
    arch_id = norm_arch()

    # Create the exact file name expected in the GitHub release assets
    asset_name = asset_pattern.format(app=app_name, os=os_id, arch=arch_id)

    # Load current version information
    vdata = read_json(version_file, default={}) or {}
    current_version = vdata.get("version", "0.0.0")

    log(f"Current version: {current_version}")
    log(f"Repo: {repo}, asset pattern -> {asset_name}")

    # Find best release candidate (filtered and semver-sorted)
    release = get_latest_release(
        repo,
        include_prereleases=include_prereleases,
        token=token,
        timeout=timeout
    )

    if not release:
        log("No suitable releases found.")
        return False

    # Tag name is like "v1.2.3"
    tag = release.get("tag_name") or "0.0.0"
    log(f"Latest release tag: {tag} (prerelease={release.get('prerelease')})")

    # Compare remote vs local
    cmp = compare_versions(tag, current_version)

    # If remote is older, skip unless allow_downgrade=True
    if cmp < 0 and not allow_downgrade:
        log("Remote version is older; skipping (use --allow-downgrade to force).")
        return False

    # If equal, no update needed
    if cmp == 0:
        log("Already up to date.")
        return False

    # Find the exact asset needed for this system
    asset = find_asset(release, asset_name)
    if not asset:
        # Helpful error: list assets in the release so user sees what exists
        names = ", ".join(a.get("name") for a in release.get("assets", []))
        raise FileNotFoundError(
            f"Asset '{asset_name}' not found in release assets: {names}"
        )

    asset_url = asset.get("browser_download_url")

    # Optional checksum asset: "<asset_name>.sha256"
    sha_asset = find_asset(release, asset_name + ".sha256")
    sha_url = sha_asset.get("browser_download_url") if sha_asset else None

    # Dry-run: stop here before writing anything to disk
    if dry_run:
        log(f"[dry-run] Would download: {asset_name}")
        if sha_url:
            log(f"[dry-run] Would verify with: {asset_name}.sha256")
        return False

    # Create an isolated temp directory for download + extraction + staging
    tmp_dir = tempfile.mkdtemp(prefix="upd-")
    zip_path = os.path.join(tmp_dir, asset_name)
    sha_path = os.path.join(tmp_dir, asset_name + ".sha256") if sha_url else None

    # Track backup_dir so we *could* rollback after swap if needed.
    # NOTE: Your current except block does not call rollback; we’ll comment it and you can upgrade later.
    backup_dir = None

    try:
        # --------------------------
        # Download release asset
        # --------------------------
        log(f"Downloading asset to {zip_path} ...")
        http_download(asset_url, zip_path, token=token, timeout=timeout)

        # --------------------------
        # Download + verify checksum
        # --------------------------
        if sha_url:
            log(f"Downloading checksum to {sha_path} ...")
            http_download(sha_url, sha_path, token=token, timeout=timeout)

            log("Verifying SHA-256 ...")
            verify_sha256_from_file(sha_path, zip_path)
        else:
            log("No .sha256 file provided for this asset (verification skipped).")

        # --------------------------
        # Extract ZIP to staging
        # --------------------------
        staging_dir = os.path.join(tmp_dir, "staging")
        os.makedirs(staging_dir, exist_ok=True)

        log(f"Extracting to staging: {staging_dir}")
        unzip_to(zip_path, staging_dir)

        # Decide what folder should become the new app_dir.
        #
        # Scenario A (flat zip):
        #   ZIP contains files directly (main.py, assets/, etc.)
        #   staging_dir itself becomes app_dir
        #
        # Scenario B (single top-level folder):
        #   ZIP contains one folder like "myapp/" and everything is inside it
        #   In that case we want that single folder to become app_dir.
        extracted_root = staging_dir
        items = [os.path.join(staging_dir, x) for x in os.listdir(staging_dir)]
        if len(items) == 1 and os.path.isdir(items[0]):
            extracted_root = items[0]

        # Prepare final staging directory:
        # We copy into tmp_dir/final because safe_swap uses rename/move,
        # and we want the folder name "final" to be the folder we move into app_dir.
        final_stage = os.path.join(tmp_dir, "final")
        shutil.copytree(extracted_root, final_stage, dirs_exist_ok=True)

        # --------------------------
        # Swap into place
        # --------------------------
        try:
            log("Swapping in new version ...")
            backup_dir = safe_swap(final_stage, app_dir)

        except OSError as e:
            # Common on Windows: rename can fail if files are "in use"
            # (ex: your app is still running and locks certain files).
            log(f"Rename swap failed: {e}; trying copy+remove fallback.")

            if os.path.exists(final_stage):
                # Back up existing app_dir first
                if os.path.exists(app_dir):
                    backup_dir = os.path.join(
                        os.path.dirname(app_dir),
                        f"{os.path.basename(app_dir)}.backup-{int(time.time())}"
                    )
                    log(f"Creating backup: {backup_dir}")
                    shutil.move(app_dir, backup_dir)

                # Move the new version into place
                shutil.move(final_stage, app_dir)
            else:
                # If final_stage doesn't exist, something went wrong earlier
                raise

        # --------------------------
        # Update version.json
        # --------------------------
        new_vdata = dict(vdata)

        # Store without leading "v" so version.json stays numeric ("1.2.3" not "v1.2.3")
        new_vdata["version"] = tag.lstrip("v")

        write_json_atomic(version_file, new_vdata)
        log(f"Updated {version_file} -> {new_vdata['version']}")

        # --------------------------
        # Cleanup backup after success
        # --------------------------
        # You currently remove the backup after success.
        # Some apps prefer keeping backups for one version in case user wants rollback.
        if backup_dir and os.path.exists(backup_dir):
            shutil.rmtree(backup_dir, ignore_errors=True)

        # --------------------------
        # Optional restart
        # --------------------------
        if restart_cmd:
            log(f"Restarting: {restart_cmd}")

            # Windows:
            # Use cmd.exe /c so it runs the command and returns immediately (no blocking).
            if os.name == "nt":
                os.spawnl(
                    os.P_NOWAIT,
                    os.environ.get("COMSPEC", "cmd.exe"),
                    "cmd", "/c", restart_cmd
                )
            else:
                # Unix-like:
                # fork() creates a child process.
                # child replaces itself with /bin/sh -lc "restart_cmd"
                # parent continues and exits normally.
                pid = os.fork()
                if pid == 0:
                    os.execl("/bin/sh", "sh", "-lc", restart_cmd)

        return True

    except Exception as e:
        log(f"ERROR: {e!r}")

        # IMPORTANT NOTE ABOUT ROLLBACK:
        # You have rollback_from_backup(), but you are NOT calling it here.
        # If an error occurs after the swap, you may want:
        #
        #   if backup_dir:
        #       log("Rolling back...")
        #       rollback_from_backup(backup_dir, app_dir)
        #
        # Right now, most errors happen before swap, but it's not guaranteed.
        return False

    finally:
        # Always delete the temp folder (zip, staging, final) even on failures.
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ================================================================
# CLI entry point
# ================================================================

def main():
    """
    Command-line interface for the updater.

    The GUI wrapper builds a command like:
      python updater.py --repo owner/repo --app-name myapp --dry-run

    So main() is what runs in that subprocess.
    """
    parser = argparse.ArgumentParser(description="GitHub Releases updater")

    # Required arguments (must be provided)
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--app-name", required=True, help="Logical app name for asset pattern")

    # Optional overrides (if not provided, defaults are used below)
    parser.add_argument("--app-dir", default=None, help="Path to your app directory (default: ./app)")
    parser.add_argument("--version-file", default=None, help="Path to version.json (default: ./version.json)")
    parser.add_argument("--asset-pattern", default="{app}-{os}-{arch}.zip", help="Asset name pattern")

    # Feature flags
    parser.add_argument("--include-prereleases", action="store_true", help="Allow prerelease updates")
    parser.add_argument("--allow-downgrade", action="store_true", help="Allow downgrades if remote < local")

    # Optional restart behavior
    parser.add_argument(
        "--restart-cmd",
        default=None,
        help='Command to relaunch app after update (e.g., "python app/main.py")'
    )

    # Network behavior
    parser.add_argument("--timeout", type=int, default=60, help="Network timeout seconds")

    # Dry-run behavior
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen, but do not change anything")

    args = parser.parse_args()

    # Optional GitHub token (recommended for private repos or rate-limit avoidance)
    token = os.environ.get("GH_TOKEN")

    # If user didn't pass explicit paths, default to current working directory
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

    # Exit codes:
    # 0 = update installed
    # 1 = no update installed OR failure
    #
    # NOTE: Your GUI prints this exit code, but logically you might want:
    #   0 = success (including "already up to date")
    #   1 = error
    #
    # Right now "already up to date" returns False -> exit 1, which can look like failure.
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()