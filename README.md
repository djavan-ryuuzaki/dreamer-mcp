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
  `comfyui_generate_video`, `comfyui_generate_audio` e `comfyui_upscale`, cada um ligado a um
  workflow seu.
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
| `comfyui_models` | Lista modelos (por pasta, com filtro) |
| `comfyui_workflows` | Lista workflows (locais + salvos no ComfyUI), params e presets |
| `comfyui_workflow_info` | Params amigáveis + inputs editáveis de cada nó |
| `comfyui_history` | Últimos jobs executados (inclusive os feitos pela interface web) |
| `comfyui_save_workflow` | Salva no ComfyUI o workflow (formato API) de um job do histórico |
| `comfyui_convert_workflow` | Converte um workflow do formato UI para API (sem executar) |
| `comfyui_run` | Executa um workflow (por nome ou JSON inline) com params/overrides |
| `comfyui_job_status` | pending (posição na fila) / running / success / error / interrupted |
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
| `comfyui_generate_audio` | Preset `audio` |

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

> O `workflows/workflows.yaml` deste repositório mapeia os workflows do autor (Qwen Image 2.1,
> MiniMax H3, YuE2), que não estão incluídos. Use-o como modelo para mapear os seus.

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

## Configuração

| Variável | Padrão | Descrição |
|---|---|---|
| `COMFYUI_URL` | `http://127.0.0.1:8188` | URL usada pelo servidor para acessar o ComfyUI |
| `COMFYUI_PUBLIC_URL` | = `COMFYUI_URL` | Base das URLs diretas devolvidas |
| `COMFYUI_API_KEY` | – | Bearer enviado ao ComfyUI (se estiver atrás de um proxy com auth) |
| `WORKFLOWS_DIR` | `workflows` (`/app/workflows` na imagem) | Pasta dos workflows + `workflows.yaml` |
| `WORKFLOWS_FROM_COMFYUI` | `true` | Listar também os workflows salvos no ComfyUI |
| `DEFAULT_WAIT_TIMEOUT` | `300` | Segundos de espera quando `wait=true` |
| `PREVIEW_MAX_SIZE` | `768` | Lado máximo das previews inline |
| `MCP_TRANSPORT` | `stdio` (`http` na imagem) | `stdio` ou `http` |
| `MCP_HOST` / `MCP_PORT` / `MCP_PATH` | `0.0.0.0` / `8000` / `/mcp` | Endpoint HTTP |
| `MCP_AUTH_TOKEN` | – | Se definido, exige `Authorization: Bearer <token>` |
| `MCP_PUBLIC_URL` | – | URL externa deste MCP; ativa os links de mídia |
| `MEDIA_SECRET` | derivado do `MCP_AUTH_TOKEN` | Chave HMAC dos links (igual em todas as réplicas) |
| `MEDIA_LINK_TTL` | `86400` | Validade dos links de mídia, em segundos |
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
