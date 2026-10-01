from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, Numeric
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class Conversa(Base):
    __tablename__ = 'conversas'

    id = Column(Integer, primary_key=True, index=True)
    remetente = Column(String(255), index=True)
    destinatario = Column(String(255), index=True)
    texto = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow)
    agente_responsavel = Column(String(255), nullable=True)
    origem = Column(String(50)) # 'whatsapp', 'chatwoot', etc.

class EstadoAtendimento(Base):
    __tablename__ = 'estados'

    id = Column(Integer, primary_key=True, index=True)
    telefone = Column(String(50), unique=True, index=True)
    estado = Column(String(50), default='aguardando_opcao')
    agente_id = Column(Integer, nullable=True)
    chatwoot_conversation_id = Column(Integer, nullable=True)
    chatwoot_contact_id = Column(Integer, nullable=True)
    dados_lead = Column(Text, nullable=True) # JSON com respostas do formulário
    atendido = Column(Boolean, default=False)
    transferido_em = Column(DateTime, default=datetime.utcnow)
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    hesitacao_detectada_em = Column(DateTime, nullable=True)
    followup_enviado = Column(Boolean, default=False)

class MensagemProcessada(Base):
    """Rastreia mensagens do WhatsApp já processadas (via webhook ou reconciliador),
    para evitar duplicidade e permitir detectar mensagens perdidas por falha de sincronização."""
    __tablename__ = 'mensagens_processadas'

    id = Column(Integer, primary_key=True, index=True)
    whatsapp_msg_id = Column(String(255), unique=True, index=True)
    origem = Column(String(20), default='webhook')  # 'webhook' ou 'reconciliador'
    criado_em = Column(DateTime, default=datetime.utcnow)

class ClienteContato(Base):
    __tablename__ = 'clientes_contatos'

    id = Column(Integer, primary_key=True, index=True)
    telefone = Column(String(50), unique=True, index=True)
    nome = Column(String(255), index=True)
    remote_jid = Column(String(255), nullable=True)
    email = Column(String(255), nullable=True)
    tipo = Column(String(50), default='contato') # contato / grupo
    foto_perfil = Column(Text, nullable=True)
    criado_em = Column(DateTime, default=datetime.utcnow)
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class ContaReceber(Base):
    """Valores a receber de clientes — gerado automaticamente ao registrar um contrato com valor informado."""
    __tablename__ = 'contas_a_receber'

    id = Column(Integer, primary_key=True, index=True)
    cliente_nome = Column(String(255), index=True)
    cliente_telefone = Column(String(50), nullable=True, index=True)
    contrato_token = Column(String(50), nullable=True, index=True)
    destino = Column(String(255), nullable=True)
    descricao = Column(Text, nullable=True)
    valor_total = Column(Numeric(12, 2), nullable=False)
    valor_recebido = Column(Numeric(12, 2), default=0)
    forma_pagamento = Column(String(50), nullable=True)  # pix, cartao, boleto
    status = Column(String(20), default='pendente', index=True)  # pendente, parcial, quitado
    vencimento = Column(DateTime, nullable=True)
    atendente_nome = Column(String(100), nullable=True)
    criado_em = Column(DateTime, default=datetime.utcnow)
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ContaPagar(Base):
    """Despesas fixas e faturas de fornecedores (consolidadoras, hotéis, etc.)."""
    __tablename__ = 'contas_a_pagar'

    id = Column(Integer, primary_key=True, index=True)
    fornecedor = Column(String(255), index=True)
    categoria = Column(String(50), default='despesa_fixa')  # despesa_fixa, fornecedor_viagem
    descricao = Column(Text, nullable=True)
    valor = Column(Numeric(12, 2), nullable=False)
    vencimento = Column(DateTime, nullable=True)
    status = Column(String(20), default='pendente', index=True)  # pendente, pago
    pago_em = Column(DateTime, nullable=True)
    contrato_token = Column(String(50), nullable=True, index=True)
    criado_em = Column(DateTime, default=datetime.utcnow)
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LancamentoContabil(Base):
    """Livro-razão de partida dobrada (débito/crédito), espelhando cada evento financeiro.
    Fonte de verdade única para o pyluca calcular saldos e fluxo de caixa."""
    __tablename__ = 'lancamentos_contabeis'

    id = Column(Integer, primary_key=True, index=True)
    data = Column(DateTime, default=datetime.utcnow, index=True)
    conta_debito = Column(String(100), index=True)
    conta_credito = Column(String(100), index=True)
    valor = Column(Numeric(12, 2), nullable=False)
    narracao = Column(Text, nullable=True)
    origem_tipo = Column(String(30), index=True)  # conta_a_receber, conta_a_pagar, manual
    origem_id = Column(Integer, nullable=True)
    criado_em = Column(DateTime, default=datetime.utcnow)


class ConhecimentoAprendido(Base):
    """Base de Conhecimento Dinâmica com ciclo de autoaprendizado e aprovação humana."""
    __tablename__ = 'conhecimentos_aprendidos'

    id = Column(Integer, primary_key=True, index=True)
    topico = Column(String(255), index=True) # Ex: 'Seguro Viagem Europa', 'Resort X', 'Cancelamento'
    pergunta_gatilho = Column(Text) # Frases ou padrões que ativam essa resposta
    resposta_sugerida = Column(Text) # Conteúdo da resposta ensinada/aprendida
    status = Column(String(50), default='pendente', index=True) # 'pendente', 'aprovado', 'rejeitado'
    origem = Column(String(100), default='analise_ia') # 'chatwoot_vendedora', 'analise_ia', 'manual'
    atendente_nome = Column(String(100), nullable=True)
    usos_count = Column(Integer, default=0)
    criado_em = Column(DateTime, default=datetime.utcnow)
    aprovado_em = Column(DateTime, nullable=True)
