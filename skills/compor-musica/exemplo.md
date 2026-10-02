# Exemplo completo: "Luz da Janela"

Conferido: cada compasso cantado tem tantas notas quanto sílabas no `w:`,
`comfyui_normalize_abc` não dá avisos e a partitura em PDF sai com título, autores, seções e a
letra sob as notas (inclusive o `_` na nota ligada do último compasso).

## `style`

```
pop acoustic, warm male vocal, piano, light drums, hopeful, 104 bpm
```

## `lyrics`

```
[Verse]
A manhã chamou meu nome baixo
e a janela abriu devagar o céu

[Chorus]
Luz da janela acorda meu coração
vem que a vida te chama
```

## `abc_notation`

```
X:1
T:Luz da Janela
C:Letra e música: Fulano de Tal
M:4/4
L:1/8
Q:1/4=104
K:G
V:Voz clef=treble name="Voz"
V:Piano clef=treble name="Piano"
%%text INTRO
V:Voz
z8|z8|z8|z8|
V:Piano
"G"G2B2d2B2|"D"A2d2f2d2|"Em"G2B2e2B2|"C"E2G2c2G2|
%%text VERSO 1
V:Voz
"G"z2 B B A G G2|"D"A A B A A4|"Em"G G A B A G G2|"C"E G A G E4|
w: A ma-nhã cha-mou|meu no-me bai-xo|e a ja-ne-la a-briu|de-va-gar o céu
V:Piano
G,2D2G2D2|F,2A,2D2A,2|E,2B,2E2B,2|C,2G,2C2G,2|
%%text REFRÃO
V:Voz
"C"e2 e d c2 e2|"G"d d e g g f e2|"D"z d e f e d d2-|"G"d B G6|
w: Luz da ja-ne-la|a-cor-da meu co-ra-ção|vem que a vi-da te|_ cha-ma
V:Piano
c2e2g2e2|B2d2g2d2|A2d2f2a2|g4 d4|
```

## Contagem do refrão

| Compasso | Notas | Sílabas |
|---|---|---|
| `e2 e d c2 e2` | 5 | Luz · da · ja · ne · la |
| `d d e g g f e2` | 7 | a · cor · da · meu · co · ra · ção |
| `z d e f e d d2-` | 6 (a pausa não conta) | vem · que · a · vi · da · te |
| `d B G6` | 3 | `_` (o `d` ligado segura "te") · cha · ma |

O refrão sobe até o `g`, acima da nota mais alta do verso (`B`): é o pico da música.
