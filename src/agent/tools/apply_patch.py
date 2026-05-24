"""apply_patch — Codex-style DSL for SWE file editing.

Edit files in the workspace using a structured diff format. Used by the harvest
agent when writing/iterating on crawler code.

Grammar (Codex apply_patch DSL):

  *** Begin Patch
  *** Add File: <path>
  +line1
  +line2
  *** Update File: <path>
  *** Move to: <new_path>          # optional rename
  @@ <header>                      # optional context anchor
   <context line>                  # leading space = unchanged
  -<line to remove>
  +<line to add>
  *** End of File                  # optional, marks chunk ends at EOF
  *** Delete File: <path>
  *** End Patch

Paths are RELATIVE to the workspace cwd:
  artifacts/{domain}/runs/{run_id}/workspace/

See: docs/工具重新设计共识.md, ../apply_patch instructions
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.config import Config
from src.utils.logging import get_logger

logger = get_logger(__name__)

# ── Tool metadata ────────────────────────────────────────

TOOL_NAME = "apply_patch"
TOOL_DESCRIPTION = (
    "Edit files in the workspace using a diff DSL.\n\n"
    "Use this for code edits — create new files, modify existing ones, delete, rename.\n"
    "Paths are RELATIVE to workspace cwd (same as `bash`): "
    "artifacts/{domain}/runs/{run_id}/workspace/\n\n"
    "Format:\n"
    "  *** Begin Patch\n"
    "  *** Add File: <path>            # every line below prefixed with '+'\n"
    "  +<line>\n"
    "  *** Update File: <path>\n"
    "  *** Move to: <new_path>         # optional rename\n"
    "  @@ <header>                     # optional disambiguation (function name etc.)\n"
    "   <unchanged context>            # space-prefix = context (3 lines above + below recommended)\n"
    "  -<line to remove>\n"
    "  +<line to add>\n"
    "  *** End of File                 # optional, if change is at file end\n"
    "  *** Delete File: <path>\n"
    "  *** End Patch\n\n"
    "Multi-file patches supported (multiple hunks in one call).\n"
    "Use `bash` for: viewing files (cat / head / sed), running code, git ops.\n"
    "Returns a summary of files added / modified / deleted."
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "patch": {
            "type": "string",
            "description": (
                "Full patch text starting with '*** Begin Patch' and "
                "ending with '*** End Patch'. Newlines are real LF, not literal '\\n'."
            ),
        },
    },
    "required": ["patch"],
    "additionalProperties": False,
}


# ── Markers ──────────────────────────────────────────────

_BEGIN = "*** Begin Patch"
_END = "*** End Patch"
_ADD = "*** Add File: "
_DELETE = "*** Delete File: "
_UPDATE = "*** Update File: "
_MOVE = "*** Move to: "
_EOF = "*** End of File"
_CONTEXT = "@@"


# ── Parser types ─────────────────────────────────────────


class PatchParseError(Exception):
    """Raised when the patch text is malformed."""


@dataclass
class UpdateChunk:
    """One contiguous edit region in an UpdateFile hunk."""

    context_header: str | None  # text after "@@ " (None if just "@@" or no marker)
    old_lines: list[str]  # context + removed lines (the search pattern)
    new_lines: list[str]  # context + added lines (the replacement)
    is_eof: bool = False  # chunk anchored at file end


@dataclass
class AddFileHunk:
    path: str
    content: str  # already includes trailing newlines


@dataclass
class DeleteFileHunk:
    path: str


@dataclass
class UpdateFileHunk:
    path: str
    move_to: str | None
    chunks: list[UpdateChunk] = field(default_factory=list)


Hunk = AddFileHunk | DeleteFileHunk | UpdateFileHunk


# ── Parser ───────────────────────────────────────────────


def parse_patch(text: str) -> list[Hunk]:
    """Parse patch text into hunks. Raises PatchParseError on malformed input."""
    # Lenient cleanup for deepseek thinking-mode quirks:
    #   - trailing residue after `*** End Patch` (e.g. `</｜DSML｜parameter>`,
    #     `</parameter>`, or a concatenated second `*** Begin Patch`)
    #     → truncate at the first `*** End Patch`.
    #   - `*** Begin Patch` present but `*** End Patch` entirely missing
    #     (model forgot the closing marker)
    #     → strip trailing XML-ish residue and append the missing marker.
    end_pos = text.find(_END)
    if end_pos != -1:
        text = text[: end_pos + len(_END)]
    elif _BEGIN in text:
        text = re.sub(r"\s*</[^>]+>\s*$", "", text, flags=re.IGNORECASE)
        text = text.rstrip() + "\n" + _END

    lines = text.strip().split("\n")
    if len(lines) < 2:
        raise PatchParseError("Patch text too short")
    if lines[0].strip() != _BEGIN:
        raise PatchParseError(f"First line must be '{_BEGIN}', got: {lines[0]!r}")
    if lines[-1].strip() != _END:
        raise PatchParseError(f"Last line must be '{_END}', got: {lines[-1]!r}")

    body = lines[1:-1]
    hunks: list[Hunk] = []
    i = 0
    while i < len(body):
        line = body[i]
        if not line.strip():
            i += 1
            continue

        if line.startswith(_ADD):
            path = line[len(_ADD):].strip()
            i += 1
            content_lines: list[str] = []
            while i < len(body) and body[i].startswith("+"):
                content_lines.append(body[i][1:])
                i += 1
            content = "\n".join(content_lines)
            if content_lines:
                content += "\n"
            hunks.append(AddFileHunk(path=path, content=content))

        elif line.startswith(_DELETE):
            path = line[len(_DELETE):].strip()
            hunks.append(DeleteFileHunk(path=path))
            i += 1

        elif line.startswith(_UPDATE):
            path = line[len(_UPDATE):].strip()
            i += 1

            move_to: str | None = None
            if i < len(body) and body[i].startswith(_MOVE):
                move_to = body[i][len(_MOVE):].strip()
                i += 1

            chunks: list[UpdateChunk] = []
            while i < len(body) and not _is_hunk_header(body[i]):
                if not body[i].strip() and not chunks:
                    # skip blank lines before first chunk
                    i += 1
                    continue
                chunk, consumed = _parse_update_chunk(
                    body, i, allow_missing_context=not chunks
                )
                chunks.append(chunk)
                i += consumed

            if not chunks:
                raise PatchParseError(
                    f"Update File '{path}' must contain at least one chunk"
                )
            hunks.append(UpdateFileHunk(path=path, move_to=move_to, chunks=chunks))

        else:
            raise PatchParseError(
                f"Unexpected line in patch: {line!r}. "
                f"Expected one of: '{_ADD.strip()}', '{_DELETE.strip()}', '{_UPDATE.strip()}'"
            )

    if not hunks:
        raise PatchParseError("Patch has no hunks")
    return hunks


def _is_hunk_header(line: str) -> bool:
    return line.startswith(_ADD) or line.startswith(_DELETE) or line.startswith(_UPDATE)


def _parse_update_chunk(
    lines: list[str], start: int, allow_missing_context: bool
) -> tuple[UpdateChunk, int]:
    """Parse one chunk starting at lines[start]. Returns (chunk, n_lines_consumed)."""
    i = start

    # Optional @@ context header
    context_header: str | None = None
    if lines[i] == _CONTEXT:
        context_header = None
        i += 1
    elif lines[i].startswith(_CONTEXT + " "):
        context_header = lines[i][len(_CONTEXT) + 1:].strip()
        i += 1
    elif lines[i].startswith(_CONTEXT):  # "@@something" without space
        context_header = lines[i][len(_CONTEXT):].strip() or None
        i += 1
    else:
        if not allow_missing_context:
            raise PatchParseError(
                f"Expected '@@' context marker at line {i}, got: {lines[i]!r}"
            )

    old_lines: list[str] = []
    new_lines: list[str] = []
    is_eof = False
    parsed_change_lines = 0

    while i < len(lines):
        line = lines[i]

        if line.startswith(_EOF):
            if parsed_change_lines == 0:
                raise PatchParseError(
                    f"Chunk at line {start} is empty (no +/-/space lines)"
                )
            is_eof = True
            i += 1
            break

        if _is_hunk_header(line):
            break

        # New chunk (another @@)
        if (line == _CONTEXT or line.startswith(_CONTEXT + " ")) and parsed_change_lines > 0:
            break

        if line.startswith("+"):
            new_lines.append(line[1:])
            parsed_change_lines += 1
        elif line.startswith("-"):
            old_lines.append(line[1:])
            parsed_change_lines += 1
        elif line.startswith(" "):
            old_lines.append(line[1:])
            new_lines.append(line[1:])
            parsed_change_lines += 1
        elif line == "":
            # blank line — treat as space-prefix context (LLM sometimes drops the space)
            old_lines.append("")
            new_lines.append("")
            parsed_change_lines += 1
        else:
            # Unrecognized line — terminate chunk (Codex behavior)
            if parsed_change_lines == 0:
                raise PatchParseError(
                    f"Unexpected line in chunk: {line!r}. "
                    "Every line must start with ' ' (context), '+' (add), or '-' (remove)"
                )
            break

        i += 1

    if parsed_change_lines == 0:
        raise PatchParseError(f"Chunk at line {start} is empty")

    return (
        UpdateChunk(
            context_header=context_header,
            old_lines=old_lines,
            new_lines=new_lines,
            is_eof=is_eof,
        ),
        i - start,
    )


# ── Seek (lenient context matching) ──────────────────────


def _seek_sequence(
    file_lines: list[str], pattern: list[str], start: int, eof: bool
) -> int | None:
    """Locate `pattern` in `file_lines` starting at `start`.

    Tries decreasing strictness: exact match → rstrip → trim both sides.
    If `eof=True`, prefers matching at file end.
    Returns the index of the first match, or None if not found.
    """
    if not pattern:
        return start
    if len(pattern) > len(file_lines):
        return None

    search_start = (
        len(file_lines) - len(pattern)
        if eof and len(file_lines) >= len(pattern)
        else start
    )
    last_idx = len(file_lines) - len(pattern)

    # 1) exact
    for i in range(search_start, last_idx + 1):
        if file_lines[i : i + len(pattern)] == pattern:
            return i
    # 2) rstrip
    for i in range(search_start, last_idx + 1):
        if all(
            file_lines[i + j].rstrip() == p.rstrip() for j, p in enumerate(pattern)
        ):
            return i
    # 3) full trim
    for i in range(search_start, last_idx + 1):
        if all(
            file_lines[i + j].strip() == p.strip() for j, p in enumerate(pattern)
        ):
            return i
    return None


# ── Applier ──────────────────────────────────────────────


def _apply_update_chunks(
    file_lines: list[str], chunks: list[UpdateChunk]
) -> list[str]:
    """Apply chunks to file_lines, returning the new file_lines.

    Chunks are applied in order; each chunk's context must occur strictly
    after the previous chunk's context (Codex contract).
    """
    new_lines: list[str] = []
    cursor = 0  # position in file_lines we've copied from up to

    for chunk_idx, chunk in enumerate(chunks):
        idx = _seek_sequence(file_lines, chunk.old_lines, cursor, chunk.is_eof)
        if idx is None:
            preview = "\n".join(chunk.old_lines[:6])
            raise PatchParseError(
                f"Chunk {chunk_idx + 1}: could not locate context. "
                f"Looking for:\n{preview}"
            )
        # Copy everything between cursor and idx unchanged
        new_lines.extend(file_lines[cursor:idx])
        # Insert the chunk's new_lines
        new_lines.extend(chunk.new_lines)
        cursor = idx + len(chunk.old_lines)

    # Trailing unchanged region
    new_lines.extend(file_lines[cursor:])
    return new_lines


def apply_patch_to_text(
    files: dict[str, str],
    patch_text: str,
    allowed_paths: set[str] | None = None,
) -> dict[str, str]:
    """In-memory variant of apply_patch — operates on a {path: text} dict.

    Used by maintain_model agent: procedural.md / semantic.md aren't real
    files on disk, they're DB-stored strings. We expose them to the agent
    as virtual filenames so it can use the same Codex apply_patch DSL it
    already knows from harvest.

    Args:
        files: current {virtual_path: text} contents.
        patch_text: standard Codex apply_patch DSL.
        allowed_paths: if set, restricts which paths the patch may touch.
                       Returns/raises if patch references a path not in this set.

    Returns:
        New {path: text} dict reflecting all hunks applied.

    Raises:
        PatchParseError on malformed input, missing file, or disallowed path.
    """
    hunks = parse_patch(patch_text)
    result = dict(files)

    for hunk in hunks:
        if isinstance(hunk, UpdateFileHunk):
            path = hunk.path
            if allowed_paths is not None and path not in allowed_paths:
                raise PatchParseError(
                    f"Path '{path}' not allowed. Permitted: {sorted(allowed_paths)}"
                )
            if path not in result:
                raise PatchParseError(f"Update File: '{path}' does not exist")
            current = result[path]
            file_lines = current.split("\n")
            new_lines = _apply_update_chunks(file_lines, hunk.chunks)
            new_content = "\n".join(new_lines)

            if hunk.move_to:
                if allowed_paths is not None and hunk.move_to not in allowed_paths:
                    raise PatchParseError(
                        f"Move to '{hunk.move_to}' not allowed. "
                        f"Permitted: {sorted(allowed_paths)}"
                    )
                del result[path]
                result[hunk.move_to] = new_content
            else:
                result[path] = new_content

        elif isinstance(hunk, AddFileHunk):
            if allowed_paths is not None and hunk.path not in allowed_paths:
                raise PatchParseError(
                    f"Add File '{hunk.path}' not allowed. "
                    f"Permitted: {sorted(allowed_paths)}"
                )
            if hunk.path in result:
                raise PatchParseError(f"Add File: '{hunk.path}' already exists")
            result[hunk.path] = hunk.content

        elif isinstance(hunk, DeleteFileHunk):
            if allowed_paths is not None and hunk.path not in allowed_paths:
                raise PatchParseError(
                    f"Delete File '{hunk.path}' not allowed. "
                    f"Permitted: {sorted(allowed_paths)}"
                )
            if hunk.path not in result:
                raise PatchParseError(f"Delete File: '{hunk.path}' does not exist")
            del result[hunk.path]

    return result


def _apply_one_hunk(hunk: Hunk, workspace: Path) -> tuple[str, str]:
    """Apply one hunk. Returns (action, path_str) for summary, raises on error."""
    if isinstance(hunk, AddFileHunk):
        target = (workspace / hunk.path).resolve()
        _verify_within(target, workspace)
        if target.exists():
            raise PatchParseError(f"Add File: '{hunk.path}' already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(hunk.content, encoding="utf-8")
        return ("Added", hunk.path)

    if isinstance(hunk, DeleteFileHunk):
        target = (workspace / hunk.path).resolve()
        _verify_within(target, workspace)
        if not target.exists():
            raise PatchParseError(f"Delete File: '{hunk.path}' does not exist")
        target.unlink()
        return ("Deleted", hunk.path)

    if isinstance(hunk, UpdateFileHunk):
        source = (workspace / hunk.path).resolve()
        _verify_within(source, workspace)
        if not source.exists():
            raise PatchParseError(f"Update File: '{hunk.path}' does not exist")
        original = source.read_text(encoding="utf-8")
        file_lines = original.split("\n")
        # split("\n") on a string ending with "\n" leaves a trailing "" — preserve
        new_lines = _apply_update_chunks(file_lines, hunk.chunks)
        new_content = "\n".join(new_lines)

        if hunk.move_to:
            dest = (workspace / hunk.move_to).resolve()
            _verify_within(dest, workspace)
            if dest.exists() and dest != source:
                raise PatchParseError(
                    f"Move to: '{hunk.move_to}' already exists"
                )
            source.unlink()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(new_content, encoding="utf-8")
            return ("Renamed", f"{hunk.path} → {hunk.move_to}")
        else:
            source.write_text(new_content, encoding="utf-8")
            return ("Modified", hunk.path)

    raise PatchParseError(f"Unknown hunk type: {type(hunk).__name__}")


def _verify_within(target: Path, workspace: Path) -> None:
    """Ensure target path is inside workspace (no .. escape)."""
    try:
        target.relative_to(workspace.resolve())
    except ValueError:
        raise PatchParseError(
            f"Path escapes workspace: {target}"
        ) from None


# ── Tool handler ─────────────────────────────────────────


async def handle(ctx: Any, **kwargs: Any) -> str:
    """Apply a patch to the workspace. Returns a human-readable summary."""
    patch_text: str = kwargs.get("patch", "")
    if not patch_text.strip():
        return "Error: empty patch"

    domain = getattr(ctx, "_domain", "unknown")
    workspace = Config.run_dir(domain) / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    workspace_resolved = workspace.resolve()

    try:
        hunks = parse_patch(patch_text)
    except PatchParseError as e:
        logger.warning(f"apply_patch parse error: {e}")
        return f"Patch parse error: {e}"

    results: list[tuple[str, str]] = []
    try:
        for hunk in hunks:
            action, path = _apply_one_hunk(hunk, workspace_resolved)
            results.append((action, path))
    except PatchParseError as e:
        logger.warning(f"apply_patch apply error: {e}")
        # Note: some hunks may have already applied — Codex behavior matches this
        applied = "\n".join(f"  {a}: {p}" for a, p in results)
        return f"Patch error: {e}\nApplied before failure:\n{applied}" if results else f"Patch error: {e}"

    summary = "\n".join(f"  {action}: {path}" for action, path in results)
    return f"Patch applied:\n{summary}"
