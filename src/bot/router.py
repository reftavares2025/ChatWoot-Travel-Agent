import json
from sqlalchemy.ext.asyncio import AsyncSession
from config.settings import settings
from src.bot.state import get_estado, get_dados_lead, update_estado_lead
from src.services.evolution import EvolutionService
from src.services.chatwoot import ChatwootService
from src.services import flight_agent
from src.database.models import Conversa

TELEFONE_PAULA = "5515997495563"
TELEFONE_FLAVIA = "5515997607519"

EQUIPE = {
    "1": {
        "nome": "Ester (Vendas)",
        "chatwoot_id": 11,
        "inbox_id": 2,
        "titulo": "nossa especialista"
    },
    "2": {
        "nome": "Thamires (Vendas)",
        "chatwoot_id": 12,
        "inbox_id": 3,
        "titulo": "nossa especialista"
    },
    "3": {
        "nome": "Flávia (CEO - Relações)",
        "chatwoot_id": 10,
        "inbox_id": 5,
        "titulo": "nossa diretora"
    },
    "4": {
        "nome": "Paula (CEO - Financeiro)",
        "chatwoot_id": 2,
        "inbox_id": 6,
        "titulo": "nossa diretora"
    }
}

OPCAO_PASSAGEM = "5"


def _menu_text() -> str:
    linhas = [
        "Olá! Sou a *Olívia*, Agente de Vendas da Captur Agência de Viagens. ✈️🤖 Gostaria de falar em qual setor responsável?\n",
        "1 - Ester (vendas)",
        "2 - Thamires (vendas)",
        "3 - Flávia (CEO - Relações)",
        "4 - Paula (CEO - Financeiro)",
    ]
    if settings.VENDA_PASSAGEM_ATIVA:
        linhas.append(f"{OPCAO_PASSAGEM} - Comprar passagem aérea agora (self-service)")
    linhas.append("\nPor favor, digite o número da opção desejada.")
    return "\n".join(linhas)


MENU_TEXT = _menu_text()

PERFIS_VIAGEM = {
    "1": "Casal",
    "2": "Família",
    "3": "Solteiro(a)",
    "4": "Grupo"
}

GREETINGS_RESET = {
    "oi", "olá", "ola", "menu", "iniciar", "inicio", "início",
    "começar", "comecar", "bom dia", "boa tarde", "boa noite",
    "voltar", "reiniciar", "oi!", "olá!", "ola!", "ola bot", "oi bot", "opa", "oie"
}

async def processar_mensagem(db: AsyncSession, telefone: str, texto: str):
    texto_limpo = texto.strip()
    if not texto_limpo:
        return

    estado_obj = await get_estado(db, telefone)
    estado = estado_obj.estado
    dados = await get_dados_lead(estado_obj)

    # 1. Salvar histórico da mensagem
    conversa = Conversa(remetente=telefone, destinatario="bot", texto=texto_limpo, origem="whatsapp")
    db.add(conversa)
    await db.commit()

    # 🚨 Se o cliente já foi direcionado para atendimento humano, O BOT FICA 100% CALADO!
    # NUNCA responder saudações, NUNCA mandar menu, NUNCA reiniciar questionário!
    if estado == 'atendimento_humano':
        print(f"[Bot Silenciado] Cliente {telefone} já está em atendimento humano com atendente (Agente ID: {getattr(estado_obj, 'agente_id', 'definido')}). O bot não irá interferir.")
        return

    # Venda self-service de passagem aérea: delega 100% pro grafo dedicado (src/services/flight_agent.py)
    if estado == 'venda_passagem':
        await flight_agent.processar_mensagem_passagem(db, telefone, texto_limpo)
        return

    # Apenas se ainda NÃO foi atendido e enviar comando explícito de reinício
    if texto_limpo.lower() in ("#menu", "#reiniciar", "#cancelar"):
        await update_estado_lead(db, telefone, novo_estado='aguardando_opcao')
        await EvolutionService.enviar_mensagem(telefone, MENU_TEXT)
        return

    # 2. Máquina de Estados do Bot
    if estado == 'aguardando_opcao':
        if settings.VENDA_PASSAGEM_ATIVA and texto_limpo == OPCAO_PASSAGEM:
            await update_estado_lead(db, telefone, novo_estado='venda_passagem')
            await flight_agent.processar_mensagem_passagem(db, telefone, "")
        elif texto_limpo in EQUIPE:
            membro = EQUIPE[texto_limpo]
            await update_estado_lead(
                db, telefone,
                novo_estado='pergunta_nome',
                chave_dado='opcao_especialista',
                valor_dado=texto_limpo,
                agente_id=membro['chatwoot_id']
            )
            msg = (
                f"Perfeito! Você selecionou *{membro['nome']}*.\n\n"
                f"Para que possamos preparar o melhor atendimento e proposta para você, por favor, responda algumas perguntas rápidas.\n\n"
                "👉 Qual é o seu *Nome Completo*?"
            )
            await EvolutionService.enviar_mensagem(telefone, msg)
        else:
            await EvolutionService.enviar_mensagem(telefone, MENU_TEXT)

    elif estado == 'pergunta_nome':
        if len(texto_limpo) < 2:
            await EvolutionService.enviar_mensagem(telefone, "Por favor, digite seu *Nome Completo* para continuarmos:")
            return

        await update_estado_lead(db, telefone, novo_estado='pergunta_email', chave_dado='nome', valor_dado=texto_limpo)
        msg = f"Prazer, *{texto_limpo}*! 😊\n\n👉 Qual é o seu *E-mail* de contato?"
        await EvolutionService.enviar_mensagem(telefone, msg)

    elif estado == 'pergunta_email':
        if "@" not in texto_limpo or "." not in texto_limpo:
            await EvolutionService.enviar_mensagem(telefone, "Por favor, informe um *E-mail válido* (exemplo: seuemail@gmail.com):")
            return

        await update_estado_lead(db, telefone, novo_estado='pergunta_telefone', chave_dado='email', valor_dado=texto_limpo)
        msg = "Perfeito! 👍\n\n👉 Qual é o seu melhor *Telefone / WhatsApp* para contato com DDD?"
        await EvolutionService.enviar_mensagem(telefone, msg)

    elif estado == 'pergunta_telefone':
        digitos = "".join([c for c in texto_limpo if c.isdigit()])
        if len(digitos) < 8:
            await EvolutionService.enviar_mensagem(telefone, "Por favor, informe um número de *Telefone com DDD* válido:")
            return

        await update_estado_lead(db, telefone, novo_estado='pergunta_destino_data', chave_dado='telefone_contato', valor_dado=texto_limpo)
        msg = (
            "Show! ✈️\n\n"
            "👉 Qual é o *Destino de Interesse* e a *Data ou Período Previsto* da sua viagem?\n"
            "_(Exemplo: Cancún em Dezembro/2026, ou ainda sem data definida)_"
        )
        await EvolutionService.enviar_mensagem(telefone, msg)

    elif estado == 'pergunta_destino_data':
        if len(texto_limpo) < 2:
            await EvolutionService.enviar_mensagem(telefone, "Por favor, informe o destino e data/período previsto para a viagem:")
            return

        await update_estado_lead(db, telefone, novo_estado='pergunta_perfil', chave_dado='destino_data', valor_dado=texto_limpo)
        msg = (
            "Excelente! 🌴\n\n"
            "👉 Qual é o perfil dos viajantes? Digite o número da opção:\n\n"
            "1 - Casal\n"
            "2 - Família\n"
            "3 - Solteiro(a)\n"
            "4 - Grupo"
        )
        await EvolutionService.enviar_mensagem(telefone, msg)

    elif estado == 'pergunta_perfil':
        texto_lower = texto_limpo.lower()
        if texto_limpo in PERFIS_VIAGEM:
            perfil_nome = PERFIS_VIAGEM[texto_limpo]
            opcao_num = texto_limpo
        elif "casal" in texto_lower:
            perfil_nome = "Casal"
            opcao_num = "1"
        elif "fam" in texto_lower:
            perfil_nome = "Família"
            opcao_num = "2"
        elif "solt" in texto_lower:
            perfil_nome = "Solteiro(a)"
            opcao_num = "3"
        elif "grup" in texto_lower:
            perfil_nome = "Grupo"
            opcao_num = "4"
        else:
            perfil_nome = texto_limpo
            opcao_num = ""

        # Apenas para quem selecionar Família (2) ou Grupo (4) são feitas as perguntas de crianças
        tem_criancas_pergunta = opcao_num in ("2", "4") or any(k in perfil_nome.lower() for k in ["família", "familia", "grupo"])

        if tem_criancas_pergunta:
            await update_estado_lead(
                db, telefone,
                novo_estado='pergunta_filhos',
                chave_dado='perfil',
                valor_dado=perfil_nome
            )
            msg = (
                "👉 Haverá *crianças ou filhos* viajando juntos?\n\n"
                "Se sim, por favor informe a quantidade e a *idade de cada criança na data da viagem* "
                "(essencial para a cotação correta de passagens e hospedagens).\n\n"
                "_(Exemplo: 2 crianças, uma com 4 anos e outra com 11 anos na data da viagem. Se não houver, digite apenas *Não*)_"
            )
            await EvolutionService.enviar_mensagem(telefone, msg)
        else:
            # Para Casal, Solteiro, etc. pula diretamente a pergunta de crianças
            await update_estado_lead(
                db, telefone,
                novo_estado='pergunta_observacao',
                novos_dados={
                    'perfil': perfil_nome,
                    'filhos': 'Não'
                }
            )
            msg = (
                "Última perguntinha! 📝\n\n"
                "👉 Gostaria de acrescentar mais alguma *observação ou detalhe importante* sobre sua viagem?\n"
                "_(Ex: orçamento estimado, preferências de hospedagem, passeios, etc. Se não houver, digite apenas *Nenhuma*)_"
            )
            await EvolutionService.enviar_mensagem(telefone, msg)

    elif estado == 'pergunta_filhos':
        if not texto_limpo:
            await EvolutionService.enviar_mensagem(
                telefone,
                "Por favor, informe se haverá crianças e as idades na data da viagem (ou digite *Não*):"
            )
            return

        await update_estado_lead(db, telefone, novo_estado='pergunta_observacao', chave_dado='filhos', valor_dado=texto_limpo)
        msg = (
            "Última perguntinha! 📝\n\n"
            "👉 Gostaria de acrescentar mais alguma *observação ou detalhe importante* sobre sua viagem?\n"
            "_(Ex: orçamento estimado, preferências de hospedagem, passeios, etc. Se não houver, digite apenas *Nenhuma*)_"
        )
        await EvolutionService.enviar_mensagem(telefone, msg)

    elif estado == 'pergunta_observacao':
        if not texto_limpo:
            await EvolutionService.enviar_mensagem(telefone, "Por favor, informe seus detalhes adicionais ou digite *Nenhuma*:")
            return

        # Salvar observações e ir para tela de Confirmação
        await update_estado_lead(db, telefone, novo_estado='confirmacao_dados', chave_dado='observacoes', valor_dado=texto_limpo)
        
        # Recarregar dados completos
        estado_obj = await get_estado(db, telefone)
        dados = await get_dados_lead(estado_obj)

        opcao_esp = dados.get('opcao_especialista', '1')
        membro = EQUIPE.get(opcao_esp, EQUIPE["1"])
        nome = dados.get('nome', '')
        email = dados.get('email', '')
        tel = dados.get('telefone_contato', telefone)
        dest = dados.get('destino_data', '')
        perf = dados.get('perfil', '')
        filh = dados.get('filhos', 'Não')
        obs = texto_limpo

        resumo_msg = (
            "Por favor, confirme se seus dados estão corretos: 📋\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 *Nome:* {nome}\n"
            f"📧 *E-mail:* {email}\n"
            f"📱 *Telefone/WhatsApp:* {tel}\n"
            f"📍 *Destino e Data da Viagem:* {dest}\n"
            f"👥 *Perfil dos Viajantes:* {perf}\n"
        )
        if perf in ("Família", "Grupo") or filh != "Não":
            resumo_msg += f"👶 *Filhos/Crianças (Idade na data da viagem):* {filh}\n"
            
        resumo_msg += (
            f"📝 *Observações:* {obs}\n"
            f"⭐ *Responsável Selecionado(a):* {membro['nome']}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "👉 Digite o número da opção:\n"
            "*1* - CONFIRMAR e enviar\n"
            "*2* - REFAZER o questionário"
        )
        await EvolutionService.enviar_mensagem(telefone, resumo_msg)

    elif estado == 'confirmacao_dados':
        if texto_limpo in ("1", "confirmar", "sim", "ok", "correto", "certo"):
            opcao_esp = dados.get('opcao_especialista', '1')
            membro = EQUIPE.get(opcao_esp, EQUIPE["1"])
            nome_cliente = dados.get('nome', f'Cliente {telefone}')
            email_cliente = dados.get('email', '')
            telefone_contato = dados.get('telefone_contato', telefone)
            destino_data = dados.get('destino_data', 'Não informado')
            perfil = dados.get('perfil', 'Não informado')
            filhos = dados.get('filhos', 'Não')
            obs = dados.get('observacoes', 'Nenhuma')

            # Briefing Estruturado
            briefing = (
                f"📋 *[Triagem Captur] Novo Briefing de Cliente* 📋\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 *Nome:* {nome_cliente}\n"
                f"📧 *E-mail:* {email_cliente}\n"
                f"📱 *Telefone/WhatsApp:* {telefone_contato}\n"
                f"📍 *Destino e Data da Viagem:* {destino_data}\n"
                f"👥 *Perfil:* {perfil}\n"
                f"👶 *Filhos/Crianças (Idade na data da viagem):* {filhos}\n"
                f"📝 *Observações:* {obs}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⭐ *Responsável Solicitado(a):* {membro['nome']}"
            )

            custom_attrs = {
                "destino_e_data": destino_data,
                "perfil_viajantes": perfil,
                "filhos_criancas": filhos,
                "observacoes_viagem": obs,
                "especialista_solicitada": membro['nome']
            }

            # Registrar a conversa no Chatwoot para o responsável selecionado na respectiva Caixa de Entrada
            conv_id = await ChatwootService.criar_conversa_com_briefing(
                telefone=telefone,
                agente_id=membro['chatwoot_id'],
                briefing_texto=briefing,
                nome=nome_cliente,
                email=email_cliente,
                custom_attributes=custom_attrs,
                inbox_id=membro.get('inbox_id', 1)
            )

            await update_estado_lead(
                db, telefone,
                novo_estado='atendimento_humano',
                agente_id=membro['chatwoot_id'],
                chatwoot_conv_id=conv_id,
                atendido=False
            )

            # Notificar Paula e Flávia no WhatsApp com cópia do Briefing
            painel_linha = f"\n📊 *Painel Chatwoot:* {settings.CHATWOOT_PANEL_URL}" if settings.CHATWOOT_PANEL_URL else ""
            msg_aviso_diretoria = (
                f"🔔 *[Captur - Novo Briefing de Cliente]*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 *Cliente:* {nome_cliente}\n"
                f"📧 *E-mail:* {email_cliente}\n"
                f"📱 *WhatsApp:* https://wa.me/{telefone}\n"
                f"📍 *Destino e Data:* {destino_data}\n"
                f"👥 *Perfil:* {perfil}\n"
                f"👶 *Filhos/Crianças:* {filhos}\n"
                f"📝 *Observações:* {obs}\n"
                f"⭐ *Atendente Direcionada:* {membro['nome']}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💬 *Chamar cliente:* https://wa.me/{telefone}"
                f"{painel_linha}"
            )
            try:
                await EvolutionService.enviar_mensagem(TELEFONE_PAULA, msg_aviso_diretoria)
            except Exception as e:
                print(f"[Aviso Diretoria] Erro ao enviar para Paula: {e}")

            try:
                await EvolutionService.enviar_mensagem(TELEFONE_FLAVIA, msg_aviso_diretoria)
            except Exception as e:
                print(f"[Aviso Diretoria] Erro ao enviar para Flávia: {e}")

            msg_final = (
                f"Muito obrigado pela confirmação, *{nome_cliente}*! 🙏✨\n\n"
                f"Todos os seus dados foram encaminhados para nossa equipe com sucesso.\n\n"
                f"Na sequência, um dos nossos especialistas irá responder para dar continuidade ao seu atendimento! ✈️"
            )
            await EvolutionService.enviar_mensagem(telefone, msg_final)

        elif texto_limpo in ("2", "refazer", "não", "nao", "corrigir", "errado"):
            await update_estado_lead(db, telefone, novo_estado='pergunta_nome')
            msg_reinicio = (
                "Sem problemas! Vamos preencher novamente com as informações corretas. 😊\n\n"
                "👉 Qual é o seu *Nome Completo*?"
            )
            await EvolutionService.enviar_mensagem(telefone, msg_reinicio)
        else:
            await EvolutionService.enviar_mensagem(
                telefone,
                "Por favor, digite *1* para Confirmar ou *2* para Refazer:"
            )

    elif estado == 'atendimento_humano':
        pass
