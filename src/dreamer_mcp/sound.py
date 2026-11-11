"""Stable Audio 3 sounds: the prompt the model was trained on, and the file formats.

The caller writes the description (see SOUND_GUIDE, shown in the tool description); this module
only adds what the model expects around it: the AudioSparx tag of the category and the length.
ComfyUI saves flac/mp3/opus itself; wav/ogg/aiff are converted here from the flac it saved."""

from __future__ import annotations

import io
import re

CATEGORIES = ("music", "instrument", "sfx", "one_shot")
CATEGORY_TAGS = {
    # AudioSparx metadata tags, recommended by the Stable Audio 3 prompting guide.
    "music": "TrackType: Music, VocalType: Instrumental",
    "instrument": "TrackType: Instrument",
    "sfx": "TrackType: SFX",
    "one_shot": "TrackType: SFX",
}
MAX_SECONDS = 380.0  # Stable Audio 3 Medium: about 6m20s
MIN_SECONDS = 0.5

NATIVE_FORMATS = {"flac": None, "mp3": "V0", "opus": "192k"}  # SaveAudioAdvanced, -> quality
CONVERTED_FORMATS = {  # libsndfile (soundfile): format, subtype
    "wav": ("WAV", "PCM_16"),
    "ogg": ("OGG", "VORBIS"),
    "aiff": ("AIFF", "PCM_16"),
}
FORMATS = (*NATIVE_FORMATS, *CONVERTED_FORMATS)

SOUND_GUIDE = """Write `prompt` yourself, in English, from the idea you were given (there is no prompt
rewriter): Stable Audio 3 was trained on Freesound/AudioSparx audio and its metadata, so describe
the sound the way a sound library would catalog it, one to three dense sentences, specific words
over vague ones, no vocals or lyrics (it cannot sing). The category tag ("TrackType: ...") and
"Length: N seconds" are added for you; don't write them.
- music (instrumental track): genre/style (genres may be combined) and adjectives; the main
  instruments and how each sounds; supporting layers (pads, textures, field recordings); rhythm
  and percussion; mood and energy woven into the sentence; era or production (live-recorded,
  close-mic'd, tape warmth); always "BPM: X". Durations: dance 120-180 s, pop/rock 180-210 s,
  cinematic/ambient 240-300 s; jingles and lullabies 20-40 s.
  e.g. "Synthwave track with arpeggiated synth leads, analog pads, electric bass, punchy drum
  machine and gated reverb snares, nostalgic and vibrant. BPM: 110."
- instrument (an isolated instrument, stem or loop): the instrument; playing technique; material,
  timbre, texture; style or mood; room, mic and effects; always "BPM: X". Loops 6-20 s, stems
  20-180 s. Several instruments together: say "duo"/"ensemble".
  e.g. "Drum loop with deep muffled kick on beat one, snappy rimshot snare on two and four with
  ghost notes, tight closed hi-hats. BPM: 85."
- sfx (sound effects and ambiences): the exact source; its material and physical character; the
  space (indoor/outdoor, dry/reverberant, close/distant); how it evolves (attack, decay, build-up,
  movement, doppler, left-to-right). Impacts/clicks 1-3 s, actions 3-6 s, ambiences 6-15 s (or
  longer beds, up to a few minutes).
  e.g. "Wooden door creaking open slowly in an old house, echoing interior, eerie tone."
- one_shot (a single sample for music production or UI/game feedback): the source; the hit type
  (pluck, slam, tap, stab, riser); material and timbre; production (dry/wet, dead room, ribbon mic,
  reverb tail). 1-3 s, up to 11 s for pads/risers.
  e.g. "Pillow kick, acoustic kick drum muffled with a heavy blanket, short dry thump with almost
  zero resonance."
Shorter durations that fit the sound give better results. The output format: flac (lossless),
mp3, opus, or wav/ogg (Vorbis)/aiff for game engines and DAWs."""


def sound_prompt(prompt: str, category: str, duration: float) -> str:
    """The prompt sent to the model: the category tag, the caller's description, the length.
    A tag or length the caller already wrote is kept as is."""
    if category not in CATEGORY_TAGS:
        raise ValueError(f"Invalid category {category!r}. Options: {', '.join(CATEGORIES)}")
    text = prompt.strip()
    if not text:
        raise ValueError("'prompt' is empty: describe the sound")
    if not re.search(r"\bTrackType\s*:", text, re.IGNORECASE):
        text = f"{CATEGORY_TAGS[category]}, {text}"
    if not re.search(r"\bLength\s*:\s*\d", text, re.IGNORECASE):
        seconds = max(1, round(duration))
        if text[-1] not in ".!?":
            text += "."
        text += f" Length: {seconds} second{'s' if seconds != 1 else ''}"
    return text


def check_duration(duration: float) -> float:
    duration = float(duration)
    if not MIN_SECONDS <= duration <= MAX_SECONDS:
        raise ValueError(f"'duration' must be between {MIN_SECONDS:g} and {MAX_SECONDS:g} seconds")
    return duration


def set_save_format(node: dict, fmt: str) -> bool:
    """Point a SaveAudioAdvanced node at `fmt`. Returns True when ComfyUI can't write it, so the
    node saves flac and the file is converted afterwards (convert)."""
    if fmt not in FORMATS:
        raise ValueError(f"Invalid format {fmt!r}. Options: {', '.join(FORMATS)}")
    native = fmt in NATIVE_FORMATS
    inputs = node.setdefault("inputs", {})
    inputs["format"] = fmt if native else "flac"
    quality = NATIVE_FORMATS.get(fmt) if native else None
    if quality:
        inputs["format.quality"] = quality
    else:
        inputs.pop("format.quality", None)
    return not native


def convert(data: bytes, fmt: str) -> bytes:
    """Re-encode an audio file (flac/wav/ogg/mp3...) as `fmt` (a CONVERTED_FORMATS key)."""
    import soundfile  # numpy + libsndfile: imported only when a conversion is asked for

    container, subtype = CONVERTED_FORMATS[fmt]
    # int16: the targets are 16-bit (or lossy), and 380 s of stereo float64 would take ~270 MB
    samples, rate = soundfile.read(io.BytesIO(data), dtype="int16", always_2d=True)
    out = io.BytesIO()
    soundfile.write(out, samples, rate, format=container, subtype=subtype)
    return out.getvalue()
