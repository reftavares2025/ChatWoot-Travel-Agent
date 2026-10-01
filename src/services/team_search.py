"""Busca de cliente/passageiro pra equipe (Olívia) — Paula, Flávia e quem mais for autorizado
mandam texto ou áudio pro WhatsApp central da Captur pedindo pra achar um cliente, e a IA
procura no que o SISTEMA DA CAPTUR tem gravado: contatos, histórico de conversas e contas a
receber (destino, valor, atendente).

Limite real: só busca o que passou pelo nosso WhatsApp/Chatwoot desde que o bot entrou no ar.
Cliente que só existe no Monde (ou em listas antigas de antes do sistema) não aparece aqui —
não há integração com o Monde.
"""
import os
from dataclasses import dataclass

from sqlalchemy import select, desc, or_
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.groq import GroqModel
from pydantic_ai.providers.groq import GroqProvider

from src.database.models import ClienteContato, ContaReceber, Conversa


@dataclass
class BuscaDeps:
    db: AsyncSession


_model = GroqModel("openai/gpt-oss-120b", provider=GroqProvider(api_key=os.getenv("GROQ_API_KEY")))

agent = Agent(
    _model,
    deps_type=BuscaDeps,
    system_prompt=(
        "Você é a Olívia ajudando a equipe da Captur (Paula, Flávia e outras atendentes) a localizar "
        "um cliente/passageiro no histórico da agência. Use a ferramenta buscar_cliente com o nome, "
        "telefone, destino ou qualquer trecho que a pessoa der — mesmo que incompleto ou com erro de "
        "digitação, tente a busca. Responda SOMENTE com o que a ferramenta realmente encontrou — nunca "
        "invente dado de cliente. Se não achar nada, diga claramente que não encontrou nos registros do "
        "sistema e que pode ser um cliente de antes da Captur usar esse WhatsApp/Chatwoot (nesse caso, "
        "sugira checar no Monde). Responda em português do Brasil, direto e objetivo."
    ),
)


async def buscar_cliente_raw(db: AsyncSession, termo: str) -> str:
    """Lógica de busca reaproveitada pelo tool local (equipe geral) e pela IA financeira (Paula)."""
    termo_like = f"%{termo}%"

    contatos = (await db.execute(
        select(ClienteContato)
        .where(or_(ClienteContato.nome.ilike(termo_like), ClienteContato.telefone.ilike(termo_like)))
        .limit(5)
    )).scalars().all()

    contas = (await db.execute(
        select(ContaReceber)
        .where(or_(
            ContaReceber.cliente_nome.ilike(termo_like),
            ContaReceber.cliente_telefone.ilike(termo_like),
            ContaReceber.destino.ilike(termo_like),
        ))
        .order_by(desc(ContaReceber.criado_em))
        .limit(5)
    )).scalars().all()

    conversas = (await db.execute(
        select(Conversa)
        .where(or_(Conversa.texto.ilike(termo_like), Conversa.remetente.ilike(termo_like)))
        .order_by(desc(Conversa.id))
        .limit(5)
    )).scalars().all()

    if not contatos and not contas and not conversas:
        return f"Nada encontrado nos registros do sistema pra '{termo}'."

    linhas = []
    if contatos:
        linhas.append("CONTATOS:")
        for c in contatos:
            linhas.append(f"- {c.nome} | telefone: {c.telefone} | cadastrado em {c.criado_em.strftime('%d/%m/%Y')}")

    if contas:
        linhas.append("\nVENDAS/CONTAS A RECEBER:")
        for c in contas:
            linhas.append(
                f"- {c.cliente_nome} | telefone: {c.cliente_telefone or 'não informado'} | "
                f"destino: {c.destino or 'não informado'} | valor: R$ {c.valor_total} | "
                f"status: {c.status} | atendente: {c.atendente_nome or 'não informado'} | "
                f"data: {c.criado_em.strftime('%d/%m/%Y')}"
            )

    if conversas:
        linhas.append("\nTRECHOS DE CONVERSA ENCONTRADOS:")
        for c in conversas:
            linhas.append(f"- [{c.timestamp.strftime('%d/%m/%Y %H:%M')}] {c.remetente}: {c.texto[:150]}")

    return "\n".join(linhas)


@agent.tool
async def buscar_cliente(ctx: RunContext[BuscaDeps], termo: str) -> str:
    """Busca um cliente/passageiro pelo nome, telefone, destino ou trecho de mensagem.
    Procura em três lugares: contatos sincronizados, contas a receber (vendas) e o
    histórico de conversas do WhatsApp/Chatwoot."""
    return await buscar_cliente_raw(ctx.deps.db, termo)


async def perguntar(db: AsyncSession, pergunta: str) -> str:
    """Ponto de entrada usado pelo webhook do WhatsApp quando alguém da equipe pede pra achar um cliente."""
    try:
        resultado = await agent.run(pergunta, deps=BuscaDeps(db=db))
        return resultado.output
    except Exception as e:
        print(f"[Busca de Cliente] Erro ao processar pedido: {e}")
        return "Tive um erro ao buscar agora, tenta de novo em instantes."
