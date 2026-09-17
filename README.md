# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio *A Ponte*: a Central de Salas da Hill Valley Tech em dois processos separados.

| Processo | Pasta | Porta | Papel |
|---|---|---|---|
| Servidor MCP | [`servidor-mcp/`](servidor-mcp/) | 7301, `/mcp` | Streamable HTTP na revisão 2026-07-28, com três tools, o resource `politica://uso` e MRTR na reserva |
| Agente | [`agente/`](agente/) | 7300, `/a2a` e `/.well-known/agent-card.json` | Servidor A2A v1.0 (JSON-RPC) por fora, host MCP por dentro, sem LLM |

Stack: Python 3.10+, SDK oficial `mcp==2.2.0` (v2) no servidor, `a2a-sdk==1.1.2` no agente. As versões estão travadas em cada `pyproject.toml`, com `uv.lock` e um `requirements.txt` exportado do lock.

```
.
├── README.md
├── dados/ validador/ exemplos/      (do starter, sem alteração)
├── servidor-mcp/
│   ├── servidor.py                  tools, resource, MRTR, log de requests no stderr
│   ├── dominio.py                   salas, reservas em memória, política e alternativas
│   └── pyproject.toml, uv.lock, requirements.txt
└── agente/
    ├── agente.py                    Agent Card, executor A2A e a ponte
    ├── host_mcp.py                  cliente MCP por HTTP (o agente como host)
    └── pyproject.toml, uv.lock, requirements.txt
```

## Como rodar

Pré-requisitos: [uv](https://docs.astral.sh/uv/getting-started/installation/) (ele baixa um Python 3.10+ se a máquina não tiver) e um `python3` 3.10+ para o validador. A seção seguinte mostra a alternativa com `pip`.

```bash
git clone https://github.com/marcos-r-r/desafio-a2a-com-mcp.git
```

```bash
cd desafio-a2a-com-mcp
```

**Terminal 1, servidor MCP.** A chave de integridade do `requestState` vem de `REQUEST_STATE_SECRET` e precisa de pelo menos 32 bytes aleatórios. Gere a sua e exporte no próprio terminal; nunca coloque o valor no repositório. O servidor não sobe sem ela.

```bash
cd servidor-mcp
```

```bash
export REQUEST_STATE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
```

```bash
uv sync --locked && uv run python servidor.py
```

O stderr desse terminal é o log de requests: método, id, alvo, `retry=sim` quando o request traz `requestState`, o `traceparent` do `_meta` e o desfecho (HTTP, `resultType` ou código de erro). Para testar o retry depois de um restart, pare com `Ctrl+C` e rode de novo `uv run python servidor.py` **no mesmo terminal**, para manter a mesma chave.

**Terminal 2, agente.**

```bash
cd agente
```

```bash
uv sync --locked && uv run python agente.py
```

**Terminal 3, validador** (na raiz do repositório, com os dois processos recém-iniciados):

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Se o `python3` do sistema for anterior a 3.10, rode o validador com o Python do uv:

```bash
uv run --no-project --python 3.12 python validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

### Alternativa sem uv

Em cada pasta (`servidor-mcp/` e `agente/`), com um Python 3.10+:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

E suba com `.venv/bin/python servidor.py` (depois do `export REQUEST_STATE_SECRET=...`) e `.venv/bin/python agente.py`.

### Variáveis de ambiente

| Variável | Padrão | Processo |
|---|---|---|
| `REQUEST_STATE_SECRET` | obrigatória, sem padrão | servidor MCP |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `7301` | servidor MCP |
| `AGENTE_HOST` / `AGENTE_PORT` | `127.0.0.1` / `7300` | agente |
| `MCP_URL` | `http://localhost:7301/mcp` | agente |
| `AGENTE_URL_PUBLICA` | `http://localhost:7300` | agente (URL anunciada no card) |

### Conversando com o agente na mão

O pedido e a resposta à pausa têm formato fixo:

```
reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>
escolha=<id da sala>        ou        escolha=recusar
```

```bash
curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' -H 'traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01' -d "$(python3 -c 'import json; print(json.dumps(json.load(open("exemplos/wire/08-a2a-send-message.json"))["request"]["body"]))')"
```

A resposta traz a Task em `TASK_STATE_INPUT_REQUIRED` com `alternativas: sala-fusca, sala-mirante`. Para continuar, mande `escolha=sala-mirante` com o `taskId` recebido (formato em `exemplos/wire/10-a2a-send-message-continuacao.json`) e confira com `GetTask`.

## Onde a ponte acontece

A ponte está em [`agente/agente.py`](agente/agente.py), na classe `AgenteDeSalas`. **Ida:** em `_traduzir` ([agente/agente.py:199](agente/agente.py:199)), quando o `tools/call` volta com `resultType: "input_required"`, o agente lê a única elicitation em form mode (chave, campo e o `enum`), guarda um objeto `Pausa` com os argumentos originais, a chave, as alternativas e o `requestState` opaco em `self.pausas`, indexado pelo id da Task ([agente/agente.py:216](agente/agente.py:216)), e publica `TASK_STATE_INPUT_REQUIRED` com a mensagem `alternativas: <ids>` na ordem do `enum`. A Task para ali e o `execute` retorna: nada fica bloqueado esperando. **Volta:** o `SendMessage` seguinte com o mesmo `taskId` cai em `_continuar` ([agente/agente.py:162](agente/agente.py:162)). Uma escolha fora do `enum` só repete a pausa. Uma escolha válida retira a `Pausa` daquela Task e repete o `tools/call` original ([agente/agente.py:182](agente/agente.py:182)) com `inputResponses` na mesma chave (`accept` com a sala, ou `decline` para `escolha=recusar`) e o `requestState` ecoado byte a byte ([agente/agente.py:191](agente/agente.py:191)). O id JSON-RPC sai de um contador do processo em [`agente/host_mcp.py`](agente/host_mcp.py:99), então o retry sempre tem id novo. O resultado passa de novo por `_traduzir`: `complete` com reserva vira `TASK_STATE_COMPLETED` com o artifact `reserva`, `reservado: false` vira `TASK_STATE_CANCELED` e `isError` vira `TASK_STATE_FAILED` com o texto da tool.

Do lado do servidor, a outra ponta é [`servidor-mcp/servidor.py`](servidor-mcp/servidor.py): `_reservar` ([servidor-mcp/servidor.py:164](servidor-mcp/servidor.py:164)) devolve o `InputRequiredResult` e `_retomar` ([servidor-mcp/servidor.py:187](servidor-mcp/servidor.py:187)) reconstrói o pedido a partir do `requestState` já verificado.

## Decisões técnicas

**Proteção do `requestState`.** O servidor usa o utilitário do próprio SDK: `MCPServer(request_state_security=RequestStateSecurity(keys=[segredo], ttl=600))` ([servidor-mcp/servidor.py:125](servidor-mcp/servidor.py:125)). Com isso o SDK instala um `RequestStateBoundary` que sela todo `requestState` de saída com **AES-256-GCM** (AEAD: integridade e sigilo), com chave derivada de `REQUEST_STATE_SECRET` por HKDF-SHA256, e verifica toda entrada antes de a tool rodar. O envelope selado carrega `iat`, `exp`, o método, o nome da tool, um digest dos argumentos e a audiência (`central-de-salas`). Qualquer caractere trocado, estado expirado ou apresentado com outros argumentos é rejeitado com `-32602` (`Invalid or expired requestState`), e o motivo real vai só para o log. Não há segredo no código: o processo sai com erro se a variável faltar ou tiver menos de 32 bytes (aceita hex, como o de `secrets.token_hex(32)`, ou texto bruto).

**Validade:** 10 minutos (`TTL_REQUEST_STATE`, dentro da faixa de 5 a 30).

**Argumentos adulterados no retry.** O digest dos argumentos dentro do envelope faz o SDK rejeitar o retry cujos argumentos divergem do que foi selado (`-32602`). Mesmo assim, `_retomar` nunca usa os argumentos do retry: sala, início, fim, responsável e a lista de alternativas saem do estado selado.

**Nada em memória entre as rodadas.** O texto selado leva tudo que a retomada precisa (`sala`, `inicio`, `fim`, `responsavel`, `alternativas`). Por isso o retry funciona depois de reiniciar o servidor MCP, desde que a chave seja a mesma. Na retomada, a escolha precisa estar entre as alternativas seladas, e a sala escolhida é revalidada contra a agenda atual.

**Por que o MRTR foi escrito na tool, e não com `Resolve(...)`/`Elicit`.** O SDK também oferece resolvers que geram o `input_required` sozinhos, mas eles refazem a pergunta a cada rodada e só aceitam a resposta se a pergunta for idêntica. Depois de um restart as reservas em memória somem, as alternativas recalculadas podem mudar e o retry voltaria a pedir input em vez de concluir. Com o `InputRequiredResult` montado na tool, a pergunta feita fica selada no estado e o retry conclui.

**Capability de elicitation.** Antes de devolver `input_required`, a tool confere `clientCapabilities.elicitation` **daquele request**. Sem form mode (um `elicitation: {}` sem modos conta como form, como na spec; só `url` não conta), responde `-32021` com `data.requiredCapabilities = {"elicitation": {"form": {}}}` e o transporte do SDK mapeia para HTTP 400. Os `-32602`/400 de `_meta` incompleto e os `-32020` de header divergente do corpo vêm da validação por request do transporte stateless do SDK, que nunca olha requests anteriores.

**Estado das Tasks.** As Tasks A2A ficam no `InMemoryTaskStore` do `a2a-sdk`. As pausas ficam num dicionário em memória do agente (`Pausas`, [agente/agente.py:85](agente/agente.py:85)), indexado pelo id da Task, e o `requestState` nunca sai dali: não vai para card, artifact, mensagem nem log. Duas Tasks pausadas ao mesmo tempo têm cada uma a sua `Pausa`. Nada disso sobrevive a restart do agente, e as reservas do servidor MCP também não, como permite o enunciado. Estado terminal é definitivo: `HandlerDeSalas` ([agente/agente.py:353](agente/agente.py:353)) recusa `SendMessage` para Task terminal antes de chegar ao executor.

**O agente como host MCP.** A cada Task nova, o agente faz `tools/list` (e confere que `reservar_sala` foi anunciada), lê `politica://uso` e extrai a versão da primeira linha, e só então chama a tool. Todo request leva `io.modelcontextprotocol/protocolVersion`, `clientInfo` e `clientCapabilities: {"elicitation": {"form": {}}}` no `_meta`, e os headers `MCP-Protocol-Version`, `Mcp-Method` e, em `tools/call` e `resources/read`, `Mcp-Name`. O agente não aplica nenhuma regra de sala: conflito, política e alternativas vêm do servidor.

**Trace context.** O agente lê o header `traceparent` do request A2A e usa o mesmo trace-id no `_meta.traceparent` de todos os requests MCP da Task, com um span-id novo em cada um. Se a continuação chegar sem o header, vale o trace-id guardado na pausa. Sem header nenhum, a Task ganha um trace-id novo.

**Detalhes do `a2a-sdk`.**
- O SDK trata a ausência do header `A2A-Version` como v0.3 e recusa o request. Como o endpoint só atende o binding v1.0 anunciado no card, `ContextoV1` ([agente/agente.py:367](agente/agente.py:367)) assume `1.0` quando o header falta.
- O SDK só move a mensagem do status para o `history` quando chega o status seguinte, e estado terminal não tem seguinte. `_encerrar` ([agente/agente.py:283](agente/agente.py:283)) publica a mensagem final num status `WORKING` e depois no estado terminal. Assim a mensagem da tool (por exemplo `Sala inexistente: sala-inexistente`) fica no `status.message` e no `history`. Na pausa, a linha `alternativas:` fica no `status.message` e entra no `history` quando a Task é retomada.
- O card é servido por uma rota própria com a serialização v1.0 do proto `AgentCard`. A rota pronta do SDK acrescenta campos de compatibilidade v0.3 (`preferredTransport`, `url`, `protocolVersion: 0.3`).

**Limitação do cliente MCP do SDK (por isso o agente fala o wire direto).** O `mcp.Client` só declara elicitation quando recebe um `elicitation_callback`, e aí declara form e url juntos, não `{"elicitation": {"form": {}}}`. Trecho de `mcp/client/session.py` (mcp 2.2.0):

```python
elicitation = (
    types.ElicitationCapability(form=types.FormElicitationCapability(), url=types.UrlElicitationCapability())
    if self._elicitation_callback is not _default_elicitation_callback
    else None
)
```

E a capability declarada é imposta pelo SDK: um `_meta` passado por request com outra `clientCapabilities` é sobrescrito no envio. Além disso, com o callback o `Client.call_tool` responde a elicitation sozinho. Por isso [`agente/host_mcp.py`](agente/host_mcp.py) é um cliente Streamable HTTP enxuto sobre `httpx`: monta `_meta` e headers explicitamente, gera ids novos e devolve o `input_required` cru. O protocolo não foi reescrito: o formato é o de `exemplos/wire/`, e o servidor (SDK oficial) valida cada request.

**Sem LLM.** O pedido é interpretado por expressão regular e a decisão é por regra. Nenhum SDK de provedor de LLM aparece nas dependências. O mesmo pedido produz sempre a mesma resposta.

## Saída do validador

Última execução, com os dois processos recém-iniciados a partir de um clone limpo (Python 3.10, código de saída 0):

```
trace-id desta execucao: 9469496e64512b2eb57f774a3d7c2422
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
