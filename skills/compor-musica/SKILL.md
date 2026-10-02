---
name: compor-musica
description: Compõe uma música com vocais pelo MCP dreamer (YuE2) - letra, partitura ABC com a letra sob as notas e geração do áudio e do PDF. Use ao criar uma música ou canção, ao mudar a letra, a melodia ou o estilo de uma música já gerada, ou quando a partitura precisa sair com a letra.
---

# Compor música

Cada música é um conjunto de três textos que andam juntos: o `style`, a `lyrics` e o
`abc_notation`. O ABC é a **fonte da verdade**: o YuE2 canta a melodia dele, e a partitura em PDF
é impressa dele, com a letra das linhas `w:`. Guarde a versão atual dos três e trabalhe sempre a
partir dela.

Exemplo completo e conferido: [exemplo.md](exemplo.md).

## Passos

### 1. Defina a música

Título, autores (para o `C:`), tema, gênero, andamento (BPM), tom, voz (masculina/feminina,
timbre), instrumentos e clima. O que o usuário não disser, escolha você de forma coerente com o
gênero e diga o que escolheu.

**Pronto quando:** cada um desses itens tem um valor.

### 2. Escreva a letra

Estrutura comum: verso, pré-refrão, refrão, verso, pré-refrão, refrão, ponte, refrão. Frases
curtas e cantáveis, com rima e métrica parecidas entre os versos da mesma seção.

Monte o `lyrics` com uma tag por seção cantada, na ordem em que é cantada: `[Verse]`,
`[Pre-Chorus]`, `[Chorus]`, `[Bridge]`. Seções só instrumentais (intro, interlúdio, solo) ficam
fora do `lyrics`.

**Pronto quando:** toda seção cantada tem sua tag, e a ordem das tags é a ordem da música.

### 3. Escreva a partitura ABC

Siga a seção [Como anotar](#como-anotar). Componha a melodia **a partir da letra**: separe as
sílabas de cada frase e dê uma nota a cada uma.

**Pronto quando:** em todo compasso da voz cantada, o número de notas (sem contar pausas) é
igual ao número de sílabas do `w:` correspondente, contando `_` e `*` como sílabas. Conte
compasso por compasso; é aqui que a partitura costuma sair errada.

### 4. Confira com `comfyui_normalize_abc`

Chame `comfyui_normalize_abc(abc_notation, lyrics, mode)`. Corrija tudo o que vier em
`warnings` (compasso com duração errada, seções fora da ordem das tags) e chame de novo.

**Pronto quando:** `warnings` vem vazio e `sung_sections` tem a mesma ordem das tags da letra.

### 5. Gere

Chame `comfyui_generate_music(style, lyrics, abc_notation)`, sempre com `abc_notation`. Sem ele,
o YuE2 escreve a própria partitura, e ela nunca tem letra.

- `style`: tags curtas em inglês, separadas por vírgula: gênero, voz, instrumentos, clima, BPM
  (ex.: `pop acoustic, warm male vocal, piano, light drums, hopeful, 104 bpm`). O BPM é o mesmo
  do `Q:`.
- `mode`: `full` (melodia + acordes, padrão) ou `melody` (só melodia; aí o ABC fica sem acordes).
- A geração leva minutos: use `wait=false` e acompanhe com `comfyui_job_status(prompt_id)`.

Guarde da resposta o `applied.seed`. Entregue ao usuário o áudio (`audio`), a partitura em PDF
(`score`) e o ABC (`abc`).

**Pronto quando:** o job terminou com `success` e o PDF da partitura foi entregue.

### 6. Ajuste

Parta sempre da **sua** versão atual do ABC, nunca do ABC que o job devolve. O ABC devolvido é o
dialeto interno do YuE2, sem `T:`, sem seções e sem letra; reenviado assim, a partitura sai só
com as notas.

- **Mudar a letra:** reescreva a frase no `lyrics` **e** no `w:`. Se o número de sílabas mudou,
  ajuste as notas daquele compasso (divida uma nota longa ou junte duas) e refaça a conferência
  do passo 3.
- **Mudar a melodia:** mude as notas mantendo a contagem de sílabas, ou ajuste o `w:` junto.
- **Mudar o estilo:** mude o `style` e, se o andamento mudar, o `Q:`.

Para uma variação da mesma gravação (mesma voz e arranjo), passe o mesmo `seed` da versão
anterior. Para uma interpretação nova, omita o `seed`. Depois, volte ao passo 4.

## Como anotar

### Cabeçalho

```
X:1
T:<título>
C:Letra e música: <autores>
M:4/4
L:1/8
Q:1/4=<bpm>
K:<tom>
V:Voz clef=treble name="Voz"
V:<Instrumento> clef=treble name="<Instrumento>"
```

O `T:` também dá nome ao arquivo PDF.

### Seções e vozes

Abra cada seção com `%%text <NOME>` (INTRO, VERSO 1, PRÉ-REFRÃO, REFRÃO, PONTE, OUTRO). Dentro
dela, escreva a voz cantada e depois a instrumental, cada bloco sob a sua linha `V:` própria:

```
%%text VERSO 1
V:Voz
"G"z2 B B A G G2|"D"A A B A A4|
w: A ma-nhã cha-mou|meu no-me bai-xo
V:Piano
G,2D2G2D2|F,2A,2D2A,2|
```

- Acordes entre aspas no início dos compassos da voz cantada. Na voz instrumental, só onde o
  cantor está em pausa (intro, interlúdio). Em `mode="melody"`, nenhum acorde.
- Escreva sempre uma linha instrumental de verdade (levada, respostas, contracanto, riff). Com o
  instrumento só em pausa, o arranjo soa como playback de karaokê.
- Até 4 compassos por linha. Frases de 2 ou 4 compassos e seções de 4 ou 8.

### Letra (`w:`)

Uma linha `w:` logo abaixo de cada linha de notas da voz cantada:

| Escreva | Para |
|---|---|
| `ma-nhã` | sílabas da mesma palavra, uma por nota |
| espaço | separar palavras, uma por nota |
| `\|` | marcar a barra de compasso (mantém letra e notas alinhadas) |
| `_` | nota que segura a sílaba anterior: melisma, e **sempre** na nota depois de uma ligadura `-` |
| `*` | nota sem sílaba |

A nota depois de uma ligadura recebe sílaba como qualquer outra: sem o `_`, toda a letra seguinte
desliza uma nota.

Separe as sílabas como elas são **cantadas**. Em português, dê uma nota a cada vogal de um
encontro entre palavras ("de a-mor": `de a-mor`, duas notas para "de a") em vez de juntá-las numa
nota só. O YuE2 canta melhor com notas de sobra do que espremido.

### Melodia que o YuE2 canta fielmente

Quando a partitura não comporta a letra, o YuE2 improvisa e a música sai mais longa que o escrito.

- **Uma nota por sílaba cantada**, mais nos melismas.
- **O ritmo do gênero**, não só colcheias retas: 3+3+2 no dance-pop, funk e reggaeton
  (`g3/2f3/2e`); antecipações ligadas por cima do tempo ou da barra (`e-|e`, com `_` no `w:`);
  síncope no samba e no pagode; notas longas nas baladas.
- **O refrão é o pico:** a nota mais alta do refrão fica acima das do verso e do pré-refrão.
- **Harmonia própria no pré-refrão** (o IV ou o ii, uma dominante) para criar tensão.
- **A linha instrumental muda entre as seções:** riff na intro, levada nos versos, mais cheia nos
  refrões.
