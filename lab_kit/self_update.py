"""Self-update support for Lab Kit's standalone installer layout."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import tokenize
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from . import ui
from .metadata import package_version
from .ui import banner, emit_json, kv, section, status_line
from .utils import die

BIN_NAME = "labkit"
LEGACY_BIN_NAME = "lab-kit"
DEFAULT_REPO_OWNER = "jethros-projects"
DEFAULT_REPO_NAME = "labkit-cli"
DEFAULT_REF = "main"


def _source_checkout_dir(path: Path) -> bool:
    return (path / "pyproject.toml").is_file() and (path / "install.sh").is_file() and (path / "lab_kit").is_dir()


def _current_executable() -> Path | None:
    argv0 = Path(sys.argv[0]).expanduser()
    if argv0.name == BIN_NAME and argv0.exists():
        return argv0.resolve()
    found = shutil.which(BIN_NAME)
    return Path(found).resolve() if found else None


def default_install_dir() -> Path:
    explicit = os.environ.get("LABKIT_INSTALL_DIR") or os.environ.get("INSTALL_DIR")
    if explicit:
        return Path(explicit).expanduser()

    executable = _current_executable()
    if executable and (executable.parent / "lab_kit").is_dir() and not _source_checkout_dir(executable.parent):
        return executable.parent

    return Path("~/.local/bin").expanduser()


def _expected_sha256(args: argparse.Namespace) -> str:
    return str(args.sha256 or os.environ.get("LABKIT_SHA256") or os.environ.get("SHA256") or "").strip().lower()


def _ref(args: argparse.Namespace) -> str:
    return str(args.ref or os.environ.get("LABKIT_REF") or os.environ.get("REF") or DEFAULT_REF)


def _repo_owner(args: argparse.Namespace) -> str:
    return str(args.repo_owner or os.environ.get("LABKIT_REPO_OWNER") or DEFAULT_REPO_OWNER)


def _repo_name(args: argparse.Namespace) -> str:
    return str(args.repo_name or os.environ.get("LABKIT_REPO_NAME") or DEFAULT_REPO_NAME)


def _archive_url(args: argparse.Namespace, ref: str, repo_owner: str, repo_name: str) -> str:
    if args.archive_url:
        return str(args.archive_url)
    return f"https://github.com/{repo_owner}/{repo_name}/archive/{ref}.tar.gz"


def _download_archive(url: str, destination: Path) -> None:
    local_path = Path(url).expanduser()
    if "://" not in url and local_path.is_file():
        shutil.copy2(local_path, destination)
        return

    request = Request(url, headers={"User-Agent": "labkit-cli"})
    try:
        with urlopen(request, timeout=60) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output)
    except (OSError, URLError) as exc:
        die(f"Could not download Lab Kit archive from {url}: {exc}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_checksum(path: Path, expected: str) -> str:
    actual = _sha256(path)
    if expected and actual != expected:
        die(f"Archive checksum mismatch: expected {expected}, got {actual}")
    return actual


def _ensure_inside(root: Path, target: Path) -> None:
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError:
        die(f"Archive contains a path outside the extraction directory: {target}")


def _safe_extract(archive_path: Path, destination: Path) -> None:
    try:
        with tarfile.open(archive_path, "r:*") as archive:
            for member in archive.getmembers():
                target = destination / member.name
                _ensure_inside(destination, target)
                if member.islnk() or member.issym():
                    if os.path.isabs(member.linkname):
                        die(f"Archive contains an unsafe absolute link: {member.name}")
                    _ensure_inside(destination, target.parent / member.linkname)
            archive.extractall(destination)
    except (tarfile.TarError, OSError) as exc:
        die(f"Could not extract Lab Kit archive: {exc}")


def _is_source_root(path: Path) -> bool:
    return (path / BIN_NAME).is_file() and (path / "lab_kit").is_dir()


def _find_source_root(extracted: Path) -> Path:
    if _is_source_root(extracted):
        return extracted
    candidates = [path for path in extracted.iterdir() if path.is_dir() and _is_source_root(path)]
    if len(candidates) == 1:
        return candidates[0]
    die("Downloaded archive does not contain a Lab Kit source tree.")
    raise AssertionError("unreachable")


def _compile_python_source(path: Path) -> None:
    try:
        with tokenize.open(str(path)) as file:
            source = file.read()
        compile(source, str(path), "exec")
    except SyntaxError as exc:
        location = f"{path}:{exc.lineno}" if exc.lineno else str(path)
        die(f"Downloaded Lab Kit Python file has invalid syntax at {location}: {exc.msg}")
    except (OSError, UnicodeDecodeError) as exc:
        die(f"Could not read downloaded Lab Kit Python file {path}: {exc}")


def _validate_source(source_root: Path) -> None:
    bin_path = source_root / BIN_NAME
    package_path = source_root / "lab_kit"
    _compile_python_source(bin_path)
    for path in sorted(package_path.rglob("*.py")):
        _compile_python_source(path)


def _replace_package(source_package: Path, destination_package: Path) -> None:
    tmp_package = destination_package.parent / f".{destination_package.name}.tmp.{os.getpid()}"
    if tmp_package.exists():
        shutil.rmtree(tmp_package)
    shutil.copytree(source_package, tmp_package)
    if destination_package.exists():
        shutil.rmtree(destination_package)
    os.replace(tmp_package, destination_package)


def _replace_launcher(source_launcher: Path, destination_launcher: Path) -> None:
    tmp_launcher = destination_launcher.with_name(f".{destination_launcher.name}.tmp.{os.getpid()}")
    shutil.copy2(source_launcher, tmp_launcher)
    mode = tmp_launcher.stat().st_mode
    tmp_launcher.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.replace(tmp_launcher, destination_launcher)


def _install_source(source_root: Path, install_dir: Path) -> dict[str, str]:
    install_dir.mkdir(parents=True, exist_ok=True)
    launcher = install_dir / BIN_NAME
    legacy_launcher = install_dir / LEGACY_BIN_NAME
    package = install_dir / "lab_kit"

    _validate_source(source_root)
    _replace_package(source_root / "lab_kit", package)
    _replace_launcher(source_root / BIN_NAME, launcher)
    if legacy_launcher.exists():
        legacy_launcher.unlink()

    return {"launcher": str(launcher), "package": str(package), "install_dir": str(install_dir)}


def _path_contains(path: Path) -> bool:
    try:
        target = path.resolve()
    except OSError:
        target = path
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry).expanduser()
        try:
            if candidate.resolve() == target:
                return True
        except OSError:
            if candidate == target:
                return True
    return False


def _plan(args: argparse.Namespace) -> dict[str, Any]:
    ref = _ref(args)
    repo_owner = _repo_owner(args)
    repo_name = _repo_name(args)
    install_dir = Path(args.install_dir).expanduser() if args.install_dir else default_install_dir()
    return {
        "command": getattr(args, "self_update_command", "update"),
        "ok": True,
        "dry_run": bool(args.dry_run),
        "current_version": package_version(),
        "ref": ref,
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "archive_url": _archive_url(args, ref, repo_owner, repo_name),
        "install_dir": str(install_dir),
        "checksum_required": bool(_expected_sha256(args)),
    }


def cmd_self_update(args: argparse.Namespace) -> None:
    result = _plan(args)
    install_dir = Path(result["install_dir"])

    if result["dry_run"]:
        if ui.JSON_OUTPUT:
            emit_json(result)
            return
        banner("Preview a Lab Kit CLI update.")
        section("Plan")
        kv("current version", result["current_version"])
        kv("source", result["archive_url"])
        kv("ref", result["ref"])
        kv("install dir", install_dir)
        kv("checksum", "required" if result["checksum_required"] else "not pinned")
        return

    expected = _expected_sha256(args)
    with tempfile.TemporaryDirectory(prefix="labkit-update.") as tmp:
        tmp_dir = Path(tmp)
        archive_path = tmp_dir / "labkit.tar.gz"
        _download_archive(str(result["archive_url"]), archive_path)
        result["sha256"] = _verify_checksum(archive_path, expected)
        extract_dir = tmp_dir / "src"
        extract_dir.mkdir()
        _safe_extract(archive_path, extract_dir)
        source_root = _find_source_root(extract_dir)
        result.update(_install_source(source_root, install_dir))

    result["path_ready"] = _path_contains(install_dir)

    if ui.JSON_OUTPUT:
        emit_json(result)
        return

    banner("Update Lab Kit CLI.")
    section("Installed")
    status_line("ok", "Lab Kit updated", result["launcher"])
    kv("source", result["archive_url"])
    kv("ref", result["ref"])
    kv("sha256", result["sha256"])
    if result["path_ready"]:
        kv("try", "labkit --version")
    else:
        status_line("warn", "Install directory is not on PATH", str(install_dir))
        kv("current shell", f'export PATH="{install_dir}:$PATH"')
