from pathlib import Path

import httpx
import pytest

from dreamer_mcp import abc
from dreamer_mcp.client import ComfyClient
from dreamer_mcp.config import Settings
from dreamer_mcp.workflows import WorkflowError, WorkflowRegistry, build_graph

REPO_WORKFLOWS = Path(__file__).parent.parent / "workflows"

LLM_ABC = """\
X:1
T:Run Into the Sunlight
C:pop sambante
M:4/4
L:1/8
Q:1/4=100
K:G
%%text INTRO
| "Gmaj7"z b d>d' d' e' d' b | "Em7"z g b>e' b g e2 |
%%text VERSO 1
| "Gmaj7"z2 d'2 d'>e' b2 | "Em7"c'2 b2 a2 g2 |
w: Morn- ing light a- cross the win- dow
| "Am7"z2 b2 a2 g2 | "D7"f2 a2 d'2 a2 |
w: Ci- ty wa- king down be- low
| "Gmaj7"z2 c'2 c'>d' e'2 |]
w: I can hear
%%text REFRÃO (repete igual)
| "Cmaj7"e'2 e'2 d'2 c'2 | "D7"b2 c'2 d'2 b2 |]
w: Run with me in- to the sun- light
"""

LYRICS = "[Verse]\nMorning light\n\n[Chorus]\nRun with me\n"


def test_llm_abc_becomes_two_voice_yue2_score():
    out, warnings = abc.to_yue2(LLM_ABC, LYRICS)
    assert warnings == []
    assert out.splitlines()[:8] == [
        "X:1", "T:Run Into the Sunlight", "M:4/4", "L:1/16", "Q:1/4=100", *abc.VOICES, "K:G"]
    assert "C:" not in out and "w:" not in out and "%%" not in out
    body = out.split("K:G\n", 1)[1]
    # Instrumental intro: chords stay on a resting Vocal line, notes move to Ins (L:1/8 -> 1/16,
    # d>d' = dotted eighth + sixteenth).
    assert body.startswith(
        '% intro\nV: Vocal\n"Gmaj7"z16|"Em7"z16|\nV: Ins\nz2b2d3d\'d\'2e\'2d\'2b2|z2g2b3e\'b2g2e4|\n')
    # Sung sections: 4 bars per line, Ins resting.
    assert ('% verse\nV: Vocal\n"Gmaj7"z4d\'4d\'3e\'b4|"Em7"c\'4b4a4g4|"Am7"z4b4a4g4|'
            '"D7"f4a4d\'4a4|\nV: Ins\nZ4|\nV: Vocal\n"Gmaj7"z4c\'4c\'3d\'e\'4|\nV: Ins\nZ1|\n'
            ) in body
    assert body.endswith('% chorus\nV: Vocal\n"Cmaj7"e\'4e\'4d\'4c\'4|"D7"b4c\'4d\'4b4|\n'
                         "V: Ins\nZ2|\n")
    assert abc.abc_vocal_sections(out) == ["verse", "chorus"]


def test_yue2_scores_pass_through():
    out, _ = abc.to_yue2(LLM_ABC)
    assert abc.to_yue2(out) == (out, [])
    noisy, warnings = abc.to_yue2(out.replace("Z4|\n", "Z4|\nw: la la\n", 1))
    assert noisy == out and warnings == ["removed 1 lyric/directive lines"]


def test_bar_lengths_pickup_and_repeats():
    out, warnings = abc.to_yue2("M:4/4\nL:1/4\nK:C\nP:Verse\nG | c d e f | g4 a |\n"
                                "|: c4 :|\nw: x\n")
    assert '% verse\nV: Vocal\nz12G4|c4d4e4f4|g16a4|c16|\nV: Ins\nZ4|\nV: Vocal\nc16|' in out
    assert warnings == ["Verse, bar 3: bar is 20/16 long, expected 16"]


def test_sections_against_lyrics_and_modes():
    out, _ = abc.to_yue2(LLM_ABC)
    assert abc.check_sections(out, "[Verse]\na\n[Chorus]\nb\n[Bridge]\nc") != []
    assert abc.check_sections(out, "[Verse 1]\na\n[Refrão]\nb") == []
    melody, _ = abc.for_yue2(LLM_ABC, mode="melody")
    assert '"Gmaj7"' not in melody and 'name="Vocal Melody"' in melody
    _, warnings = abc.for_yue2(melody, mode="full")
    assert any("chord symbols" in w for w in warnings)
    assert abc.section_name("TAG / FINAL (ad-libs livres)") == "outro"
    assert abc.section_name("Pré-Refrão") == "pre-chorus"
    assert abc.section_name("DROP (instrumental)") == "interlude"
    with pytest.raises(abc.AbcError):
        abc.to_yue2("just some text")


def _registry() -> WorkflowRegistry:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.startswith("/object_info/"):
            cls = req.url.path.rsplit("/", 1)[1]
            mode = ["COMBO", {"options": ["full", "melody"]}]
            return httpx.Response(200, json={cls: {"input": {"required": {"mode": mode}}}})
        return httpx.Response(404)

    client = ComfyClient(Settings(comfyui_url="http://comfy:8188"),
                         transport=httpx.MockTransport(handler))
    return WorkflowRegistry(client, REPO_WORKFLOWS, include_comfyui=False)


async def test_generate_music_normalizes_the_abc_param():
    reg = _registry()
    entry = await reg.get("generate_music")
    graph, applied, _ = await build_graph(reg, entry, {"prompt": "pop", "lyrics": LYRICS})
    assert graph["18"]["inputs"]["value"] is True  # [$GERAR_ABC]: YuE2 writes the score

    graph, applied, _ = await build_graph(
        reg, entry, {"prompt": "pop", "lyrics": "[Bridge]\nla", "abc": LLM_ABC})
    assert graph["18"]["inputs"]["value"] is False
    assert graph["19"]["inputs"]["value"] == applied["abc"] and "V: Ins" in applied["abc"]
    assert "lyric tags (bridge)" in applied["abc_warnings"][0]

    cover = await reg.get("generate_cover_music")
    graph, applied, _ = await build_graph(reg, cover, {"abc": LLM_ABC})  # workflow mode: melody
    assert '"' not in graph["24"]["inputs"]["value"].split("K:G", 1)[1]
    graph, applied, _ = await build_graph(reg, cover, {"abc": LLM_ABC, "mode": "full"})
    assert graph["8"]["inputs"]["mode"] == graph["19"]["inputs"]["mode"] == "full"
    assert '"Gmaj7"' in applied["abc"]
    assert graph["5"]["inputs"]["text"] == ["23", 0]  # Save Text gets the ABC, not the style

    with pytest.raises(WorkflowError, match="'abc': no bars found"):
        await build_graph(reg, entry, {"abc": "hello"})


def test_second_voice_becomes_the_ins_line():
    two_voices = """\
M:4/4
L:1/8
K:D
V:Voz
V:Cavaco
%%text INTRO
V:Voz
| "D"z8 |
V:Cavaco
| "D"f a f a f a f a |
%%text VERSO
V:Voz
| "D"F2 A2 B2 A2 | "G"z8 |
w: la la la la
V:Cavaco
| z8 | "G"z4 B d g b |
%%text REFRÃO
V:Voz
| "A"c2 e2 a4 |
w: oh oh oh
"""
    out, warnings = abc.to_yue2(two_voices)
    assert warnings == []
    assert "% intro\nV: Vocal\n\"D\"z16|\nV: Ins\nf2a2f2a2f2a2f2a2|\n" in out
    assert '% verse\nV: Vocal\n"D"F4A4B4A4|"G"z16|\nV: Ins\nz16|z8B2d2g2b2|\n' in out
    assert out.endswith('% chorus\nV: Vocal\n"A"c4e4a8|\nV: Ins\nZ1|\n')
    # a third voice is dropped with a warning
    _, warnings = abc.to_yue2(two_voices.replace("V:Cavaco\n| z8", "V:Bass\n| C8 |\nV:Cavaco\n| z8"))
    assert warnings == ["voice 'Bass' ignored (only a melody and an instrumental voice are used)"]
