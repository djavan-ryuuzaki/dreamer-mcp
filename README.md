# Dreamer MCP

[![CI](https://github.com/djavan-ryuuzaki/dreamer-mcp/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/djavan-ryuuzaki/dreamer-mcp/actions/workflows/docker-publish.yml)
[![Docker Hub](https://img.shields.io/docker/v/djavanryuuzaki/dreamer-mcp?label=docker&sort=semver)](https://hub.docker.com/r/djavanryuuzaki/dreamer-mcp)

Servidor [MCP](https://modelcontextprotocol.io) que dá a assistentes de IA (Claude, Cursor, VS Code…)
o controle de uma instalação **remota** do [ComfyUI](https://github.com/comfyanonymous/ComfyUI):
gerar e editar imagens, criar assets com fundo transparente, gerar vídeo e áudio, acompanhar a
fila e baixar os resultados.

Inspirado no [Comfy-Org/comfy-mcp](https://github.com/Comfy-Org/comfy-mcp), com uma diferença de
arquitetura: em vez de rodar na mesma máquina do ComfyUI, o Dreamer MCP fala com a **API HTTP** dele
e expõe o MCP via **Streamable HTTP**. Assim ele roda como um container stateless (Docker ou
Kubernetes) apontando para o ComfyUI onde quer que ele esteja. O modo stdio também está disponível.

## Destaques

- **Presets de um comando:** `comfyui_generate_image`, `comfyui_generate_asset`,
  `comfyui_generate_video`, `comfyui_generate_music`, `comfyui_song_to_abc` e `comfyui_upscale`,
  cada um ligado a um workflow seu.
- **Música com partitura:** o YuE2 pode seguir uma partitura ABC sua (escrita por você ou por um
  LLM), que é reescrita automaticamente no dialeto que o modelo entende; covers a partir de uma
  música de referência.
- **Gerar ou editar no mesmo workflow:** sem imagens, gera; com 1 a 9 imagens de referência, edita.
  Os slots não usados são removidos do grafo automaticamente.
- **Assets transparentes:** PNG com canal alpha real, largura/altura exatas e cópias recortadas no
  objeto.
- **Qualquer tamanho:** pixels exatos, qualquer proporção (`"16:9"`, `"5:4"`, `"1.5"`) com
  megapixels, ou o tamanho da imagem que está sendo editada.
- **Seus workflows, sem código:** um manifesto YAML dá nomes amigáveis às entradas dos nós, e os
  workflows podem ficar salvos no próprio ComfyUI.
- **Conversão UI → API:** converte workflows salvos pela interface, resolvendo `SetNode`/`GetNode`,
  reroutes e nós em bypass.
- **Mídia por streaming:** links assinados e temporários para vídeo, áudio e imagens, repassados
  pelo MCP, sem expor o ComfyUI.

## Ferramentas

| Tool | O que faz |
|---|---|
| `comfyui_status` | Online?, fila, GPU, VRAM/RAM, versões |
| `comfyui_requirements` | Nós e modelos que os presets usam, com o que está faltando e onde baixar |
| `comfyui_models` | Lista modelos (por pasta, com filtro) |
| `comfyui_workflows` | Lista workflows (locais + salvos no ComfyUI), params e presets |
| `comfyui_workflow_info` | Params amigáveis + inputs editáveis de cada nó |
| `comfyui_history` | Últimos jobs executados (inclusive os feitos pela interface web) |
| `comfyui_save_workflow` | Salva no ComfyUI o workflow (formato API) de um job do histórico |
| `comfyui_convert_workflow` | Converte um workflow do formato UI para API (sem executar) |
| `comfyui_run` | Executa um workflow (por nome ou JSON inline) com params/overrides |
| `comfyui_job_status` | pending (posição na fila) / running (nó atual, passo do sampling, %) / success / error / interrupted |
| `comfyui_cancel` | Remove da fila ou interrompe o job em execução |
| `comfyui_get_output` | Arquivos gerados + links de download/streaming |
| `comfyui_view_image` | Retorna a imagem (reduzida) para o modelo ver |
| `comfyui_queue` | Lista ou limpa a fila |
| `comfyui_upload_file` | Envia imagem/áudio/vídeo (URL/base64) para a pasta `input` |
| `comfyui_free_memory` | Descarrega modelos e libera VRAM |
| `comfyui_generate_image` | Preset `image`: gera; com `images`, edita |
| `comfyui_generate_asset` | Preset `asset`: PNG com fundo transparente |
| `comfyui_upscale` | Preset `upscale` |
| `comfyui_generate_video` | Preset `video` |
| `comfyui_generate_audio` | Preset `audio` (genérico: prompt + letra + duração) |
| `comfyui_generate_music` | Presets `music` / `music_cover`: música com vocais, opcionalmente seguindo uma partitura ABC ou fazendo cover de uma música |
| `comfyui_song_to_abc` | Preset `song_abc`: transcreve uma música para ABC (SheetSage2), com partitura em PDF |
| `comfyui_normalize_abc` | Converte ABC qualquer para o dialeto do YuE2 e aponta problemas (sem executar nada) |

## Início rápido

**Docker** (modo HTTP):

```bash
docker run -d -p 8000:8000 \
  -e COMFYUI_URL=http://192.168.0.10:8188 \
  -e MCP_AUTH_TOKEN=troque-este-token \
  -v ./workflows:/app/workflows:ro \
  djavanryuuzaki/dreamer-mcp:latest
```

Conecte o cliente, por exemplo o Claude Code:

```bash
claude mcp add --transport http dreamer http://localhost:8000/mcp --header "Authorization: Bearer troque-este-token"
```

**Local (stdio)**, sem Docker, usando [uv](https://docs.astral.sh/uv/):

```bash
claude mcp add dreamer -e COMFYUI_URL=http://192.168.0.10:8188 -- uvx --from git+https://github.com/djavan-ryuuzaki/dreamer-mcp dreamer-mcp
```

> Nomes `.local` (mDNS) normalmente não resolvem dentro de containers. Use o IP do ComfyUI,
> `--add-host` no Docker ou `hostAliases` no Kubernetes.

## Requisitos no ComfyUI

Os presets usam modelos e alguns nós que precisam estar instalados no ComfyUI. Ao iniciar, o
Dreamer MCP consulta o servidor e mostra no log o que já está lá e o que falta, com o link de
download e a pasta de destino de cada modelo. A verificação roda em segundo plano, sem atrasar o
MCP, e pode ser desligada com `CHECK_REQUIREMENTS=false`. Para ver o relatório sem subir o
servidor:

```bash
docker run --rm -e COMFYUI_URL=http://192.168.0.10:8188 djavanryuuzaki/dreamer-mcp requirements
```

```bash
uvx --from git+https://github.com/djavan-ryuuzaki/dreamer-mcp dreamer-mcp requirements
```

O comando termina com código 1 se faltar algo; `--offline` só lista os requisitos, sem consultar o
ComfyUI. Com o MCP conectado, o assistente pode usar a tool `comfyui_requirements`.

Versão testada: **ComfyUI 0.37.0**. Os nós marcados como *core* vêm com o próprio ComfyUI; se
estiverem faltando, atualize-o.

### Nós

| Pacote | Usado em | Nós |
|---|---|---|
| ComfyUI (core) | imagem/asset | `TextEncodeQwenImage21`, `ModelAttentionBackend` |
| ComfyUI (core) | vídeo | `MiniMaxH3ReferenceToVideo`, `ComfyMathExpression`, `ComfySwitchNode`, `ResolutionSelector` |
| ComfyUI (core) | áudio | `YuE2GenerateABC`, `YuE2GenerateMusic`, `EmptyYuE2LatentAudio`, `SheetSage2AudioToABC`, `AudioEncoderLoader`, `LoadAudio`, `SaveAudio`, `SaveText`, `PreviewAny` |
| [ComfyUI-Easy-Use](https://github.com/yolain/ComfyUI-Easy-Use) | imagem/asset/áudio | `easy ifElse`, `easy cleanGpuUsed`, `easy clearCacheAll` |
| [ComfyUI-KJNodes](https://github.com/kijai/ComfyUI-KJNodes) *(opcional)* | imagem/asset | `SetNode`/`GetNode`: só para abrir e editar as versões de interface dos workflows |

### Modelos

Cada arquivo vai para `ComfyUI/models/<pasta>/`; subpastas também valem.

**Imagens e assets** (`comfyui_generate_image`, `comfyui_generate_asset`), Qwen Image 2.1, ~17 GB:

| Arquivo | Pasta | Tamanho |
|---|---|---|
| [`qwen_image_2.1_int8_convrot.safetensors`](https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/diffusion_models/qwen_image_2.1_int8_convrot.safetensors) | `diffusion_models` | 7,3 GB |
| [`qwen3vl_8b_int8_convrot.safetensors`](https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/text_encoders/qwen3vl_8b_int8_convrot.safetensors) | `text_encoders` | 9,4 GB |
| [`qwen_image_2.1_vae_bf16.safetensors`](https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/vae/qwen_image_2.1_vae_bf16.safetensors) | `vae` | 0,7 GB |

**Vídeo com som** (`comfyui_generate_video`), MiniMax H3 reference-to-video, ~52 GB:

| Arquivo | Pasta | Tamanho |
|---|---|---|
| [`minimax_h3_ref2va_pruned_int8_convrot.safetensors`](https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors) | `diffusion_models` | 21,0 GB |
| [`qwen3vl_32b_minimax_h3_int8_convrot.safetensors`](https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors) | `text_encoders` | 27,1 GB |
| [`minimax_h3_video_vae_int8_convrot.safetensors`](https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_video_vae_int8_convrot.safetensors) | `vae` | 2,8 GB |
| [`minimax_h3_audio_vae_fp32.safetensors`](https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_audio_vae_fp32.safetensors) | `vae` | 0,6 GB |
| [`minimax_h3_taomate_3step_lora_avg_rank_19_bf16.safetensors`](https://huggingface.co/Kijai/MiniMax-H3_comfy/resolve/main/loras/minimax_h3_taomate_3step_lora_avg_rank_19_bf16.safetensors) | `loras` | 0,2 GB |

A LoRA TaoMate acelera o vídeo para 3 passos.

**Música com vocais** (`comfyui_generate_music`, `comfyui_song_to_abc`), YuE2 + SheetSage2, ~5,4 GB:

| Arquivo | Pasta | Tamanho |
|---|---|---|
| [`yue2_3b_int8_convrot.safetensors`](https://huggingface.co/Comfy-Org/YuE2/resolve/main/checkpoints/yue2_3b_int8_convrot.safetensors) | `checkpoints` | 4,0 GB |
| [`sheetsage2_bf16.safetensors`](https://huggingface.co/Comfy-Org/YuE2/resolve/main/audio_encoders/sheetsage2_bf16.safetensors) | `audio_encoders` | 1,4 GB |

O SheetSage2 só é usado em covers e em `comfyui_song_to_abc`.

Só é preciso instalar o que for usar: cada grupo funciona sozinho. Os workflows já vêm com o MCP
(veja abaixo), então basta ter os nós e os modelos.

## Workflows e presets

O MCP executa workflows no **formato API** do ComfyUI. Há três maneiras de obtê-los:

- **Conversão:** `comfyui_convert_workflow("comfyui:MEU_FLUXO.json", "meu_fluxo")` converte um
  workflow salvo pela interface e grava o resultado no ComfyUI em `workflows/api/meu_fluxo.json`.
  Resolve `SetNode`/`GetNode`, reroutes e nós em bypass (`include_bypassed=true` os ativa), e avisa
  sobre nós que só existem na interface. Não funciona com subgraphs.
- **Pelo histórico:** rode o workflow uma vez na interface e use `comfyui_history` →
  `comfyui_save_workflow(prompt_id, "meu_nome")`. Funciona com qualquer workflow, inclusive com
  subgraphs.
- **Manual:** **Workflow → Export (API)** na interface, com o `.json` salvo em `WORKFLOWS_DIR`.

Workflows salvos no ComfyUI aparecem como `comfyui:<caminho>` e não exigem redeploy.

### Workflows incluídos

Os presets já vêm prontos, em [`workflows/`](workflows) (também embutidos no pacote e na imagem):

| Arquivo | Preset | Modelo |
|---|---|---|
| `generate_image.json` | `image` | Qwen Image 2.1: gera ou edita (1 a 9 referências) + upscale |
| `generate_asset.json` | `asset` | Qwen Image 2.1: PNG com fundo transparente + upscale |
| `minimax_h3_r2v.json` | `video` | MiniMax H3: vídeo com som a partir de 2 imagens de referência |
| `generate_music.json` | `audio`, `music` | YuE2: música com vocais a partir de estilo + letra, com partitura ABC opcional; devolve o áudio e o ABC usado (`.md`) |
| `generate_cover_music.json` | `music_cover` | YuE2 + SheetSage2: cover (nova letra/estilo sobre a melodia de uma música) |
| `generate_song_abc.json` | `song_abc` | SheetSage2: transcreve uma música para ABC |
| `yue2_text2music.json` | – | YuE2: versão anterior (estilo + letra) |
| `qwen_image_t2i.json` | – | Qwen Image 2.1: template original de texto para imagem |

No manifesto, cada preset aponta primeiro para `comfyui:api/<nome>.json` e usa a cópia incluída
(`fallback`) quando esse arquivo não existe no ComfyUI. Para personalizar um fluxo, salve sua versão
em `workflows/api/` no ComfyUI: ela passa a ter prioridade, sem redeploy.

Se os seus modelos estiverem em subpastas (por exemplo `models/diffusion_models/QWEN/...`), não é
preciso editar o workflow: um nome que não existe no ComfyUI é trocado automaticamente pelo arquivo
de mesmo nome em uma subpasta, desde que haja só um.

### Partituras ABC no YuE2

**Escreva ABC "normal"**, com tudo o que o registro da música pede; o MCP converte para o YuE2 e
gera o PDF a partir do texto original:

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
%%text VERSO 1
V:Voz
"G"z2 B B A G G2|"D"A A B A A4|"Em"G G A B A G G2|"C"E G A G E4|
w: A ma-nhã cha-mou|meu no-me bai-xo|e a ja-ne-la a-briu|de-va-gar o céu
V:Piano
G,2D2G2D2|F,2A,2D2A,2|E,2B,2E2B,2|C,2G,2C2G,2|
```

- `T:` (título, também nome do PDF), `C:` (autores) e seções em `%%text`;
- cada voz sob a sua linha `V:` (ou com `[V:Voz]` no início da linha); a segunda voz vira a
  linha instrumental do YuE2;
- uma linha `w:` sob cada linha da voz cantada: uma sílaba por nota, hífen dentro da palavra, `|`
  nas barras, `_` na nota que segura a sílaba anterior (inclusive a nota depois de uma ligadura
  `-`, que senão recebe a próxima sílaba) e `*` na nota sem sílaba;
- as tags `[Verse]`/`[Chorus]` do `lyrics` na mesma ordem das seções cantadas, com o mesmo texto
  dos `w:`. Ao mudar a letra, reescreva os `w:`. O ABC que um job devolve (escrito pelo YuE2 ou
  pelo SheetSage2) não tem letra: reenviado sem `w:`, a partitura em PDF sai só com as notas.

O guia completo para um agente compor e anotar músicas está na skill
[`skills/compor-musica`](skills/compor-musica/SKILL.md).

O YuE2 **não interpreta** ABC: o `YuE2GenerateMusic` só tokeniza o texto e o coloca no prompt do
modelo. Por isso a partitura só funciona se tiver a cara das que o próprio YuE2
(`YuE2GenerateABC`) e o SheetSage2 escrevem, e é para isso que o ABC normal é convertido:

```
X:1
T:
M:4/4
L:1/16
Q:1/4=104
V: Vocal clef=treble name="Vocal Melody" snm="Vocal"
V: Ins clef=treble name="Ins Melody" snm="Inst."
K:G
% verse
V: Vocal
"G"z2d2d2e2d2B2A4|"Em"G2z2A2B2G4z4|"Am"z2e2e2d2e2B2c4|"D"d2B2A2G2F2A2d4|
V: Ins
Z4|
```

Duas vozes intercaladas a cada (até) 4 compassos, `L:1/16`, seções como comentários `% verse`,
`% chorus`..., acordes entre aspas no início do compasso (nenhum no modo `melody`) e **sem**
linhas `w:` (a letra vai separada, com tags `[Verse]`/`[Chorus]` na mesma ordem das seções
cantadas). Um ABC já nesse dialeto é mantido, só sem as linhas `w:` e `%%`. Escreva sempre uma
linha instrumental sob o vocal (respostas, levada, contracanto): com a voz `Ins` só em pausa
(`Z4`) o arranjo tende a soar como playback de karaokê. A conversão é feita pelo parâmetro `abc`
(`transform: yue2_abc` no manifesto); `comfyui_normalize_abc` mostra o resultado e os avisos
antes de gerar.

**Para o YuE2 cantar a partitura com fidelidade.** Quando a partitura não "cabe" no que o modelo
canta, ele improvisa e a música sai mais longa que o escrito (nos testes, +18% com poucas notas para
uma letra em português, contra −1% com a partitura do próprio YuE2). O que ajuda:

- **Notas suficientes para a letra:** cerca de uma nota por sílaba cantada, mais nos melismas.
  Português e espanhol pedem mais notas do que as sílabas escritas sugerem; evite espremer vogais
  (`do~a`) numa nota só.
- **O ritmo do gênero, não só colcheias retas:** 3+3+2 no dance-pop, funk e reggaeton
  (`g3/2f3/2e` em `L:1/8`) e antecipações ligadas por cima do tempo ou da barra (`e-|e…`); síncope
  no samba e no pagode; notas longas nas baladas.
- **O refrão é o pico:** a nota mais alta do refrão acima da do verso e da do pré-refrão.
- **Harmonia própria no pré-refrão** (o IV ou o ii, uma dominante) para criar tensão.
- **Frases regulares** (2 ou 4 compassos, seções de 4 ou 8) e uma linha instrumental que muda entre
  as seções (riff na intro, levada nos versos, mais cheia nos refrões).

### Partitura em PDF

`comfyui_generate_music` (músicas e covers) e `comfyui_song_to_abc` também devolvem a partitura em
PDF (saída com `label: "score"`), gravada no ComfyUI em `output/dreamer-mcp/scores/` e entregue com
os mesmos links da mídia:

- se você (ou o LLM) passou `abc_notation`, a partitura é desse texto **original**, com a letra das
  linhas `w:` e as seções do jeito que foram escritas, e sai já na resposta (`score`), mesmo com
  `wait=false`;
- senão, é do ABC que o workflow escreveu (YuE2 ou SheetSage2), criada quando o job termina: na
  resposta com `wait=true`, ou em `comfyui_job_status` / `comfyui_get_output`.

A renderização roda no próprio MCP, sem ComfyUI: `abcm2ps` (ABC → PostScript) + Ghostscript
(PostScript → PDF). A imagem Docker já traz os dois; fora dela, instale-os
(`apt install abcm2ps ghostscript`). Sem eles, a música é gerada normalmente e a resposta traz
`score_error`. No manifesto, isso é a opção `score: {param: abc, output: abc}` do workflow.

### O manifesto `workflows.yaml`

Dê aos nós de entrada títulos como `[$PROMPT]` e `[$IMAGEM_0]` e referencie-os pelo título, que
não muda quando você reorganiza o fluxo (o id do nó também funciona):

```yaml
presets:
  image: meu_flux          # usado por comfyui_generate_image
  asset: meu_asset         # comfyui_generate_asset
  upscale: upscale
  video: wan_i2v
  audio: ace_step

workflows:
  meu_flux:
    file: "comfyui:api/meu_flux.json"     # ou um arquivo local em WORKFLOWS_DIR
    fallback: meu_flux.json               # opcional: cópia local se não existir no ComfyUI
    description: Flux dev
    params:
      prompt: {target: "[$PROMPT].value", type: string}
      seed:   {target: "25.noise_seed", type: seed}        # aleatório se não informado
      steps:  {target: "KSampler.steps", type: int}        # id, título ou classe do nó
      image:  {target: "[$IMAGEM].image", type: file}      # upload automático (URL/base64)
```

Tipos: `string`, `int`, `float`, `bool`, `seed`, `choice`, `file`, `file_list`. Workflows sem
entrada no manifesto também rodam com `comfyui_run(inputs={"6.text": "..."})`; veja o que pode ser
alterado com `comfyui_workflow_info`.

**Imagens opcionais (gerar ou editar no mesmo fluxo).** Um `LoadImage` não pode ficar vazio, então
os slots sem arquivo são removidos do grafo, junto com o que depende deles:

```yaml
    params:
      images:
        target: ["[$IMAGEM_0].image", "[$IMAGEM_1].image", "[$IMAGEM_2].image"]
        type: file_list
        prune_missing: true
        flatten_alpha: "#ffffff"   # PNGs transparentes são achatados antes do upload
    when_missing:                  # overrides quando o param NÃO é enviado
      images: {"[$MANTER_RESOLUCAO].value": false}
    when_set:                      # overrides quando o param É enviado
      width: {"[$MANTER_RESOLUCAO].value": false}
```

**Várias saídas e etapas opcionais.** Cada arquivo volta com um rótulo, e remover um `SaveImage`
basta para pular uma etapa, porque o ComfyUI só executa o que alimenta alguma saída:

```yaml
    outputs: {base: "160", upscale: "118"}   # "label": "upscale" em cada arquivo
    params:
      upscale: {type: bool, default: true, remove_when_false: ["118"]}
```

**Tamanho em pixels.** Quando `width`/`height` não são enviados, a regra `size` converte proporção
e megapixels em pixels ou usa o tamanho da imagem editada:

```yaml
    size:
      aspect_ratio: aspect_ratio   # "16:9" @ 1 MP = 1360x768
      megapixels: megapixels       # 1.0 = ~1024x1024
      from_image: images           # editando sem tamanho: tamanho exato de <image1>
      max_side: 1536               # teto para imagens grandes
    params:
      width:  {target: "[$WIDTH].value", type: int, default: 1024, multiple_of: 16}
      height: {target: "[$HEIGHT].value", type: int, default: 1024, multiple_of: 16}
      aspect_ratio: {type: string}  # param sem target: só alimenta regras
      megapixels:   {type: float}
```

A prioridade é: `width`/`height` explícitos, depois `aspect_ratio`/`megapixels`, depois o tamanho
de `<image1>` e, por fim, os defaults.

**Outras opções:**

| Opção | Onde | Efeito |
|---|---|---|
| `choice` | tipo | Valida contra as opções do combo (lidas de `/object_info`); `"16:9"` casa com `"16:9 (Widescreen)"` |
| `append_when` | param de texto | Acrescenta um texto fixo quando um param `bool` é true |
| `remove_when_true` | param `bool` | Remove nós quando o valor é true |
| `trim_alpha` | workflow | Devolve também cópias das saídas transparentes recortadas no objeto |
| `score` | workflow | Devolve também a partitura em PDF: do ABC passado em `param`, ou do texto ABC da saída `output` |
| `transform` | param de texto | Reescreve o valor depois de aplicar todos os params, ex.: `{name: yue2_abc, lyrics: "[$LYRICS].value", mode: "8.mode"}` (os argumentos são lidos do grafo final); avisos voltam em `<param>_warnings` |

Regras condicionais nunca sobrescrevem um param enviado explicitamente. Os presets aceitam `extra`
para params adicionais ou overrides crus (`{"KSampler.cfg": 4}`), e `comfyui_run` sorteia as seeds
que você não fixou (`randomize_seed=true`) para o cache do ComfyUI não repetir o resultado.

## Assets com fundo transparente

`comfyui_generate_asset` foi pensado para o Qwen Image 2.1, cuja VAE decodifica RGBA: o `SaveImage`
grava um PNG com transparência real, sem nó de remoção de fundo. O manifesto acrescenta ao prompt,
e também ao prompt da passada de upscale, a instrução *"This is an RGBA image with transparency.
The image has an alpha channel and the background is transparent."*; quem chama só descreve o
objeto.

- Tamanho em pixels (padrão 1024×1024); editando sem tamanho, usa o de `<image1>`.
- Imagens de entrada transparentes são achatadas sobre branco, porque o `LoadImage` descarta o alpha.
- `trim` (ligado por padrão) devolve também `base_trimmed`/`upscale_trimmed`, recortados no objeto
  com margem, salvos ao lado dos originais.
- As previews mostram a transparência sobre um xadrez.

## Vídeo, áudio e imagens: streaming

As tools devolvem **links** que qualquer player abre por streaming (com suporte a `Range`). Vídeo
e áudio também vêm como blocos `resource_link` com o tipo MIME. Com `MCP_PUBLIC_URL` definido:

```json
{ "filename": "clip_00005_.mp4", "mime_type": "video/mp4",
  "url":    "https://dreamer.example.com/media/<token>/clip_00005_.mp4",
  "player": "https://dreamer.example.com/media/<token>/player" }
```

- O arquivo é repassado pelo MCP: **o ComfyUI não precisa ser exposto**.
- O `<token>` é assinado (HMAC), expira e dá acesso só àquele arquivo, já que players não enviam
  o header `Authorization`.
- `player` é uma página simples com `<video>`/`<audio>`/`<img>` e link de download.

Sem `MCP_PUBLIC_URL` (ou em stdio), os links apontam para o `/view` do ComfyUI.

### Progresso dos jobs

Enquanto um job roda, `comfyui_job_status` traz `progress` com o nó em execução e o passo do
sampling:

```json
{"status": "running", "progress": {"node": "115", "node_type": "KSampler", "step": 20,
 "steps": 30, "percent": 67, "nodes_done": 18, "nodes_total": 22, "elapsed_s": 108.6}}
```

Com `wait=true`, as notificações de progresso do MCP mostram o mesmo resumo
(`running, KSampler, step 20/30 (67%), node 18/22`). O ComfyUI só envia esses dados pelo WebSocket
`/ws` e só para o `client_id` que enfileirou o prompt, então o servidor mantém uma conexão com o
mesmo id dos envios. Jobs enfileirados por outro cliente (a interface web, ou outra réplica do
MCP) aparecem só como `running`, sem `progress`. Se o WebSocket estiver inacessível, nada quebra:
o status volta a ser só pending/running. `percent` é do nó atual (cada sampler conta de novo do
zero); `nodes_total` conta todos os nós do grafo, inclusive os que o ComfyUI acaba não executando.

## Configuração

| Variável | Padrão | Descrição |
|---|---|---|
| `COMFYUI_URL` | `http://127.0.0.1:8188` | URL usada pelo servidor para acessar o ComfyUI |
| `COMFYUI_PUBLIC_URL` | = `COMFYUI_URL` | Base das URLs diretas devolvidas |
| `COMFYUI_API_KEY` | – | Bearer enviado ao ComfyUI (se estiver atrás de um proxy com auth) |
| `COMFYUI_PROGRESS` | `true` | Acompanhar os jobs pelo WebSocket do ComfyUI (nó e passo em `running`) |
| `WORKFLOWS_DIR` | `workflows` (`/app/workflows` na imagem) | Pasta dos workflows + `workflows.yaml`; se não existir, usa os incluídos no pacote |
| `WORKFLOWS_FROM_COMFYUI` | `true` | Listar também os workflows salvos no ComfyUI |
| `DEFAULT_WAIT_TIMEOUT` | `300` | Segundos de espera quando `wait=true` |
| `PREVIEW_MAX_SIZE` | `768` | Lado máximo das previews inline |
| `MCP_TRANSPORT` | `stdio` (`http` na imagem) | `stdio` ou `http` |
| `MCP_HOST` / `MCP_PORT` / `MCP_PATH` | `0.0.0.0` / `8000` / `/mcp` | Endpoint HTTP |
| `MCP_AUTH_TOKEN` | – | Se definido, exige `Authorization: Bearer <token>` |
| `MCP_PUBLIC_URL` | – | URL externa deste MCP; ativa os links de mídia |
| `MEDIA_SECRET` | derivado do `MCP_AUTH_TOKEN` | Chave HMAC dos links (igual em todas as réplicas) |
| `MEDIA_LINK_TTL` | `86400` | Validade dos links de mídia, em segundos |
| `CHECK_REQUIREMENTS` | `true` | Verificar e mostrar no log, ao iniciar, os nós/modelos que faltam |
| `LOG_LEVEL` | `INFO` | Nível de log |

Veja [`.env.example`](.env.example).

## Kubernetes

Os manifestos (kustomize) estão em [`deploy/k8s`](deploy/k8s):

```bash
cp deploy/k8s/secret.env.example deploy/k8s/secret.env   # defina MCP_AUTH_TOKEN
kubectl apply -k deploy/k8s
```

Ajuste `COMFYUI_URL` e `MCP_PUBLIC_URL` em `kustomization.yaml` e coloque o seu `workflows.yaml`
em `deploy/k8s/workflows/`: ele vira um ConfigMap, e qualquer alteração gera um rollout
automático. O `ingress.yaml` opcional já vem com timeout longo e sem buffering, para `wait=true` e
streaming.

## Desenvolvimento

```bash
uv sync
uv run pytest
uv run ruff check src tests
```
