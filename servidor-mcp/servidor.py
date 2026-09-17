"""Central de Salas: servidor MCP em Streamable HTTP (revisao 2026-07-28).

Sem sessao: cada request traz no _meta a versao do protocolo e as capabilities
do cliente, e o SDK recusa (-32602, HTTP 400) o que vier sem elas. A reserva em
intervalo ocupado nao chama o cliente de volta (nao existe canal de volta): ela
termina a resposta com resultType=input_required, uma elicitation em form mode e
um requestState que o SDK sela com AES-256-GCM antes de sair do processo.

    REQUEST_STATE_SECRET=... python servidor.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

import uvicorn
from mcp.server.mcpserver import Context, MCPServer, RequestStateSecurity
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.shared.exceptions import MCPError
from mcp_types import (
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    ClientCapabilities,
    ElicitationCapability,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    FormElicitationCapability,
    InputRequiredResult,
    MissingRequiredClientCapabilityErrorData,
)
from pydantic import BaseModel

from dominio import ERRO_SEM_ALTERNATIVA, Agenda, ErroDeRegra, ler_politica

NOME = "central-de-salas"
VERSAO = "1.0.0"
URI_POLITICA = "politica://uso"

# Chave do inputRequests. Atribuida pelo servidor e selada no requestState: o
# cliente precisa devolver exatamente esta chave no inputResponses.
CHAVE_ESCOLHA = "escolha_de_sala"
MENSAGEM_ESCOLHA = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."

# Validade do requestState: dentro da faixa de 5 a 30 minutos pedida pelo enunciado.
TTL_REQUEST_STATE = 10 * 60
TAMANHO_MINIMO_SEGREDO = 32

log = logging.getLogger("servidor-mcp")


# --- contratos de saida (viram outputSchema das tools) ----------------------


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


# --- chave de integridade do requestState -----------------------------------


def carregar_segredo() -> bytes:
    """Le REQUEST_STATE_SECRET. Aceita hex (como o gerado por secrets.token_hex) ou texto bruto."""
    bruto = os.environ.get("REQUEST_STATE_SECRET", "").strip()
    if not bruto:
        sys.exit(
            "REQUEST_STATE_SECRET nao definido. Gere um com:\n"
            '  export REQUEST_STATE_SECRET="$(python3 -c \'import secrets; print(secrets.token_hex(32))\')"'
        )
    try:
        chave = bytes.fromhex(bruto)
    except ValueError:
        chave = bruto.encode()
    if len(chave) < TAMANHO_MINIMO_SEGREDO:
        sys.exit(f"REQUEST_STATE_SECRET precisa de pelo menos {TAMANHO_MINIMO_SEGREDO} bytes; veio com {len(chave)}.")
    return chave


# --- servidor ------------------------------------------------------------------

agenda = Agenda()

mcp = MCPServer(
    name=NOME,
    version=VERSAO,
    instructions="Reserva de salas de reuniao da Hill Valley Tech.",
    # O SDK instala um RequestStateBoundary: todo requestState de saida e selado
    # (AES-256-GCM, chave derivada por HKDF, expiracao, ligado a tool e aos
    # argumentos do request) e todo requestState de entrada e verificado antes de
    # chegar na tool. Falhou a verificacao ou expirou: -32602.
    request_state_security=RequestStateSecurity(keys=[carregar_segredo()], ttl=TTL_REQUEST_STATE),
)


@mcp.tool(name="listar_salas", description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**vars(s)) for s in agenda.salas.values()])


@mcp.tool(
    name="consultar_disponibilidade",
    description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.",
)
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    try:
        encontrada, ini, fin = agenda.validar(sala, inicio, fim)
    except ErroDeRegra as erro:
        raise ToolError(str(erro)) from None
    conflitos = agenda.conflitos(encontrada.id, ini, fin)
    return Disponibilidade(
        sala=encontrada.id,
        livre=not conflitos,
        conflitos=[ConflitoOut(id=r.id, inicio=r.inicio, fim=r.fim, responsavel=r.responsavel) for r in conflitos],
    )


@mcp.tool(
    name="reservar_sala",
    description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.",
)
def reservar_sala(sala: str, inicio: str, fim: str, responsavel: str, ctx: Context) -> ReservaOut | InputRequiredResult:
    try:
        if ctx.request_state is not None:
            return _retomar(ctx)
        return _reservar(sala, inicio, fim, responsavel, ctx)
    except ErroDeRegra as erro:
        raise ToolError(str(erro)) from None


def _reservar(sala: str, inicio: str, fim: str, responsavel: str, ctx: Context) -> ReservaOut | InputRequiredResult:
    """Primeira rodada: reserva direto se livre, senao pede a escolha via input_required."""
    pedida, ini, fin = agenda.validar(sala, inicio, fim)
    if not agenda.conflitos(pedida.id, ini, fin):
        return _confirmar(pedida.id, inicio, fim, responsavel)

    alternativas = agenda.alternativas(pedida, ini, fin)
    if not alternativas:
        raise ErroDeRegra(ERRO_SEM_ALTERNATIVA)

    _exigir_elicitation_form(ctx)
    # Tudo que a retomada precisa vai aqui dentro, e nada fica em memoria: o
    # retry funciona mesmo depois de um restart. O SDK sela este texto na saida.
    selado = {
        "sala": sala,
        "inicio": inicio,
        "fim": fim,
        "responsavel": responsavel,
        "alternativas": alternativas,
    }
    return _pedir_escolha(selado)


def _retomar(ctx: Context) -> ReservaOut | InputRequiredResult:
    """Retry do MRTR. O requestState ja chega verificado pelo SDK; os argumentos do
    retry nao sao usados: o pedido e reconstruido a partir do que foi selado."""
    assert ctx.request_state is not None
    selado: dict[str, Any] = json.loads(ctx.request_state)
    resposta = (ctx.input_responses or {}).get(CHAVE_ESCOLHA)

    if not isinstance(resposta, ElicitResult):
        # Sem resposta para a pergunta que foi feita: pergunta de novo.
        return _pedir_escolha(selado)
    if resposta.action in ("decline", "cancel"):
        motivo = "recusado" if resposta.action == "decline" else "cancelado"
        return ReservaOut(reservado=False, motivo=motivo)

    escolhida = (resposta.content or {}).get("sala")
    if escolhida not in selado["alternativas"]:
        # Escolha fora do que foi oferecido: repete a mesma pergunta.
        return _pedir_escolha(selado)

    _, ini, fin = agenda.validar(escolhida, selado["inicio"], selado["fim"])
    if agenda.conflitos(escolhida, ini, fin):
        raise ErroDeRegra(f"Sala escolhida ficou ocupada no intervalo: {escolhida}")
    return _confirmar(escolhida, selado["inicio"], selado["fim"], selado["responsavel"])


def _pedir_escolha(selado: dict[str, Any]) -> InputRequiredResult:
    alternativas: list[str] = selado["alternativas"]
    esquema = {
        "type": "object",
        "properties": {
            "sala": {
                "type": "string",
                "title": "Sala",
                "description": "Sala alternativa escolhida",
                "enum": alternativas,
            }
        },
        "required": ["sala"],
    }
    pergunta = ElicitRequest(params=ElicitRequestFormParams(message=MENSAGEM_ESCOLHA, requested_schema=esquema))
    return InputRequiredResult(input_requests={CHAVE_ESCOLHA: pergunta}, request_state=json.dumps(selado))


def _confirmar(sala: str, inicio: str, fim: str, responsavel: str) -> ReservaOut:
    reserva = agenda.criar(sala, inicio, fim, responsavel)
    log.info("reserva criada: %s sala=%s inicio=%s fim=%s", reserva.id, sala, inicio, fim)
    return ReservaOut(
        reserva=reserva.id,
        reservado=True,
        sala=reserva.sala,
        inicio=reserva.inicio,
        fim=reserva.fim,
        responsavel=reserva.responsavel,
        politica=agenda.politica,
    )


def _exigir_elicitation_form(ctx: Context) -> None:
    """Sem elicitation em form mode declarada neste request, nao ha pergunta: -32021.

    Um `elicitation: {}` sem modos conta como form (compatibilidade da spec); so url nao conta.
    """
    capabilities = ctx.client_capabilities
    elicitation = capabilities.elicitation if capabilities is not None else None
    if elicitation is not None and (elicitation.form is not None or elicitation.url is None):
        return
    requeridas = ClientCapabilities(elicitation=ElicitationCapability(form=FormElicitationCapability()))
    dados = MissingRequiredClientCapabilityErrorData(required_capabilities=requeridas)
    raise MCPError(
        code=MISSING_REQUIRED_CLIENT_CAPABILITY,
        message="Client did not declare the form elicitation capability required by tool 'reservar_sala'",
        data=dados.model_dump(by_alias=True, mode="json", exclude_none=True),
    )


@mcp.resource(URI_POLITICA, name="politica-de-uso", title="Politica de uso das salas", mime_type="text/markdown")
def politica_de_uso() -> str:
    try:
        return ler_politica()
    except OSError as erro:
        raise ResourceError("politica de uso indisponivel") from erro


# --- log de requests no stderr ------------------------------------------------


class RegistroDeRequests:
    """Middleware ASGI: registra metodo, id, alvo e traceparent de cada request, e o desfecho.

    Fica por fora do transporte do SDK, entao registra tambem o que o SDK recusa
    antes de despachar (por exemplo, _meta incompleto).
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        recebidas: list[dict] = []
        corpo = b""
        while True:
            mensagem = await receive()
            recebidas.append(mensagem)
            if mensagem["type"] != "http.request":
                break
            corpo += mensagem.get("body", b"")
            if not mensagem.get("more_body", False):
                break

        pedido, ident = _resumo_do_pedido(corpo)
        log.info("<- %s", pedido)

        async def reenviar() -> dict:
            return recebidas.pop(0) if recebidas else await receive()

        estado = {"status": 0, "corpo": b""}

        async def capturar(mensagem: dict) -> None:
            if mensagem["type"] == "http.response.start":
                estado["status"] = mensagem["status"]
            elif mensagem["type"] == "http.response.body":
                estado["corpo"] += mensagem.get("body", b"")
            await send(mensagem)

        await self.app(scope, reenviar, capturar)
        log.info("-> %s http=%s %s", ident, estado["status"], _resumo_da_resposta(estado["corpo"]))


def _resumo_do_pedido(corpo: bytes) -> tuple[str, str]:
    """Devolve a linha de log do pedido e o par metodo/id usado na linha da resposta."""
    try:
        mensagem = json.loads(corpo)
    except ValueError:
        mensagem = None
    if not isinstance(mensagem, dict):
        return "method=? id=? corpo-invalido", "method=? id=?"
    params = mensagem.get("params") if isinstance(mensagem.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    ident = f"method={mensagem.get('method')} id={mensagem.get('id')}"
    partes = [ident]
    alvo = params.get("name") or params.get("uri")
    if alvo:
        partes.append(f"name={alvo}")
    if "requestState" in params:
        partes.append("retry=sim")
    partes.append(f"traceparent={meta.get('traceparent', '-')}")
    return " ".join(partes), ident


def _resumo_da_resposta(corpo: bytes) -> str:
    try:
        mensagem = json.loads(corpo)
    except ValueError:
        return ""
    if not isinstance(mensagem, dict):
        return ""
    if isinstance(mensagem.get("error"), dict):
        return f"error={mensagem['error'].get('code')}"
    resultado = mensagem.get("result") if isinstance(mensagem.get("result"), dict) else {}
    resumo = f"resultType={resultado.get('resultType', '-')}"
    if resultado.get("isError"):
        resumo += " isError=true"
    return resumo


def main() -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    porta = int(os.environ.get("MCP_PORT", "7301"))
    app = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True, host=host)
    log.info("servidor MCP em http://%s:%s/mcp", host, porta)
    uvicorn.run(RegistroDeRequests(app), host=host, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
