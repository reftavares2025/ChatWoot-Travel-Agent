"""
Módulo financeiro da Captur — contas a pagar, contas a receber e fluxo de caixa.

Usa partida dobrada de verdade (via pyluca) para garantir que todo lançamento bate.
A tabela `lancamentos_contabeis` é a fonte única de verdade para saldo/fluxo de caixa;
`contas_a_receber` e `contas_a_pagar` são as visões operacionais (o que está pendente,
vencimento, etc.) — toda mudança nelas gera automaticamente o lançamento contábil espelho.

Plano de contas (simples, adequado a uma agência de viagens pequena):
    ATIVO (débito aumenta):     Caixa, Contas a Receber
    PASSIVO (crédito aumenta):  Contas a Pagar
    RECEITA (crédito aumenta):  Receita de Vendas
    DESPESA (débito aumenta):   Despesas Operacionais, Custo de Fornecedores
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pyluca.accountant import Accountant
from pyluca.journal import Journal
from pyluca.ledger import Ledger

from src.database.models import ContaPagar, ContaReceber, LancamentoContabil

CONTABIL_CONFIG = {
    "account_types": {
        "ATIVO": {"balance_type": "DEBIT"},
        "PASSIVO": {"balance_type": "CREDIT"},
        "RECEITA": {"balance_type": "CREDIT"},
        "DESPESA": {"balance_type": "DEBIT"},
    },
    "accounts": {
        "Caixa": {"type": "ATIVO"},
        "Contas a Receber": {"type": "ATIVO"},
        "Contas a Pagar": {"type": "PASSIVO"},
        "Receita de Vendas": {"type": "RECEITA"},
        "Despesas Operacionais": {"type": "DESPESA"},
        "Custo de Fornecedores": {"type": "DESPESA"},
    },
    "rules": {},
}


async def _lancar(db: AsyncSession, conta_debito: str, conta_credito: str, valor: Decimal,
                   narracao: str, origem_tipo: str, origem_id: Optional[int] = None,
                   data: Optional[datetime] = None):
    """Grava um lançamento contábil (débito/crédito) — nunca grava só metade."""
    lanc = LancamentoContabil(
        data=data or datetime.utcnow(),
        conta_debito=conta_debito,
        conta_credito=conta_credito,
        valor=valor,
        narracao=narracao,
        origem_tipo=origem_tipo,
        origem_id=origem_id,
    )
    db.add(lanc)
    await db.flush()
    return lanc


# ---------------------------------------------------------------------------
# CONTAS A RECEBER
# ---------------------------------------------------------------------------

async def registrar_conta_receber(
    db: AsyncSession,
    cliente_nome: str,
    valor_total: Decimal,
    cliente_telefone: str = None,
    contrato_token: str = None,
    destino: str = None,
    descricao: str = None,
    forma_pagamento: str = None,
    atendente_nome: str = None,
) -> ContaReceber:
    conta = ContaReceber(
        cliente_nome=cliente_nome,
        cliente_telefone=cliente_telefone,
        contrato_token=contrato_token,
        destino=destino,
        descricao=descricao,
        valor_total=valor_total,
        forma_pagamento=forma_pagamento,
        atendente_nome=atendente_nome,
    )
    db.add(conta)
    await db.flush()

    await _lancar(
        db,
        conta_debito="Contas a Receber",
        conta_credito="Receita de Vendas",
        valor=valor_total,
        narracao=f"Venda para {cliente_nome}" + (f" ({destino})" if destino else ""),
        origem_tipo="conta_a_receber",
        origem_id=conta.id,
    )
    await db.commit()
    await db.refresh(conta)
    return conta


async def registrar_recebimento(db: AsyncSession, conta_receber_id: int, valor_pago: Decimal) -> Optional[ContaReceber]:
    """Dá baixa (total ou parcial) numa conta a receber — é o dinheiro entrando no caixa."""
    result = await db.execute(select(ContaReceber).where(ContaReceber.id == conta_receber_id))
    conta = result.scalar_one_or_none()
    if not conta:
        return None

    conta.valor_recebido = Decimal(conta.valor_recebido or 0) + Decimal(valor_pago)
    conta.status = "quitado" if conta.valor_recebido >= conta.valor_total else "parcial"

    await _lancar(
        db,
        conta_debito="Caixa",
        conta_credito="Contas a Receber",
        valor=valor_pago,
        narracao=f"Recebimento de {conta.cliente_nome}",
        origem_tipo="conta_a_receber",
        origem_id=conta.id,
    )
    await db.commit()
    await db.refresh(conta)
    return conta


# ---------------------------------------------------------------------------
# CONTAS A PAGAR
# ---------------------------------------------------------------------------

async def registrar_conta_pagar(
    db: AsyncSession,
    fornecedor: str,
    valor: Decimal,
    categoria: str = "despesa_fixa",
    descricao: str = None,
    vencimento: datetime = None,
    contrato_token: str = None,
) -> ContaPagar:
    conta = ContaPagar(
        fornecedor=fornecedor,
        categoria=categoria,
        descricao=descricao,
        valor=valor,
        vencimento=vencimento,
        contrato_token=contrato_token,
    )
    db.add(conta)
    await db.flush()

    conta_despesa = "Custo de Fornecedores" if categoria == "fornecedor_viagem" else "Despesas Operacionais"
    await _lancar(
        db,
        conta_debito=conta_despesa,
        conta_credito="Contas a Pagar",
        valor=valor,
        narracao=f"Fatura de {fornecedor}" + (f" — {descricao}" if descricao else ""),
        origem_tipo="conta_a_pagar",
        origem_id=conta.id,
    )
    await db.commit()
    await db.refresh(conta)
    return conta


async def dar_baixa_pagamento(db: AsyncSession, conta_pagar_id: int) -> Optional[ContaPagar]:
    """Marca uma conta a pagar como paga — é o dinheiro saindo do caixa."""
    result = await db.execute(select(ContaPagar).where(ContaPagar.id == conta_pagar_id))
    conta = result.scalar_one_or_none()
    if not conta or conta.status == "pago":
        return conta

    conta.status = "pago"
    conta.pago_em = datetime.utcnow()

    await _lancar(
        db,
        conta_debito="Contas a Pagar",
        conta_credito="Caixa",
        valor=conta.valor,
        narracao=f"Pagamento a {conta.fornecedor}",
        origem_tipo="conta_a_pagar",
        origem_id=conta.id,
    )
    await db.commit()
    await db.refresh(conta)
    return conta


# ---------------------------------------------------------------------------
# CONSULTAS / RELATÓRIOS (via pyluca — nunca inventa número, sempre recalcula do zero)
# ---------------------------------------------------------------------------

async def _carregar_ledger(db: AsyncSession, data_inicio: datetime = None, data_fim: datetime = None) -> Ledger:
    query = select(LancamentoContabil)
    if data_inicio:
        query = query.where(LancamentoContabil.data >= data_inicio)
    if data_fim:
        query = query.where(LancamentoContabil.data <= data_fim)
    result = await db.execute(query)
    lancamentos = result.scalars().all()

    journal = Journal()
    accountant = Accountant(journal, CONTABIL_CONFIG, key="id")
    for i, l in enumerate(lancamentos):
        accountant.enter_journal(
            l.conta_debito, l.conta_credito, float(l.valor),
            l.data.strftime("%Y-%m-%d"), l.narracao or "",
        )
    return Ledger(journal, CONTABIL_CONFIG)


async def saldo_caixa(db: AsyncSession, data_inicio: datetime = None, data_fim: datetime = None) -> Decimal:
    ledger = await _carregar_ledger(db, data_inicio, data_fim)
    return Decimal(str(ledger.get_account_balance("Caixa")))


async def resumo_financeiro(db: AsyncSession, data_inicio: datetime = None, data_fim: datetime = None) -> dict:
    """Resumo completo e real (tudo recalculado na hora, nada fixo/estimado)."""
    ledger = await _carregar_ledger(db, data_inicio, data_fim)

    contas = ["Caixa", "Contas a Receber", "Contas a Pagar", "Receita de Vendas",
              "Despesas Operacionais", "Custo de Fornecedores"]
    saldos = {c: Decimal(str(ledger.get_account_balance(c))) for c in contas}

    q_receber = select(ContaReceber).where(ContaReceber.status != "quitado")
    q_pagar = select(ContaPagar).where(ContaPagar.status == "pendente")
    if data_inicio:
        q_receber = q_receber.where(ContaReceber.criado_em >= data_inicio)
        q_pagar = q_pagar.where(ContaPagar.criado_em >= data_inicio)
    if data_fim:
        q_receber = q_receber.where(ContaReceber.criado_em <= data_fim)
        q_pagar = q_pagar.where(ContaPagar.criado_em <= data_fim)

    a_receber = (await db.execute(q_receber)).scalars().all()
    a_pagar = (await db.execute(q_pagar)).scalars().all()

    return {
        "saldos_contabeis": saldos,
        "contas_a_receber_pendentes": [
            {"id": c.id, "cliente": c.cliente_nome, "valor_total": c.valor_total,
             "valor_recebido": c.valor_recebido, "status": c.status,
             "vencimento": c.vencimento.strftime("%d/%m/%Y") if c.vencimento else None}
            for c in a_receber
        ],
        "contas_a_pagar_pendentes": [
            {"id": c.id, "fornecedor": c.fornecedor, "categoria": c.categoria,
             "valor": c.valor, "vencimento": c.vencimento.strftime("%d/%m/%Y") if c.vencimento else None}
            for c in a_pagar
        ],
    }
