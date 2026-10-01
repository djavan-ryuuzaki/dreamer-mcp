"""Rewrite ABC notation into the dialect YuE2 was trained on.

YuE2 does not parse ABC: YuE2GenerateMusic tokenizes the text and feeds it to the model as part
of its prompt. So a score only steers the music when it looks like what YuE2GenerateABC and
SheetSage2AudioToABC produce:

    X:1
    T:
    M:4/4
    L:1/16
    Q:1/4=120
    V: Vocal clef=treble name="Vocal Melody" snm="Vocal"
    V: Ins clef=treble name="Ins Melody" snm="Inst."
    K:C
    % verse
    V: Vocal
    "F"z4e2g2g2e2e2d2|"C"d4e4z8|"G"z4d2d2d2c2d2c2|"Am"e4z12|
    V: Ins
    Z4|

i.e. two voices interleaved every (up to) 4 bars, the sung melody in "Vocal" (chords quoted at
the start of bars), the instrumental melody in "Ins" (Z<n> = n bars of rest), sections as plain
"% verse" / "% chorus" comments, and no lyrics (they go to the model separately). ABC written by
an LLM usually has a single voice, L:1/8, w: lyric lines and %%text / P: section markers, which
the model has never seen. `to_yue2` converts that kind of score.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from fractions import Fraction

UNIT = Fraction(1, 16)
BARS_PER_LINE = 4
VOICES = (
    'V: Vocal clef=treble name="Vocal Melody" snm="Vocal"',
    'V: Ins clef=treble name="Ins Melody" snm="Inst."',
)
SECTIONS = {
    # canonical name: keywords (lowercase, accents stripped) that start a section label
    "pre-chorus": ("pre-chorus", "pre chorus", "prechorus", "pre-refrao", "pre refrao",
                   "prerefrao", "pre-coro", "pre coro", "build"),
    "chorus": ("chorus", "refrao", "refrain", "coro", "estribillo", "hook"),
    "verse": ("verse", "verso", "estrofe", "estrofa", "rap"),
    "bridge": ("bridge", "ponte", "puente", "middle 8"),
    "intro": ("intro", "introducao", "introduccion", "abertura"),
    "interlude": ("interlude", "interludio", "instrumental", "solo", "break", "riff", "drop"),
    "outro": ("outro", "final", "fim", "ending", "coda", "tag", "fade"),
}
VOCAL_SECTIONS = {"verse", "pre-chorus", "chorus", "bridge"}
INS_VOICE = re.compile(r"ins|inst|acc|comp|piano|keys|guit|gtr|viol|cava|banjo|bass|baix|synth|"
                       r"harm|chord|acorde|counter|contra|back|riff|pad|string|horn|sax",
                       re.IGNORECASE)
LYRIC_TAG = re.compile(r"^\s*\[([^\]]+)\]\s*$", re.MULTILINE)

_TOKEN = re.compile(
    r'(?P<chord>"[^"]*")'
    r"|(?P<deco>![^!]*!|\+[^+\s]*\+)"
    r"|(?P<grace>\{[^}]*\})"
    r"|(?P<field>\[[A-Za-z]:[^\]]*\])"
    r"|(?P<tuplet>\((?P<p>\d)(?::(?P<q>\d*))?(?::(?P<r>\d*))?)"
    r"|(?P<multi>Z(?P<mbars>\d*))"
    r"|(?P<note>(?:\[[^\]]+\]|[=^_]*[A-Ga-g][',]*|[zx]))(?P<len>\d*(?:/\d*)*)"
    r"|(?P<tie>-)"
    r"|(?P<broken>[<>]{1,3})"
    r"|(?P<other>.)",
    re.DOTALL,
)
_BAR = re.compile(r"\[\||\|\]|\|\||:\|\]?|\|:|::|\|(?:\d)?|\[\d")


class AbcError(ValueError):
    """The text is not ABC notation that can be converted."""


@dataclass
class _Section:
    name: str
    label: str
    bars: list[list] = field(default_factory=list)
    ins: list[list] = field(default_factory=list)
    """Bars of the second (instrumental) voice, when the score has one."""
    lyrics: bool = False


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c))


def section_name(label: str) -> str | None:
    """'REFRÃO (repete igual)' -> 'chorus'; None when the label is not a known section."""
    low = _strip_accents(label).lower().strip(" :-#[]()")
    low = re.sub(r"^(\d+\s*[.)-]?\s*)", "", low)
    best = None
    for name, keys in SECTIONS.items():
        for key in keys:
            if low.startswith(key) and (best is None or len(key) > best[1]):
                best = (name, len(key))
    return best[0] if best else None


def _duration(text: str) -> Fraction:
    if not text:
        return Fraction(1)
    m = re.fullmatch(r"(\d*)((?:/\d*)*)", text)
    value = Fraction(int(m.group(1))) if m.group(1) else Fraction(1)
    for part in re.findall(r"/(\d*)", m.group(2)):
        value /= int(part) if part else 2
    return value


def _meter(text: str) -> Fraction:
    text = text.strip()
    if text in ("C", "C|", ""):
        return Fraction(1)
    m = re.match(r"(\d+)\s*/\s*(\d+)", text)
    return Fraction(int(m.group(1)), int(m.group(2))) if m else Fraction(1)


def _tempo(text: str) -> str | None:
    """'1/4=100', '"Allegro" 1/8=200', '100' -> '1/4=<bpm>'."""
    m = re.search(r"(\d+)/(\d+)\s*=\s*(\d+(?:\.\d+)?)", text)
    if m:
        bpm = float(m.group(3)) * Fraction(int(m.group(1)), int(m.group(2))) / Fraction(1, 4)
    elif m := re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", text):
        bpm = float(m.group(1))
    else:
        return None
    return f"1/4={round(bpm)}"


def _key(text: str) -> str:
    key = text.strip().split()[0] if text.strip() else "C"
    return "C" if key.lower() == "none" else key


class _BarParser:
    """Turns the text of one bar into events: (kind, text, duration) with kind note/chord/tie."""

    def __init__(self, unit: Fraction, warnings: list[str]):
        self.unit = unit
        self.warnings = warnings

    def parse(self, text: str) -> tuple[list[list], int]:
        events: list[list] = []
        rest_bars = 0
        broken: Fraction | None = None
        tuplet: list | None = None  # [notes left, factor]
        for m in _TOKEN.finditer(text):
            if m.group("chord"):
                events.append(["chord", m.group("chord"), Fraction(0)])
            elif m.group("tuplet"):
                p = int(m.group("p"))
                q = int(m.group("q")) if m.group("q") else {2: 3, 3: 2, 4: 3, 6: 2}.get(p, 2)
                r = int(m.group("r")) if m.group("r") else p
                tuplet = [r, Fraction(q, p)]
            elif m.group("multi"):
                rest_bars += int(m.group("mbars") or 1)
            elif m.group("note"):
                dur = _duration(m.group("len")) * self.unit
                if tuplet:
                    dur *= tuplet[1]
                    tuplet[0] -= 1
                    if tuplet[0] <= 0:
                        tuplet = None
                if broken is not None:
                    dur *= broken
                    broken = None
                events.append(["note", m.group("note"), dur])
            elif m.group("tie"):
                events.append(["tie", "-", Fraction(0)])
            elif m.group("broken"):
                prev = next((e for e in reversed(events) if e[0] == "note"), None)
                n = len(m.group("broken"))
                short = Fraction(1, 2 ** n)
                long_ = 2 - short
                if prev is None:
                    continue
                if m.group("broken")[0] == ">":
                    prev[2] *= long_
                    broken = short
                else:
                    prev[2] *= short
                    broken = long_
            elif m.group("field") and not m.group("field").startswith("[P:"):
                self.warnings.append(f"inline field {m.group('field')} ignored")
            elif (other := m.group("other")) and not other.isspace() and other not in "`.":
                self.warnings.append(f"unknown symbol {m.group('other')!r} ignored")
        return events, rest_bars


def _render(events: list[list], measure: Fraction, warnings: list[str], label: str,
            pickup: bool, chords: bool = True) -> str:
    total = sum((e[2] for e in events), Fraction(0))
    if total < measure:
        pad = ["note", "z", measure - total]
        if pickup:  # an anacrusis: the rest goes before the notes (after a leading chord)
            at = 1 if events and events[0][0] == "chord" else 0
            events = [*events[:at], pad, *events[at:]]
        else:
            events = [*events, pad]
    elif total > measure:
        warnings.append(f"{label}: bar is {total / UNIT}/16 long, expected {measure / UNIT}")
    out = []
    for kind, text, dur in events:
        if kind == "chord":
            if chords:
                out.append(text)
        elif kind == "tie":
            out.append("-")
        else:
            units = dur / UNIT
            n = round(units)
            if units != n:
                warnings.append(f"{label}: duration {units}/16 rounded to {max(n, 1)}/16")
            n = max(n, 1)
            out.append(text + ("" if n == 1 else str(n)))
    return "".join(out)


def is_yue2(abc: str) -> bool:
    return bool(re.search(r"^V:\s*Vocal\b", abc, re.MULTILINE)
                and re.search(r"^V:\s*Ins\b", abc, re.MULTILINE)
                and re.search(r"^L:\s*1/(16|32)\s*$", abc, re.MULTILINE))


def _clean_yue2(abc: str) -> tuple[str, list[str]]:
    keep, dropped = [], 0
    for line in abc.strip().splitlines():
        if re.match(r"^\s*(w:|W:|%%)", line):
            dropped += 1
            continue
        keep.append(line.rstrip())
    warnings = [f"removed {dropped} lyric/directive lines"] if dropped else []
    return "\n".join(keep) + "\n", warnings


def to_yue2(abc: str, lyrics: str | None = None) -> tuple[str, list[str]]:
    """Returns (abc in YuE2's dialect, warnings). Scores already in that dialect are only
    cleaned (lyric lines and %% directives removed)."""
    if not abc or not abc.strip():
        raise AbcError("empty ABC")
    if is_yue2(abc):
        result, warnings = _clean_yue2(abc)
    else:
        result, warnings = _convert(abc)
    if lyrics:
        warnings.extend(check_sections(result, lyrics))
    return result, warnings


def _convert(abc: str) -> tuple[str, list[str]]:
    warnings: list[str] = []
    header: dict[str, str] = {}
    in_body = False
    voice: str | None = None
    roles: dict[str, str | None] = {}  # voice name -> "bars" (sung melody) / "ins" / None
    sections: list[_Section] = []
    pending = {"bars": "", "ins": ""}  # bar text continued on the next line
    repeat_start: dict[str, int | None] = {}

    def role(name: str) -> str | None:
        if name not in roles:
            taken = set(roles.values())
            wanted = ["ins", "bars"] if INS_VOICE.search(name) else ["bars", "ins"]
            roles[name] = next((r for r in wanted if r not in taken), None)
            if roles[name] is None:
                warnings.append(f"voice {name!r} ignored (only a melody and an instrumental "
                                "voice are used)")
        return roles[name]

    def current() -> _Section:
        if not sections:
            sections.append(_Section("verse", ""))
        return sections[-1]

    def start(label: str) -> None:
        name = section_name(label)
        if name is None:
            warnings.append(f"unknown section {label.strip()!r}, kept as a verse")
            name = "verse"
        sections.append(_Section(name, label.strip()))
        repeat_start.clear()

    unit = Fraction(1, 8)
    parser = _BarParser(unit, warnings)
    for raw in abc.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if m := re.match(r"^%%\s*(?:text|section|part|center)\s+(.*)$", line, re.IGNORECASE):
            start(m.group(1))
            continue
        if line.startswith("%"):
            label = line.lstrip("%").strip()
            if label and section_name(label):
                start(label)
            continue
        if m := re.match(r"^([A-Za-z]):(.*)$", line):
            key, value = m.group(1), m.group(2).strip()
            if key in "wW":
                if in_body:
                    current().lyrics = True
                continue
            if key == "P" and in_body:
                start(value)
                continue
            if key == "T" and in_body:
                if section_name(value):
                    start(value)
                continue
            if key == "V":
                voice = value.split()[0] if value else ""
                role(voice)
                continue
            if key == "L":
                try:
                    unit = parser.unit = Fraction(value)
                except ValueError:
                    warnings.append(f"invalid L:{value} ignored")
            elif in_body and key in "MKQ":
                warnings.append(f"{key}: change inside the tune ignored")
            if not in_body:
                header[key] = value
            in_body = in_body or key == "K"
            continue
        if not in_body:
            continue
        target = role(voice) if voice is not None else "bars"
        if target is None:
            continue
        line = re.sub(r"%.*$", "", line)
        line = line.removesuffix("\\")
        text = pending[target] + line
        pos = 0
        for m in _BAR.finditer(text):
            bar_text = text[pos:m.start()]
            pos = m.end()
            sym = m.group(0)
            bars = getattr(current(), target)
            if bar_text.strip():
                events, rests = parser.parse(bar_text)
                if events:
                    bars.append(events)
                bars.extend([[]] * rests)
            if sym in ("|:", "::"):
                repeat_start[target] = len(bars)
            if sym.startswith(":|") or sym == "::":
                bars.extend(bars[repeat_start.get(target) or 0:])
                repeat_start[target] = len(bars) if sym == "::" else None
            if re.fullmatch(r"\|\d|\[\d|:\|\d", sym) or (sym.startswith(":|") and len(sym) > 2
                                                        and sym[-1].isdigit()):
                warnings.append("numbered repeat endings ([1 / [2) are not supported; "
                                "write the bars out instead")
        pending[target] = text[pos:]
    for target, text in pending.items():
        if text.strip():
            events, rests = parser.parse(text)
            bars = getattr(current(), target)
            if events:
                bars.append(events)
            bars.extend([[]] * rests)

    sections = [s for s in sections if s.bars or s.ins]
    if not sections:
        raise AbcError("no bars found (is this ABC notation?)")

    measure = _meter(header.get("M", "4/4"))
    has_lyrics = any(s.lyrics for s in sections)
    tempo = _tempo(header.get("Q", "")) or "1/4=120"
    lines = [
        "X:1",
        f"T:{header.get('T', '')}",
        f"M:{header.get('M', '4/4') or '4/4'}",
        "L:1/16",
        f"Q:{tempo}",
        *VOICES,
        f"K:{_key(header.get('K', 'C'))}",
    ]
    full = round(measure / UNIT)
    for index, section in enumerate(sections):
        vocal = section.lyrics if has_lyrics else section.name in VOCAL_SECTIONS
        lines.append(f"% {section.name}")
        total = max(len(section.bars), len(section.ins))
        for start_bar in range(0, total, BARS_PER_LINE):
            count = min(BARS_PER_LINE, total - start_bar)
            label = section.label or section.name
            first = index == 0 and start_bar == 0

            def render(bars: list[list], chords: bool, start: int = start_bar,
                       label: str = label, first: bool = first) -> list[str]:
                return [_render(e, measure, warnings, f"{label}, bar {start + i + 1}",
                                first and i == 0, chords)
                        for i, e in enumerate(bars)]

            melody = _chunk(section.bars, start_bar, count)
            ins = _chunk(section.ins, start_bar, count)
            if vocal:
                ins_line = "|".join(render(ins, False)) if _has_notes(ins) else f"Z{count}"
                lines += ["V: Vocal", "|".join(render(melody, True)) + "|",
                          "V: Ins", ins_line + "|"]
            else:  # chords stay on a resting Vocal line, the lead line goes to Ins
                chords = [next((t for k, t, _ in m + i if k == "chord"), "")
                          for m, i in zip(melody, ins, strict=True)]
                lead = melody if _has_notes(melody) else ins
                lines += ["V: Vocal", "|".join(f"{c}z{full}" for c in chords) + "|",
                          "V: Ins", "|".join(render(lead, False)) + "|"]
    if not has_lyrics and not any(s.name in VOCAL_SECTIONS for s in sections):
        warnings.append("no w: lyric lines nor verse/chorus sections: everything was treated "
                        "as instrumental")
    return "\n".join(lines) + "\n", _unique(warnings)


def _chunk(bars: list[list], start: int, count: int) -> list[list]:
    part = bars[start:start + count]
    return part + [[]] * (count - len(part))


def _has_notes(bars: list[list]) -> bool:
    return any(k == "note" and t not in ("z", "x") for bar in bars for k, t, _ in bar)


def _unique(items: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    for item in items:
        seen[item] = seen.get(item, 0) + 1
    return [k if n == 1 else f"{k} (x{n})" for k, n in seen.items()]


def lyric_sections(lyrics: str) -> list[str]:
    """Canonical names of the [Section] tags in YuE-style lyrics."""
    return [section_name(tag) or "verse" for tag in LYRIC_TAG.findall(lyrics)]


def abc_vocal_sections(abc: str) -> list[str]:
    """Sections of a YuE2-dialect score whose Vocal voice has notes (not only rests)."""
    result: list[str] = []
    name, sung, voice = None, False, None

    def close() -> None:
        if name and sung:
            result.append(name)

    for line in abc.splitlines():
        line = line.strip()
        if line.startswith("%") and not line.startswith("%%"):
            close()
            name, sung = section_name(line.lstrip("% ")) or line.lstrip("% "), False
        elif m := re.match(r"^V:\s*(\w+)", line):
            voice = m.group(1)
        elif voice == "Vocal" and name and re.search(r"[A-Ga-g]", re.sub(r'"[^"]*"', "", line)):
            sung = True
    close()
    return result


def check_sections(abc: str, lyrics: str) -> list[str]:
    """Warn when the sung sections of the score don't follow the [Section] tags of the lyrics."""
    tags = [t for t in lyric_sections(lyrics) if t in VOCAL_SECTIONS]
    sung = [s for s in abc_vocal_sections(abc) if s in VOCAL_SECTIONS]
    if not tags or tags == sung:
        return []
    return [(f"sung sections of the ABC ({', '.join(sung) or 'none'}) differ from the lyric "
             f"tags ({', '.join(tags)}); YuE2 follows the ABC structure, keep them in the same "
             "order")]


def strip_chords(abc: str) -> str:
    """Remove chord symbols from the music lines (melody mode scores have none)."""
    return "\n".join(line if re.match(r"^[A-Za-z]:", line) else re.sub(r'"[^"]*"', "", line)
                     for line in abc.splitlines()) + "\n"


def for_yue2(abc: str, lyrics: str | None = None, mode: str | None = None
             ) -> tuple[str, list[str]]:
    """to_yue2 plus the mode: "melody" scores carry no chord symbols, "full" ones do."""
    result, warnings = to_yue2(abc, lyrics)
    has_chords = any('"' in line for line in result.splitlines()
                     if not re.match(r"^[A-Za-z]:", line))
    if mode == "melody" and has_chords:
        result = strip_chords(result)
    elif mode == "full" and not has_chords:
        warnings.append('mode "full" expects chord symbols ("G", "Em7"...) at the start of bars')
    return result, warnings
