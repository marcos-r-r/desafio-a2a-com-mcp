"""O agente como host MCP: um cliente Streamable HTTP que fala o wire da revisao 2026-07-28.

Cada request e autocontido. Nada de sessao: todo request leva no _meta a versao
do protocolo, as capabilities (elicitation em form mode) e o traceparent da Task,
e nos headers o espelho do corpo (MCP-Protocol-Version, Mcp-Method, Mcp-Name).

Este cliente nunca responde elicitation sozinho. Um input_required volta cru para
quem chamou, com o requestState intacto, porque quem responde e o cliente A2A.
"""

from __future__ import annotations

import itertools
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any

import httpx

VERSAO_PROTOCOLO = "2026-07-28"
CLIENT_INFO = {"name": "agente-central-de-salas", "version": "1.0.0"}
CAPABILITIES = {"elicitation": {"form": {}}}

_TRACEPARENT = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")

log = logging.getLogger("agente.mcp")


class FalhaMCP(Exception):
    """O servidor MCP nao respondeu, ou respondeu com erro de protocolo (JSON-RPC error)."""

    def __init__(self, mensagem: str, codigo: int | None = None) -> None:
        super().__init__(mensagem)
        self.codigo = codigo


@dataclass(frozen=True)
class Rastro:
    """Trace context W3C de uma Task. O trace-id e fixo; cada request MCP ganha um span-id novo."""

    trace_id: str
    flags: str = "01"

    @classmethod
    def do_header(cls, valor: str | None) -> Rastro | None:
        casamento = _TRACEPARENT.match((valor or "").strip().lower())
        if not casamento or casamento.group(1) == "ff" or set(casamento.group(2)) == {"0"}:
            return None
        return cls(trace_id=casamento.group(2), flags=casamento.group(4))

    @classmethod
    def novo(cls) -> Rastro:
        return cls(trace_id=secrets.token_hex(16))

    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{secrets.token_hex(8)}-{self.flags}"


class ClienteMCP:
    """Cliente MCP de verdade, por HTTP. O objeto vive entre chamadas; o protocolo nao guarda estado."""

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        self.url = url
        self._http = httpx.AsyncClient(timeout=timeout)
        # ids JSON-RPC nunca se repetem neste processo: o retry do MRTR sempre sai com id novo.
        self._ids = itertools.count(1)

    async def fechar(self) -> None:
        await self._http.aclose()

    async def listar_tools(self, rastro: Rastro) -> list[dict[str, Any]]:
        resultado = await self._request("tools/list", {}, rastro)
        return list(resultado.get("tools") or [])

    async def ler_resource(self, uri: str, rastro: Rastro) -> list[dict[str, Any]]:
        resultado = await self._request("resources/read", {"uri": uri}, rastro, nome=uri)
        return list(resultado.get("contents") or [])

    async def chamar_tool(
        self,
        nome: str,
        argumentos: dict[str, Any],
        rastro: Rastro,
        *,
        input_responses: dict[str, Any] | None = None,
        request_state: str | None = None,
    ) -> dict[str, Any]:
        """tools/call. Devolve o result cru, seja complete ou input_required."""
        params: dict[str, Any] = {"name": nome, "arguments": argumentos}
        if input_responses is not None:
            params["inputResponses"] = input_responses
        if request_state is not None:
            # Ecoado byte a byte: o agente nao abre, nao interpreta e nao reconstroi o requestState.
            params["requestState"] = request_state
        return await self._request("tools/call", params, rastro, nome=nome)

    async def _request(self, metodo: str, params: dict[str, Any], rastro: Rastro, nome: str | None = None) -> dict[str, Any]:
        id_ = next(self._ids)
        traceparent = rastro.traceparent()
        corpo = {
            "jsonrpc": "2.0",
            "id": id_,
            "method": metodo,
            "params": {
                **params,
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": VERSAO_PROTOCOLO,
                    "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
                    "io.modelcontextprotocol/clientCapabilities": CAPABILITIES,
                    "traceparent": traceparent,
                },
            },
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": VERSAO_PROTOCOLO,
            "Mcp-Method": metodo,
        }
        if nome is not None:
            headers["Mcp-Name"] = nome

        log.info("-> mcp %s id=%s%s traceparent=%s", metodo, id_, f" name={nome}" if nome else "", traceparent)
        try:
            resposta = await self._http.post(self.url, json=corpo, headers=headers)
            mensagem = resposta.json()
        except (httpx.HTTPError, ValueError) as erro:
            raise FalhaMCP(f"Servidor MCP indisponivel: {erro.__class__.__name__}") from erro

        if not isinstance(mensagem, dict):
            raise FalhaMCP("Resposta MCP invalida")
        if isinstance(mensagem.get("error"), dict):
            erro = mensagem["error"]
            log.info("<- mcp %s id=%s error=%s", metodo, id_, erro.get("code"))
            raise FalhaMCP(f"Erro do servidor MCP ({erro.get('code')}): {erro.get('message')}", erro.get("code"))
        resultado = mensagem.get("result")
        if not isinstance(resultado, dict):
            raise FalhaMCP("Resposta MCP sem result")
        log.info("<- mcp %s id=%s resultType=%s", metodo, id_, resultado.get("resultType"))
        return resultado
