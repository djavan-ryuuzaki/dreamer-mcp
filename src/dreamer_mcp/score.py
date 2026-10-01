"""Sheet music (PDF) from ABC notation, rendered locally: abcm2ps (ABC -> PostScript) and
Ghostscript (PostScript -> PDF). Both are Debian packages (`apt install abcm2ps ghostscript`,
already in the Docker image); without them scores are skipped with a note."""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
import unicodedata
from pathlib import Path

TIMEOUT = 60  # seconds per external command


class ScoreError(Exception):
    """The score could not be rendered."""


def tools() -> tuple[str, str] | None:
    """(abcm2ps, ghostscript) executables, or None when either is missing."""
    abcm2ps = shutil.which("abcm2ps")
    gs = shutil.which("gs") or shutil.which("gswin64c") or shutil.which("gswin32c")
    return (abcm2ps, gs) if abcm2ps and gs else None


def title(abc: str) -> str:
    m = re.search(r"^T:(.*)$", abc, re.MULTILINE)
    return m.group(1).strip() if m else ""


def slug(text: str, default: str = "score") -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or default


def prepare(abc: str) -> str:
    """Make a tune abcm2ps accepts: starts with X:, UTF-8, no leading junk."""
    text = abc.replace("\r\n", "\n").strip()
    start = re.search(r"^X:", text, re.MULTILINE)
    if start:
        text = text[start.start():]
    else:
        text = "X:1\n" + text
    if not re.search(r"^K:", text, re.MULTILINE):
        raise ScoreError("ABC has no K: (key) line")
    return "%abc-2.1\n%%encoding utf-8\n" + text + "\n"


async def _run(*cmd: str, cwd: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), TIMEOUT)
    except TimeoutError as e:
        proc.kill()
        raise ScoreError(f"{Path(cmd[0]).name} timed out") from e
    text = out.decode(errors="replace")
    if proc.returncode not in (0, None) and Path(cmd[0]).stem.startswith("gs"):
        raise ScoreError(f"ghostscript failed: {text.strip()[-300:]}")
    return text


async def render_pdf(abc: str) -> bytes:
    """ABC text -> PDF bytes. Raises ScoreError."""
    found = tools()
    if found is None:
        raise ScoreError("sheet music needs abcm2ps and ghostscript on the MCP server "
                         "(apt install abcm2ps ghostscript)")
    abcm2ps, gs = found
    try:
        workdir = tempfile.TemporaryDirectory(prefix="dreamer-score-")
    except OSError as e:
        raise ScoreError(f"no writable temp dir ({e}); with a read-only root filesystem mount "
                         "an emptyDir at /tmp") from e
    with workdir as tmp:
        Path(tmp, "score.abc").write_text(prepare(abc), encoding="utf-8")
        log = await _run(abcm2ps, "-q", "-O", "score.ps", "score.abc", cwd=tmp)
        ps = Path(tmp, "score.ps")
        if not ps.is_file() or ps.stat().st_size == 0:
            raise ScoreError(f"abcm2ps produced no output: {log.strip()[-300:]}")
        await _run(gs, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
                   "-sPAPERSIZE=a4", "-sOutputFile=score.pdf", "score.ps", cwd=tmp)
        pdf = Path(tmp, "score.pdf")
        if not pdf.is_file() or pdf.stat().st_size == 0:
            raise ScoreError("ghostscript produced no PDF")
        return pdf.read_bytes()
