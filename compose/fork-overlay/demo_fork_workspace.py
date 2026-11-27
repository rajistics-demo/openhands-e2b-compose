"""Bounded Git workspace archive round-trip for isolated checkpoint pilots.

Standalone, stdlib-only module that can:

  * create a deterministic tar archive of a bounded "git workspace" (a
    working tree plus its ``.git`` directory, including the index and any
    local commits), and
  * restore that archive into a fresh destination directory,

while rejecting content that would be unsafe or out of scope for an
isolated, ephemeral-sandbox checkpoint pilot:

  * special files (fifo, device, socket, door, ...) are rejected,
  * symlinks are archived *without following them*, but a symlink whose
    target would resolve outside the workspace root is rejected,
  * any path containing ``..`` segments, an absolute path, or other
    traversal is rejected,
  * any path that has a symlink as one of its parent directories is
    rejected (its final resolved location could be outside the root),
  * ``.git`` metadata that points outside the workspace root (linked
    worktrees via ``.git/worktrees``, or object "alternates") is rejected;
    such setups are explicitly out of scope for this pilot,
  * the number of archive members and the total uncompressed byte count
    are bounded and enforced both while scanning and while extracting,
  * file mode bits are preserved on round-trip,
  * extraction always targets a destination directory that does not yet
    exist (a "fresh" destination) to avoid clobbering or merging state,
  * a deterministic manifest is produced containing a per-member content
    hash and a single tree hash, so two archives of the same logical
    workspace content can be compared for equality without extracting.

This module intentionally has **no** third-party dependencies and performs
**no** network access, no git command invocation, and no writes outside of
the explicit ``dest`` path supplied by the caller.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import stat
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Iterable, Iterator


class WorkspaceArchiveError(Exception):
    """Base class for all errors raised by this module."""


class UnsafeMemberError(WorkspaceArchiveError):
    """Raised when a file system entry or archive member is unsafe to include."""


class ArchiveLimitError(WorkspaceArchiveError):
    """Raised when a bound (member count or total bytes) would be exceeded."""


class DestinationExistsError(WorkspaceArchiveError):
    """Raised when the extraction destination is not fresh (already exists)."""


# Reasonable defaults for a small, standalone public checkout in a sandbox.
DEFAULT_MAX_MEMBERS = 200_000
DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB

_MODE_MASK = 0o777


@dataclasses.dataclass(frozen=True)
class MemberRecord:
    """A single, normalized entry destined for (or read from) the archive."""

    path: str  # POSIX-style, relative to the workspace root, no leading "./"
    kind: str  # "file" | "dir" | "symlink"
    mode: int
    size: int = 0
    linkname: str = ''
    sha256: str = ''  # content hash; empty for dirs and symlinks


@dataclasses.dataclass(frozen=True)
class ArchiveManifest:
    """Deterministic summary of an archive's content, for consistency checks."""

    member_count: int
    total_bytes: int
    tree_hash: str
    members: tuple  # tuple[MemberRecord, ...], sorted by path


def _to_posix_rel(path: str) -> str:
    """Normalize a member path to a safe, root-relative POSIX path.

    Raises UnsafeMemberError on any absolute path, empty path, or path that
    contains a ``..`` traversal segment.
    """
    if not path or path in ('.', './'):
        raise UnsafeMemberError('empty or root path is not a valid member path')
    norm = path
    if norm.startswith('/'):
        raise UnsafeMemberError(f'absolute paths are not allowed: {path!r}')
    parts = [p for p in norm.split('/') if p not in ('', '.')]
    if not parts:
        raise UnsafeMemberError(f'empty path after normalization: {path!r}')
    if any(p == '..' for p in parts):
        raise UnsafeMemberError(f'path traversal is not allowed: {path!r}')
    return '/'.join(parts)


def _is_within_root(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True


def _check_git_metadata_scope(root: Path, git_dir: Path) -> None:
    """Reject .git metadata that references locations outside the root.

    Linked worktrees (``.git/worktrees/*``) and object "alternates" let a
    repository's history live partly outside of its own directory. Both are
    explicitly out of scope for this pilot, so their presence is rejected
    rather than silently archived in a way that would not round-trip.
    """
    worktrees_dir = git_dir / 'worktrees'
    if worktrees_dir.is_dir():
        raise UnsafeMemberError(
            'linked git worktrees are out of scope for this pilot: '
            f'found {worktrees_dir}'
        )

    alternates_candidates = [
        git_dir / 'objects' / 'info' / 'alternates',
    ]
    # A ".git" file (as opposed to a directory) indicates the real git dir
    # lives elsewhere, e.g. in a submodule or linked worktree setup.
    if git_dir.is_file():
        raise UnsafeMemberError(
            f'.git is a file (gitdir redirection), not a directory: {git_dir}'
        )
    for alt_file in alternates_candidates:
        if alt_file.is_file():
            try:
                lines = alt_file.read_text(errors='replace').splitlines()
            except OSError as exc:
                raise UnsafeMemberError(f'cannot read alternates file: {exc}') from exc
            for line in lines:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                alt_path = Path(line)
                if not alt_path.is_absolute():
                    alt_path = alt_file.parent / alt_path
                try:
                    resolved = alt_path.resolve()
                except OSError:
                    resolved = alt_path
                if not _is_within_root(root.resolve(), resolved):
                    raise UnsafeMemberError(
                        'git alternates pointing outside the workspace root '
                        'are out of scope for this pilot: '
                        f'{line!r} in {alt_file}'
                    )


def _iter_fs_entries(root: Path) -> Iterator[Path]:
    """Yield every entry under ``root`` (dirs, files, symlinks), root-first.

    Does not follow symlinked directories while walking (``os.walk`` with
    ``followlinks=False``, the default), so a symlink to a directory is
    yielded once as a symlink and never descended into.
    """

    def walk_error(error):
        raise error

    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(
        root, followlinks=False, onerror=walk_error
    ):
        dirpath_p = Path(dirpath)
        # Don't descend into symlinked directories; os.walk still lists them
        # in dirnames, so filter and report them as entries, not containers.
        real_dirnames = []
        for name in sorted(dirnames):
            p = dirpath_p / name
            if p.is_symlink():
                yield p
            else:
                real_dirnames.append(name)
                yield p
        dirnames[:] = real_dirnames
        for name in sorted(filenames):
            yield dirpath_p / name


def _check_no_symlink_parent(root: Path, entry: Path) -> None:
    """Reject entries whose containing directories include a symlink.

    A symlink parent means the entry's *effective* resolved location could
    be outside the workspace root even though its nominal path is inside
    it, so such entries are rejected outright rather than silently
    resolved.
    """
    rel = entry.relative_to(root)
    current = root
    for part in rel.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise UnsafeMemberError(
                f'path has a symlink as a parent directory: {entry} '
                f'(symlink parent: {current})'
            )


def _validate_symlink_target(root: Path, entry: Path) -> str:
    """Return the raw link target, rejecting out-of-root symlinks.

    The link is not followed/resolved for content purposes; only its target
    path is checked so that restoring it cannot later escape the root.
    """
    linkname = os.readlink(entry)
    if os.path.isabs(linkname):
        raise UnsafeMemberError('Absolute symlink targets are outside the pilot scope')
    resolved = entry.parent / linkname
    try:
        resolved = resolved.resolve(strict=False)
    except OSError as exc:
        raise UnsafeMemberError(f'cannot resolve symlink target: {exc}') from exc
    if not _is_within_root(root, resolved):
        raise UnsafeMemberError(
            f'symlink target escapes the workspace root: {entry} -> {linkname}'
        )
    return linkname


class _HashingReader:
    def __init__(self, fd):
        self.fd = fd
        self.hasher = hashlib.sha256()

    def read(self, size):
        payload = self.fd.read(size)
        self.hasher.update(payload)
        return payload


def _build_manifest(records: Iterable[MemberRecord]) -> ArchiveManifest:
    sorted_records = tuple(sorted(records, key=lambda r: r.path))
    total_bytes = sum(r.size for r in sorted_records)
    tree_hasher = hashlib.sha256()
    for r in sorted_records:
        line = '\x00'.join(
            [r.path, r.kind, format(r.mode, 'o'), str(r.size), r.linkname, r.sha256]
        )
        tree_hasher.update(line.encode('utf-8'))
        tree_hasher.update(b'\n')
    return ArchiveManifest(
        member_count=len(sorted_records),
        total_bytes=total_bytes,
        tree_hash=tree_hasher.hexdigest(),
        members=sorted_records,
    )


def create_archive(
    root: str | os.PathLike,
    dest_tar: str | os.PathLike,
    *,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    require_git: bool = True,
) -> ArchiveManifest:
    """Create a bounded, deterministic tar archive of a git workspace.

    Includes regular files, directories, symlinks (not followed), hidden
    and git-ignored files, and the entire ``.git`` directory (index and
    local commits included) -- i.e. every filesystem entry under ``root``
    with no git-aware filtering.

    Raises UnsafeMemberError for special files, out-of-root symlinks, path
    traversal, symlink parents, and out-of-scope git metadata (linked
    worktrees/alternates). Raises ArchiveLimitError if ``max_members`` or
    ``max_total_bytes`` would be exceeded. Raises FileNotFoundError if
    ``require_git`` is True and ``root/.git`` is missing.

    Returns the ArchiveManifest describing exactly what was archived, so
    callers can compare ``tree_hash`` across archives for a consistency
    check without extracting either one.
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise WorkspaceArchiveError(f'root is not a directory: {root}')

    git_dir = root / '.git'
    if git_dir.is_symlink():
        raise UnsafeMemberError('Linked .git directories are outside the pilot scope')
    if require_git and not git_dir.exists():
        raise FileNotFoundError(f'no .git directory found under root: {git_dir}')
    if git_dir.exists():
        _check_git_metadata_scope(root, git_dir)

    dest_tar = Path(dest_tar)
    dest_tar.parent.mkdir(parents=True, exist_ok=True)
    if dest_tar.exists():
        raise WorkspaceArchiveError(f'destination archive already exists: {dest_tar}')

    records = []
    total_bytes = 0
    member_count = 0

    tmp_tar = dest_tar.with_suffix(dest_tar.suffix + '.tmp')
    if tmp_tar.exists():
        tmp_tar.unlink()

    try:
        with tarfile.open(tmp_tar, mode='w:gz', format=tarfile.PAX_FORMAT) as tar:
            for entry in _iter_fs_entries(root):
                rel_path = entry.relative_to(root).as_posix()
                rel_path = _to_posix_rel(rel_path)
                _check_no_symlink_parent(root, entry)

                st = entry.lstat()
                mode = stat.S_IMODE(st.st_mode)
                if mode & ~_MODE_MASK:
                    raise UnsafeMemberError(
                        'Special permission bits are outside the pilot scope'
                    )

                if stat.S_ISLNK(st.st_mode):
                    linkname = _validate_symlink_target(root, entry)
                    kind = 'symlink'
                    size = 0
                    sha256 = ''
                elif stat.S_ISDIR(st.st_mode):
                    kind = 'dir'
                    linkname = ''
                    size = 0
                    sha256 = ''
                elif stat.S_ISREG(st.st_mode):
                    kind = 'file'
                    linkname = ''
                    size = st.st_size
                    sha256 = ''
                else:
                    raise UnsafeMemberError(
                        f'special file is not allowed in archive: {entry} '
                        f'(mode={oct(st.st_mode)})'
                    )

                member_count += 1
                if member_count > max_members:
                    raise ArchiveLimitError(
                        f'member count exceeds limit: {member_count} > {max_members}'
                    )
                total_bytes += size
                if total_bytes > max_total_bytes:
                    raise ArchiveLimitError(
                        f'total bytes exceeds limit: {total_bytes} > {max_total_bytes}'
                    )

                info = tarfile.TarInfo(name=rel_path)
                info.mode = mode
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ''
                info.gname = ''

                if kind == 'symlink':
                    info.type = tarfile.SYMTYPE
                    info.linkname = linkname
                    tar.addfile(info)
                elif kind == 'dir':
                    info.type = tarfile.DIRTYPE
                    tar.addfile(info)
                else:
                    info.type = tarfile.REGTYPE
                    info.size = size
                    fd = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, 'rb') as stream:
                        before = os.fstat(stream.fileno())
                        if (
                            not stat.S_ISREG(before.st_mode)
                            or before.st_size != size
                            or before.st_ino != st.st_ino
                        ):
                            raise WorkspaceArchiveError(
                                'Workspace member changed during capture'
                            )
                        reader = _HashingReader(stream)
                        tar.addfile(info, fileobj=reader)
                        after = os.fstat(stream.fileno())
                        if (
                            before.st_mtime_ns != after.st_mtime_ns
                            or before.st_ctime_ns != after.st_ctime_ns
                        ):
                            raise WorkspaceArchiveError(
                                'Workspace member changed during capture'
                            )
                        sha256 = reader.hasher.hexdigest()
                records.append(
                    MemberRecord(rel_path, kind, mode, size, linkname, sha256)
                )

        manifest = _build_manifest(records)
        tmp_tar.replace(dest_tar)
    except BaseException:
        if tmp_tar.exists():
            tmp_tar.unlink()
        if dest_tar.exists():
            dest_tar.unlink()
        raise

    return manifest


def extract_archive(
    src_tar: str | os.PathLike,
    dest: str | os.PathLike,
    *,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> ArchiveManifest:
    """Extract a tar archive created by :func:`create_archive` into ``dest``.

    ``dest`` must not already exist (a fresh destination is required); it
    is created fresh and removed again if extraction fails partway
    through. Re-validates every member against the same safety rules used
    at creation time (no special files, no traversal, no out-of-root
    symlinks, no symlink parents, bounded member count/bytes) so a
    tampered or foreign archive cannot escape ``dest`` even if it was not
    produced by :func:`create_archive`.

    Returns the ArchiveManifest of what was actually written to disk, for
    comparison against the manifest returned by :func:`create_archive`.
    """
    src_tar = Path(src_tar)
    dest = Path(dest)
    if os.path.lexists(dest):
        raise DestinationExistsError(f'destination already exists, not fresh: {dest}')

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.mkdir()

    records = []
    total_bytes = 0
    member_count = 0

    seen = set()
    directories = []
    links = []
    try:
        with tarfile.open(src_tar, mode='r:*') as tar:
            for info in tar:
                rel_path = _to_posix_rel(info.name)
                target = dest / rel_path
                if rel_path in seen:
                    raise UnsafeMemberError('Duplicate member in workspace archive')
                seen.add(rel_path)

                # Re-check symlink parents against what has been created so far.
                current = dest
                for part in Path(rel_path).parts[:-1]:
                    current = current / part
                    if current.is_symlink():
                        raise UnsafeMemberError(
                            f'path has a symlink as a parent directory: {rel_path}'
                        )

                member_count += 1
                if member_count > max_members:
                    raise ArchiveLimitError(
                        f'member count exceeds limit: {member_count} > {max_members}'
                    )

                mode = info.mode
                if mode & ~_MODE_MASK:
                    raise UnsafeMemberError(
                        'Special permission bits are outside the pilot scope'
                    )

                if info.issym():
                    linkname = info.linkname
                    if os.path.isabs(linkname):
                        raise UnsafeMemberError(
                            'Absolute symlink targets are outside the pilot scope'
                        )
                    links.append(target)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.symlink(linkname, target)
                    if stat.S_IMODE(target.lstat().st_mode) != mode:
                        os.chmod(target, mode, follow_symlinks=False)
                    records.append(
                        MemberRecord(
                            path=rel_path, kind='symlink', mode=mode, linkname=linkname
                        )
                    )
                elif info.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    directories.append((target, mode))
                    records.append(MemberRecord(path=rel_path, kind='dir', mode=mode))
                elif info.isreg():
                    size = info.size
                    total_bytes += size
                    if total_bytes > max_total_bytes:
                        raise ArchiveLimitError(
                            f'total bytes exceeds limit: {total_bytes} > {max_total_bytes}'
                        )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    src_f = tar.extractfile(info)
                    if src_f is None:
                        raise UnsafeMemberError(f'could not read member: {info.name}')
                    h = hashlib.sha256()
                    with open(target, 'xb') as out_f:
                        while True:
                            chunk = src_f.read(1024 * 1024)
                            if not chunk:
                                break
                            h.update(chunk)
                            out_f.write(chunk)
                    os.chmod(target, mode)
                    records.append(
                        MemberRecord(
                            path=rel_path,
                            kind='file',
                            mode=mode,
                            size=size,
                            sha256=h.hexdigest(),
                        )
                    )
                else:
                    raise UnsafeMemberError(
                        f'special file is not allowed in archive: {info.name} '
                        f'(type={info.type!r})'
                    )

        for link in links:
            _validate_symlink_target(dest.resolve(), link)
        for directory, mode in sorted(directories, key=lambda row: -len(row[0].parts)):
            os.chmod(directory, mode)
        manifest = _build_manifest(records)
    except BaseException:
        _rmtree(dest)
        raise

    return manifest


def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def verify_round_trip(root: str | os.PathLike, workdir: str | os.PathLike) -> bool:
    """Convenience helper: archive ``root``, restore it, and compare hashes.

    ``workdir`` must not already contain ``archive.tar`` or ``restored/``.
    Returns True iff the manifest tree_hash of the created archive matches
    the manifest tree_hash of the restored content.
    """
    workdir = Path(workdir)
    tar_path = workdir / 'archive.tar'
    restored_dir = workdir / 'restored'
    create_manifest = create_archive(root, tar_path)
    extract_manifest = extract_archive(tar_path, restored_dir)
    return create_manifest.tree_hash == extract_manifest.tree_hash


def main(argv):
    command, *args = argv
    archive = Path(args[0] if command in ('restore', 'cleanup') else args[1])
    if archive.parent != Path('/tmp') or not re.fullmatch(
        r'canvas-pilot-[a-f0-9]{32}[.]tar[.]gz', archive.name
    ):
        raise WorkspaceArchiveError('Unexpected archive location')
    if command == 'cleanup':
        archive.unlink(missing_ok=True)
        print('{}')
        return

    workspace = Path(args[0] if command == 'capture' else args[1])
    root = Path('/workspace/project')
    if not workspace.is_relative_to(root) or workspace.resolve() != workspace:
        raise WorkspaceArchiveError('Unexpected workspace root')
    limits = {'max_members': 3000, 'max_total_bytes': 12 * 1024 * 1024}
    os.umask(0o077)
    if command == 'capture':
        manifest = create_archive(workspace, archive, require_git=False, **limits)
    elif command == 'restore':
        workspace.mkdir(parents=True, exist_ok=True)
        if any(workspace.iterdir()):
            raise DestinationExistsError('Replacement workspace must start empty')
        with tempfile.TemporaryDirectory(prefix='.fork-extract-', dir=workspace) as staging:
            extracted = Path(staging) / 'tree'
            manifest = extract_archive(archive, extracted, **limits)
            git_dir = extracted / '.git'
            if git_dir.exists():
                _check_git_metadata_scope(extracted, git_dir)
            for child in extracted.iterdir():
                child.rename(workspace / child.name)
    else:
        raise WorkspaceArchiveError('Unknown workspace operation')
    print(json.dumps({'tree_hash': manifest.tree_hash, 'member_count': manifest.member_count}))


if __name__ == '__main__':
    main(sys.argv[1:])
