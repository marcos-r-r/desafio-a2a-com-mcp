"""Central de Salas: agente A2A v1.0 (binding JSON-RPC) que e host MCP por dentro.

Por fora: Agent Card no well-known URI, SendMessage e GetTask em /a2a.
Por dentro: descobre as tools do servidor MCP, le a politica e chama reservar_sala.
No meio, a ponte: input_required do MCP vira TASK_STATE_INPUT_REQUIRED no A2A, e
a resposta do cliente A2A vira o retry MCP com inputResponses e o requestState.

Sem LLM: o pedido tem formato fixo e a decisao e por regra.

    python agente.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import uvicorn
from a2a.helpers.proto_helpers import new_task, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.context import ServerCallContext
from a2a.server.routes import DefaultServerCallContextBuilder, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.utils.constants import PROTOCOL_VERSION_1_0, VERSION_HEADER
from a2a.utils.errors import InvalidParamsError
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    Message,
    SendMessageRequest,
    Task,
    TaskState,
)
from google.protobuf.json_format import MessageToDict
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from host_mcp import ClienteMCP, FalhaMCP, Rastro

TOOL_RESERVA = "reservar_sala"
URI_POLITICA = "politica://uso"
EXEMPLO_PEDIDO = (
    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
)

PEDIDO = re.compile(r"^reservar\s+sala=(\S+)\s+inicio=(\S+)\s+fim=(\S+)\s+responsavel=(.+)$")
ESCOLHA = re.compile(r"^escolha=(\S+)$")
RECUSAR = "recusar"

log = logging.getLogger("agente")


# --- estado da ponte, por Task ---------------------------------------------------


@dataclass
class Pausa:
    """Tudo que o agente guarda de uma Task interrompida. Nunca sai do processo.

    O requestState e opaco: e guardado e devolvido tal como veio, sem ser aberto.
    """

    argumentos: dict[str, Any]
    chave: str
    campo: str
    alternativas: list[str]
    request_state: str
    politica: str
    rastro: Rastro


class Pausas:
    """Tasks em TASK_STATE_INPUT_REQUIRED, indexadas pelo id da Task."""

    def __init__(self) -> None:
        self._por_task: dict[str, Pausa] = {}

    def guardar(self, task_id: str, pausa: Pausa) -> None:
        self._por_task[task_id] = pausa

    def retirar(self, task_id: str) -> Pausa | None:
        return self._por_task.pop(task_id, None)

    def consultar(self, task_id: str) -> Pausa | None:
        return self._por_task.get(task_id)


class PedidoInvalido(Exception):
    pass


# --- executor ------------------------------------------------------------------------


class AgenteDeSalas(AgentExecutor):
    def __init__(self, mcp: ClienteMCP) -> None:
        self.mcp = mcp
        self.pausas = Pausas()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        assert context.task_id and context.context_id
        tarefa = context.current_task
        # A continuacao pode chegar so com taskId: o contextId vale o da Task existente.
        context_id = tarefa.context_id if tarefa is not None and tarefa.context_id else context.context_id
        updater = TaskUpdater(event_queue, context.task_id, context_id)
        texto = context.get_user_input().strip()
        rastro_da_chamada = Rastro.do_header(_header(context, "traceparent"))

        if tarefa is not None and tarefa.status.state == TaskState.TASK_STATE_INPUT_REQUIRED:
            await self._continuar(context.task_id, texto, rastro_da_chamada, updater)
        else:
            # Task nova: nasce em TASK_STATE_SUBMITTED, com id e contextId proprios.
            historico = [context.message] if context.message is not None else []
            await event_queue.enqueue_event(
                new_task(context.task_id, context_id, TaskState.TASK_STATE_SUBMITTED, history=historico)
            )
            await self._iniciar(context.task_id, texto, rastro_da_chamada or Rastro.novo(), updater)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        assert context.task_id and context.context_id
        tarefa = context.current_task
        context_id = tarefa.context_id if tarefa is not None and tarefa.context_id else context.context_id
        self.pausas.retirar(context.task_id)
        await TaskUpdater(event_queue, context.task_id, context_id).cancel()

    async def _iniciar(self, task_id: str, texto: str, rastro: Rastro, updater: TaskUpdater) -> None:
        casamento = PEDIDO.match(texto)
        if not casamento:
            await _encerrar(updater, TaskState.TASK_STATE_REJECTED, f"Pedido invalido. Use o formato: {EXEMPLO_PEDIDO}")
            return
        sala, inicio, fim, responsavel = casamento.groups()
        argumentos = {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel.strip()}
        await updater.start_work()

        try:
            # Descoberta em runtime: nada de lista de tools fixa no codigo.
            tools = await self.mcp.listar_tools(rastro)
            if TOOL_RESERVA not in {t.get("name") for t in tools}:
                await _encerrar(updater, TaskState.TASK_STATE_FAILED, f"O servidor MCP nao oferece a tool {TOOL_RESERVA}")
                return
            politica = await self._versao_da_politica(rastro)
            resultado = await self.mcp.chamar_tool(TOOL_RESERVA, argumentos, rastro)
        except FalhaMCP as erro:
            await _encerrar(updater, TaskState.TASK_STATE_FAILED, str(erro))
            return

        await self._traduzir(task_id, resultado, argumentos, politica, rastro, updater)

    async def _continuar(self, task_id: str, texto: str, rastro_da_chamada: Rastro | None, updater: TaskUpdater) -> None:
        pausa = self.pausas.consultar(task_id)
        if pausa is None:
            await _encerrar(updater, TaskState.TASK_STATE_FAILED, "A pausa desta Task nao existe mais neste agente")
            return

        casamento = ESCOLHA.match(texto)
        escolha = casamento.group(1) if casamento else None
        if escolha is None or (escolha != RECUSAR and escolha not in pausa.alternativas):
            # Fora do enum: a Task continua pausada e a pergunta se repete.
            await updater.requires_input(_mensagem(updater, _linha_de_alternativas(pausa.alternativas)))
            return

        self.pausas.retirar(task_id)
        await updater.start_work()
        if escolha == RECUSAR:
            resposta: dict[str, Any] = {"action": "decline"}
        else:
            resposta = {"action": "accept", "content": {pausa.campo: escolha}}

        # A PONTE, volta: o mesmo tools/call, com id novo, a mesma chave no
        # inputResponses e o requestState ecoado sem modificacao.
        rastro = rastro_da_chamada or pausa.rastro
        try:
            resultado = await self.mcp.chamar_tool(
                TOOL_RESERVA,
                pausa.argumentos,
                rastro,
                input_responses={pausa.chave: resposta},
                request_state=pausa.request_state,
            )
        except FalhaMCP as erro:
            await _encerrar(updater, TaskState.TASK_STATE_FAILED, str(erro))
            return

        await self._traduzir(task_id, resultado, pausa.argumentos, pausa.politica, rastro, updater)

    async def _traduzir(
        self,
        task_id: str,
        resultado: dict[str, Any],
        argumentos: dict[str, Any],
        politica: str,
        rastro: Rastro,
        updater: TaskUpdater,
    ) -> None:
        """Traduz um result MCP em estado de Task. Nenhuma regra de sala e decidida aqui."""
        if resultado.get("resultType") == "input_required":
            # A PONTE, ida: input_required do MCP vira TASK_STATE_INPUT_REQUIRED no A2A.
            try:
                chave, campo, alternativas = _ler_elicitation(resultado)
            except PedidoInvalido as erro:
                await _encerrar(updater, TaskState.TASK_STATE_FAILED, str(erro))
                return
            self.pausas.guardar(
                task_id,
                Pausa(
                    argumentos=argumentos,
                    chave=chave,
                    campo=campo,
                    alternativas=alternativas,
                    request_state=resultado["requestState"],
                    politica=politica,
                    rastro=rastro,
                ),
            )
            await updater.requires_input(_mensagem(updater, _linha_de_alternativas(alternativas)))
            return

        if resultado.get("isError"):
            texto = " ".join(p.get("text", "") for p in resultado.get("content") or [] if p.get("type", "text") == "text")
            await _encerrar(updater, TaskState.TASK_STATE_FAILED, texto.strip() or "A tool falhou sem mensagem")
            return

        dados = resultado.get("structuredContent") or {}
        if dados.get("reservado") is False:
            motivo = dados.get("motivo") or "recusado"
            await _encerrar(updater, TaskState.TASK_STATE_CANCELED, f"Reserva nao realizada: {motivo}.")
            return

        reserva = {
            "reserva": dados.get("reserva"),
            "sala": dados.get("sala"),
            "inicio": dados.get("inicio"),
            "fim": dados.get("fim"),
            "responsavel": dados.get("responsavel"),
            "politica": politica,
        }
        await updater.add_artifact([new_text_part(json.dumps(reserva))], name="reserva")
        await _encerrar(updater, TaskState.TASK_STATE_COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")

    async def _versao_da_politica(self, rastro: Rastro) -> str:
        """Le o resource da politica e extrai a versao declarada na primeira linha."""
        conteudos = await self.mcp.ler_resource(URI_POLITICA, rastro)
        texto = next((c.get("text", "") for c in conteudos if c.get("uri") == URI_POLITICA), "")
        primeira = texto.splitlines()[0] if texto else ""
        chave, _, valor = primeira.partition(":")
        return valor.strip() if chave.strip() == "versao" else ""


def _ler_elicitation(resultado: dict[str, Any]) -> tuple[str, str, list[str]]:
    """Extrai chave, campo e opcoes da unica elicitation em form mode. So traduz protocolo."""
    pedidos = resultado.get("inputRequests") or {}
    if len(pedidos) != 1 or not resultado.get("requestState"):
        raise PedidoInvalido("O servidor MCP pediu informacao num formato que este agente nao atende")
    chave, pedido = next(iter(pedidos.items()))
    params = pedido.get("params") or {}
    if pedido.get("method") != "elicitation/create" or params.get("mode", "form") != "form":
        raise PedidoInvalido("O servidor MCP pediu informacao num formato que este agente nao atende")
    propriedades = (params.get("requestedSchema") or {}).get("properties") or {}
    for campo, esquema in propriedades.items():
        opcoes = esquema.get("enum") or ([esquema["const"]] if "const" in esquema else [])
        if opcoes:
            return chave, campo, [str(o) for o in opcoes]
    raise PedidoInvalido("O servidor MCP pediu uma escolha sem opcoes")


def _linha_de_alternativas(alternativas: list[str]) -> str:
    return "alternativas: " + ", ".join(alternativas)


async def _encerrar(updater: TaskUpdater, estado: TaskState, texto: str) -> None:
    """Publica a mensagem final e o estado terminal, deixando a mensagem no status e no history.

    O a2a-sdk so move a mensagem do status para o history quando chega o status
    seguinte, e estado terminal nao tem seguinte. Por isso a mesma mensagem sai
    primeiro num status WORKING e depois no terminal.
    """
    mensagem = _mensagem(updater, texto)
    await updater.update_status(TaskState.TASK_STATE_WORKING, message=mensagem)
    await updater.update_status(estado, message=mensagem)


def _mensagem(updater: TaskUpdater, texto: str):
    return updater.new_agent_message([new_text_part(texto)])


def _header(context: RequestContext, nome: str) -> str | None:
    call = context.call_context
    headers = call.state.get("headers") if call is not None else None
    if not isinstance(headers, dict):
        return None
    return next((v for k, v in headers.items() if k.lower() == nome), None)


# --- Agent Card e aplicacao ----------------------------------------------------------


def montar_card(url_a2a: str) -> AgentCard:
    return AgentCard(
        name="Central de Salas",
        description="Reserva salas de reuniao da Hill Valley Tech.",
        provider=AgentProvider(organization="Hill Valley Tech", url="https://hillvalley.example"),
        version="1.0.0",
        supported_interfaces=[AgentInterface(url=url_a2a, protocol_binding="JSONRPC", protocol_version="1.0")],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False, extended_agent_card=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="reservar-sala",
                name="Reservar sala",
                description="Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                tags=["salas", "agenda"],
                input_modes=["text/plain"],
                output_modes=["text/plain"],
                examples=[EXEMPLO_PEDIDO],
            )
        ],
    )


def card_como_json(card: AgentCard) -> dict[str, Any]:
    """Serializa o card na forma v1.0, mantendo as capabilities falsas visiveis."""
    dados = MessageToDict(card)
    dados["capabilities"] = {
        "streaming": card.capabilities.streaming,
        "pushNotifications": card.capabilities.push_notifications,
        "extendedAgentCard": card.capabilities.extended_agent_card,
    }
    return dados


ESTADOS_TERMINAIS = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_REJECTED,
}


class HandlerDeSalas(DefaultRequestHandler):
    """Estado terminal e definitivo: mensagem para Task terminal e recusada antes de chegar ao executor."""

    async def on_message_send(self, params: SendMessageRequest, context: ServerCallContext) -> Message | Task:
        task_id = params.message.task_id
        if task_id:
            tarefa = await self.task_store.get(task_id, context)
            if tarefa is not None and tarefa.status.state in ESTADOS_TERMINAIS:
                raise InvalidParamsError(
                    message=f"Task {task_id} is in terminal state: {TaskState.Name(tarefa.status.state)}"
                )
        return await super().on_message_send(params, context)


class ContextoV1(DefaultServerCallContextBuilder):
    """Este endpoint so atende o binding v1.0 (o card declara protocolVersion 1.0).

    O a2a-sdk interpreta a ausencia do header A2A-Version como v0.3 e recusa o
    request; aqui a ausencia vale como a versao que o card anuncia.
    """

    def build(self, request: Request) -> ServerCallContext:
        contexto = super().build(request)
        headers = contexto.state.setdefault("headers", {})
        if not any(k.lower() == VERSION_HEADER.lower() for k in headers):
            headers[VERSION_HEADER.lower()] = PROTOCOL_VERSION_1_0
        return contexto


class RegistroA2A:
    """Middleware ASGI: registra no stderr o metodo A2A e o traceparent recebido."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            log.info("<- %s %s traceparent=%s", scope.get("method"), scope.get("path"), headers.get("traceparent", "-"))
        await self.app(scope, receive, send)


def montar_app() -> Starlette:
    host = os.environ.get("AGENTE_HOST", "127.0.0.1")
    porta = int(os.environ.get("AGENTE_PORT", "7300"))
    url_publica = os.environ.get("AGENTE_URL_PUBLICA", f"http://localhost:{porta}")
    url_mcp = os.environ.get("MCP_URL", "http://localhost:7301/mcp")

    card = montar_card(f"{url_publica.rstrip('/')}/a2a")
    cliente = ClienteMCP(url_mcp)
    handler = HandlerDeSalas(
        agent_executor=AgenteDeSalas(cliente),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    corpo_do_card = card_como_json(card)

    async def agent_card(_: Request) -> JSONResponse:
        return JSONResponse(corpo_do_card)

    @asynccontextmanager
    async def ciclo_de_vida(_: Starlette):
        log.info("agente A2A em http://%s:%s/a2a, servidor MCP em %s", host, porta, url_mcp)
        yield
        await handler.aclose()
        await cliente.fechar()

    rotas = [Route("/.well-known/agent-card.json", agent_card, methods=["GET"])]
    rotas += create_jsonrpc_routes(handler, rpc_url="/a2a", context_builder=ContextoV1())
    return Starlette(routes=rotas, lifespan=ciclo_de_vida)


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    host = os.environ.get("AGENTE_HOST", "127.0.0.1")
    porta = int(os.environ.get("AGENTE_PORT", "7300"))
    uvicorn.run(RegistroA2A(montar_app()), host=host, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
