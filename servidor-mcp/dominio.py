"""Dominio das salas: dados do starter, politica de uso e reservas em memoria.

Tudo aqui e deliberadamente simples. Quem decide conflito, politica e
alternativas e este modulo, dentro do servidor MCP; o agente nunca decide nada
disso.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

DADOS = Path(__file__).resolve().parent.parent / "dados"

FUSO_SAO_PAULO = timezone(timedelta(hours=-3))
ABERTURA = 8
FECHAMENTO = 20
DURACAO_MAXIMA = timedelta(hours=2)
MAX_ALTERNATIVAS = 3

ERRO_SALA = "Sala inexistente: {sala}"
ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"
ERRO_DATA = "Data invalida: use ISO 8601 com fuso, por exemplo 2026-11-03T14:00:00-03:00"


class ErroDeRegra(Exception):
    """Violacao de regra de negocio. Vira erro de execucao da tool (isError)."""


@dataclass(frozen=True)
class Sala:
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


@dataclass(frozen=True)
class Reserva:
    id: str
    sala: str
    inicio: str
    fim: str
    responsavel: str

    def intervalo(self) -> tuple[datetime, datetime]:
        return datetime.fromisoformat(self.inicio), datetime.fromisoformat(self.fim)


def _ler_json(nome: str) -> list[dict]:
    return json.loads((DADOS / nome).read_text(encoding="utf-8"))


def ler_politica() -> str:
    return (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")


def versao_da_politica(texto: str) -> str:
    primeira = texto.splitlines()[0] if texto else ""
    chave, _, valor = primeira.partition(":")
    return valor.strip() if chave.strip() == "versao" else ""


class Agenda:
    """Salas fixas e reservas em memoria. Nao sobrevive a restart, por decisao do enunciado."""

    def __init__(self) -> None:
        self.salas: dict[str, Sala] = {s["id"]: Sala(**s) for s in _ler_json("salas.json")}
        self.reservas: list[Reserva] = [Reserva(**r) for r in _ler_json("reservas.json")]
        self.politica = versao_da_politica(ler_politica())

    def sala(self, sala_id: str) -> Sala:
        sala = self.salas.get(sala_id)
        if sala is None:
            raise ErroDeRegra(ERRO_SALA.format(sala=sala_id))
        return sala

    def validar(self, sala_id: str, inicio: str, fim: str) -> tuple[Sala, datetime, datetime]:
        """Aplica, nesta ordem: sala existe, datas legiveis, intervalo, janela e duracao."""
        sala = self.sala(sala_id)
        try:
            ini, fin = datetime.fromisoformat(inicio), datetime.fromisoformat(fim)
        except ValueError:
            raise ErroDeRegra(ERRO_DATA) from None
        if ini.tzinfo is None or fin.tzinfo is None:
            raise ErroDeRegra(ERRO_DATA)
        if fin <= ini:
            raise ErroDeRegra(ERRO_INTERVALO)
        local_ini = ini.astimezone(FUSO_SAO_PAULO)
        local_fim = fin.astimezone(FUSO_SAO_PAULO)
        abertura = local_ini.replace(hour=ABERTURA, minute=0, second=0, microsecond=0)
        fechamento = local_ini.replace(hour=FECHAMENTO, minute=0, second=0, microsecond=0)
        if local_ini < abertura or local_fim > fechamento:
            raise ErroDeRegra(ERRO_JANELA)
        if fin - ini > DURACAO_MAXIMA:
            raise ErroDeRegra(ERRO_DURACAO)
        return sala, ini, fin

    def conflitos(self, sala_id: str, ini: datetime, fin: datetime) -> list[Reserva]:
        encontrados = []
        for reserva in self.reservas:
            if reserva.sala != sala_id:
                continue
            r_ini, r_fim = reserva.intervalo()
            if r_ini < fin and ini < r_fim:
                encontrados.append(reserva)
        return encontrados

    def alternativas(self, sala: Sala, ini: datetime, fin: datetime) -> list[str]:
        """Salas livres no intervalo, com capacidade >= a pedida, por capacidade e depois id, no maximo tres."""
        candidatas = [
            s
            for s in self.salas.values()
            if s.id != sala.id and s.capacidade >= sala.capacidade and not self.conflitos(s.id, ini, fin)
        ]
        candidatas.sort(key=lambda s: (s.capacidade, s.id))
        return [s.id for s in candidatas[:MAX_ALTERNATIVAS]]

    def criar(self, sala_id: str, inicio: str, fim: str, responsavel: str) -> Reserva:
        numero = max((int(r.id.rsplit("-", 1)[-1]) for r in self.reservas), default=0) + 1
        reserva = Reserva(id=f"res-{numero:04d}", sala=sala_id, inicio=inicio, fim=fim, responsavel=responsavel)
        self.reservas.append(reserva)
        return reserva
