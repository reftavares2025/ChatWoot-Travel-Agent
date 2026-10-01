"""
Assistente Financeiro IA da Paula — conversa privada dentro do Chatwoot (inbox "Financeiro IA").

Regra de ouro: a IA NUNCA responde um valor financeiro de cabeça. Toda resposta sobre
dinheiro vem de uma tool que consulta o banco de dados real na hora. Se a pergunta pedir
algo que nenhuma tool cobre, a IA deve dizer isso claramente em vez de estimar.

Toda baixa de pagamento/recebimento exige o ID exato do lançamento (nunca "paga pro
fornecedor X" de forma ambígua) — reduz risco de baixar o registro errado.
"""

import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.groq import GroqModel
from pydantic_ai.providers.groq import GroqProvider

from config.settings import settings
from src.services import financeiro
from src.services.team_search import buscar_cliente_raw
from src.database.models import Conversa


@dataclass
class FinanceiroDeps:
    db: AsyncSession


def _fmt_reais(valor) -> str:
    try:
        return f"R$ {Decimal(valor):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    except (InvalidOperation, TypeError):
        return str(valor)


def _parse_data(data_str: Optional[str]) -> Optional[datetime]:
    if not data_str:
        return None
    try:
        return datetime.strptime(data_str, "%Y-%m-%d")
    except ValueError:
        return None


_model = GroqModel("openai/gpt-oss-120b", provider=GroqProvider(api_key=os.getenv("GROQ_API_KEY")))

agent = Agent(
    _model,
    deps_type=FinanceiroDeps,
    system_prompt=(
        "Você é o Assistente Financeiro privado da Paula, da Captur Agência de Viagens. "
        "Responda SOMENTE com base no resultado real das ferramentas disponíveis — nunca "
        "estime, arredonde de cabeça ou invente um valor financeiro. Se a pergunta pedir "
        "um dado que nenhuma ferramenta cobre, diga isso claramente em vez de supor. "
        "Datas no formato AAAA-MM-DD ao chamar ferramentas. Responda em português do Brasil, "
        "direto e objetivo, com os valores em Reais (R$ 1.234,56).\n\n"
        "Além do financeiro, você também guarda ideias soltas do Régis (ferramenta anotar_ideia) e "
        "pode propor automações do Chatwoot (ferramenta propor_automacao): quando ele descrever uma "
        "automação, resuma em texto claro o que ela vai fazer e pergunte se confirma antes de aplicar. "
        "Só chame aplicar_automacao depois que ele confirmar explicitamente (ex: 'sim', 'pode aplicar', "
        "'confirmo'). A regra criada vai sempre ficar DESATIVADA no Chatwoot — depois de criar, diga a "
        "ele exatamente o comando pra ativar (ex: 'ativa automação #7'), e chame a ferramenta "
        "ativar_automacao quando ele mandar esse comando."
    ),
)


@agent.tool
async def resumo_financeiro_geral(ctx: RunContext[FinanceiroDeps]) -> str:
    """Resumo financeiro completo e atual: saldo de caixa, contas a receber pendentes e contas a pagar pendentes."""
    r = await financeiro.resumo_financeiro(ctx.deps.db)
    saldos = r["saldos_contabeis"]
    linhas = ["SALDOS CONTÁBEIS (recalculados agora):"]
    for conta, valor in saldos.items():
        linhas.append(f"- {conta}: {_fmt_reais(valor)}")

    linhas.append(f"\nCONTAS A RECEBER PENDENTES ({len(r['contas_a_receber_pendentes'])}):")
    for c in r["contas_a_receber_pendentes"][:30]:
        falta = Decimal(c["valor_total"]) - Decimal(c["valor_recebido"])
        linhas.append(f"- #{c['id']} {c['cliente']}: falta receber {_fmt_reais(falta)} de {_fmt_reais(c['valor_total'])} (status: {c['status']}, vence: {c['vencimento'] or 'sem data'})")

    linhas.append(f"\nCONTAS A PAGAR PENDENTES ({len(r['contas_a_pagar_pendentes'])}):")
    for c in r["contas_a_pagar_pendentes"][:30]:
        linhas.append(f"- #{c['id']} {c['fornecedor']} ({c['categoria']}): {_fmt_reais(c['valor'])} (vence: {c['vencimento'] or 'sem data'})")

    return "\n".join(linhas)


@agent.tool
async def saldo_de_caixa(ctx: RunContext[FinanceiroDeps], data_inicio: Optional[str] = None, data_fim: Optional[str] = None) -> str:
    """Saldo de caixa (dinheiro que já entrou menos o que já saiu), opcionalmente filtrado por período (datas em AAAA-MM-DD)."""
    saldo = await financeiro.saldo_caixa(ctx.deps.db, _parse_data(data_inicio), _parse_data(data_fim))
    periodo = f" entre {data_inicio} e {data_fim}" if data_inicio or data_fim else " (desde o início)"
    return f"Saldo de caixa{periodo}: {_fmt_reais(saldo)}"


@agent.tool
async def registrar_despesa(
    ctx: RunContext[FinanceiroDeps],
    fornecedor: str,
    valor: float,
    categoria: str = "despesa_fixa",
    descricao: Optional[str] = None,
) -> str:
    """Registra uma nova conta a pagar. categoria deve ser 'despesa_fixa' (aluguel, luz, etc.) ou 'fornecedor_viagem' (consolidadora, hotel)."""
    conta = await financeiro.registrar_conta_pagar(
        ctx.deps.db, fornecedor=fornecedor, valor=Decimal(str(valor)), categoria=categoria, descricao=descricao
    )
    return f"Conta a pagar #{conta.id} registrada: {fornecedor} — {_fmt_reais(conta.valor)} ({categoria})."


@agent.tool
async def dar_baixa_conta_pagar(ctx: RunContext[FinanceiroDeps], conta_pagar_id: int) -> str:
    """Marca uma conta a pagar como PAGA. Exige o ID exato (peça pra Paula confirmar o ID antes se ela não tiver dado)."""
    conta = await financeiro.dar_baixa_pagamento(ctx.deps.db, conta_pagar_id)
    if not conta:
        return f"Não encontrei nenhuma conta a pagar com o ID #{conta_pagar_id}. Confirme o número antes de tentar de novo."
    if conta.status != "pago":
        return f"Não consegui confirmar a baixa da conta #{conta_pagar_id}."
    return f"Baixa confirmada: conta a pagar #{conta.id} ({conta.fornecedor}, {_fmt_reais(conta.valor)}) marcada como PAGA."


@agent.tool
async def dar_baixa_conta_receber(ctx: RunContext[FinanceiroDeps], conta_receber_id: int, valor_recebido: float) -> str:
    """Registra o recebimento (total ou parcial) de uma conta a receber. Exige o ID exato do lançamento."""
    conta = await financeiro.registrar_recebimento(ctx.deps.db, conta_receber_id, Decimal(str(valor_recebido)))
    if not conta:
        return f"Não encontrei nenhuma conta a receber com o ID #{conta_receber_id}. Confirme o número antes de tentar de novo."
    falta = Decimal(conta.valor_total) - Decimal(conta.valor_recebido)
    return (f"Recebimento registrado na conta #{conta.id} ({conta.cliente_nome}): "
            f"{_fmt_reais(valor_recebido)} recebidos agora. Status: {conta.status}. "
            f"Falta receber: {_fmt_reais(falta) if falta > 0 else 'nada, quitado'}.")


@agent.tool
async def anotar_ideia(ctx: RunContext[FinanceiroDeps], ideia: str) -> str:
    """Guarda uma ideia solta do Régis pra consultar depois. Use sempre que ele disser algo como
    'anota uma ideia', 'tive uma ideia' ou mencionar algo que parece um plano/melhoria futura."""
    registro = Conversa(remetente="regis", destinatario="ideias", texto=ideia, origem="ideia_regis")
    ctx.deps.db.add(registro)
    await ctx.deps.db.commit()
    return f"Ideia anotada (#{registro.id}): \"{ideia}\""


@agent.tool
async def listar_ideias(ctx: RunContext[FinanceiroDeps], limite: int = 20) -> str:
    """Lista as últimas ideias anotadas pelo Régis, mais recentes primeiro."""
    result = await ctx.deps.db.execute(
        select(Conversa).where(Conversa.origem == "ideia_regis").order_by(desc(Conversa.id)).limit(limite)
    )
    ideias = result.scalars().all()
    if not ideias:
        return "Nenhuma ideia anotada ainda."
    linhas = [f"- #{i.id} ({i.timestamp.strftime('%d/%m %H:%M')}): {i.texto}" for i in ideias]
    return "Últimas ideias anotadas:\n" + "\n".join(linhas)


@agent.tool
async def propor_automacao(ctx: RunContext[FinanceiroDeps], resumo_em_portugues: str) -> str:
    """Registra uma PROPOSTA de automação do Chatwoot em texto simples, sem aplicar nada ainda.
    Use isso primeiro sempre que o Régis descrever uma automação que ele quer, resumindo em
    português claro o que a regra vai fazer, e pergunte se ele confirma antes de chamar
    aplicar_automacao."""
    registro = Conversa(remetente="regis", destinatario="automacao_proposta", texto=resumo_em_portugues, origem="automacao_proposta")
    ctx.deps.db.add(registro)
    await ctx.deps.db.commit()
    return f"Proposta registrada (#{registro.id}): \"{resumo_em_portugues}\". Pergunte se ele confirma antes de aplicar."


@agent.tool
async def aplicar_automacao(
    ctx: RunContext[FinanceiroDeps],
    nome: str,
    event_name: str,
    condicoes: list[dict],
    acoes: list[dict],
) -> str:
    """Cria de verdade a automação no Chatwoot, SEMPRE desativada (active=false) — só chame isso
    depois que o Régis confirmar explicitamente a proposta feita por propor_automacao.

    event_name válido: 'conversation_created', 'conversation_updated', 'message_created',
    'conversation_opened', 'conversation_resolved'.

    Cada item de `condicoes` é um dict: {"attribute_key": str, "filter_operator": str, "values": list,
    "query_operator": "AND"|"OR"|""}. attribute_key comuns: "status", "assignee_id", "inbox_id",
    "browser_language", "country_code", "created_at". filter_operator comuns: "equal_to",
    "not_equal_to", "contains", "is_present", "is_not_present".

    Cada item de `acoes` é um dict: {"action_name": str, "action_params": list}. action_name comuns:
    "assign_agent" (action_params=[agent_id]), "assign_team" (action_params=[team_id]),
    "send_message" (action_params=["texto"]), "send_email_to_team" (action_params=[team_id]),
    "add_label" (action_params=["label"]), "change_priority" (action_params=["urgent"|"high"|"medium"|"low"]).

    Se não tiver certeza dos valores exatos (ex: ID de equipe/agente), pergunte ao Régis antes de chamar."""
    headers = {"api_access_token": settings.CHATWOOT_API_ACCESS_TOKEN, "X-Forwarded-Proto": "https"}
    url = f"{settings.CHATWOOT_API_URL}/api/v1/accounts/{settings.CHATWOOT_ACCOUNT_ID}/automation_rules"
    payload = {
        "automation_rule": {
            "name": nome,
            "description": f"Criada via IA em {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC — revisar antes de ativar.",
            "event_name": event_name,
            "conditions": condicoes,
            "actions": acoes,
            "active": False,
        }
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code not in (200, 201):
                return f"O Chatwoot recusou a criação da automação (status {r.status_code}): {r.text[:300]}"
            rule_id = r.json().get("id")
    except Exception as e:
        return f"Erro ao chamar a API do Chatwoot: {e}"
    return (f"Automação '{nome}' criada no Chatwoot (ID #{rule_id}), mas está DESATIVADA. "
            f"Quando quiser ligar ela de verdade, me manda 'ativa automação #{rule_id}'.")


@agent.tool
async def ativar_automacao(ctx: RunContext[FinanceiroDeps], automation_rule_id: int) -> str:
    """Ativa de verdade uma automação do Chatwoot que já foi criada (desativada) por
    aplicar_automacao. Use quando o Régis mandar algo como 'ativa automação #7' ou
    'liga aquela automação que criamos'."""
    headers = {"api_access_token": settings.CHATWOOT_API_ACCESS_TOKEN, "X-Forwarded-Proto": "https"}
    url = f"{settings.CHATWOOT_API_URL}/api/v1/accounts/{settings.CHATWOOT_ACCOUNT_ID}/automation_rules/{automation_rule_id}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.patch(url, json={"automation_rule": {"active": True}}, headers=headers)
            if r.status_code not in (200, 201):
                return f"Não consegui ativar a automação #{automation_rule_id} (status {r.status_code}): {r.text[:300]}"
    except Exception as e:
        return f"Erro ao chamar a API do Chatwoot: {e}"
    return f"Automação #{automation_rule_id} está ATIVA agora no Chatwoot."


@agent.tool
async def buscar_cliente(ctx: RunContext[FinanceiroDeps], termo: str) -> str:
    """Busca um cliente/passageiro no histórico da Captur (contatos, vendas e conversas) por
    nome, telefone, destino ou qualquer trecho. Use quando o Régis ou a Paula pedirem pra
    localizar alguém. Se não achar nada, diga que pode ser um cliente de antes desse sistema
    (sugira checar no Monde)."""
    return await buscar_cliente_raw(ctx.deps.db, termo)


async def perguntar(db: AsyncSession, pergunta: str) -> str:
    """Ponto de entrada usado pelo webhook do Chatwoot para processar a pergunta da Paula."""
    try:
        resultado = await agent.run(pergunta, deps=FinanceiroDeps(db=db))
        return resultado.output
    except Exception as e:
        print(f"[Financeiro IA] Erro ao processar pergunta: {e}")
        return "Desculpa, tive um erro ao consultar os dados financeiros agora. Tenta de novo em instantes."
