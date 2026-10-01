import os
from pathlib import Path
from typing import Optional
from pydantic import BaseModel
from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from src.services.contract_engine import (
    get_contract,
    finalize_signed_contract,
    register_contract,
    convert_docx_to_pdf
)
from src.services.evolution import EvolutionService

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

@router.get("/static/captur_logo.png")
async def get_captur_logo():
    logo_file = STATIC_DIR / "captur_logo.png"
    if logo_file.exists():
        return FileResponse(str(logo_file), media_type="image/png")
    raise HTTPException(status_code=404, detail="Logo not found")

@router.get("/static/captur_brand.jpeg")
async def get_captur_brand():
    brand_file = STATIC_DIR / "captur_brand.jpeg"
    if brand_file.exists():
        return FileResponse(str(brand_file), media_type="image/jpeg")
    raise HTTPException(status_code=404, detail="Brand image not found")

class SignContractRequest(BaseModel):
    nome: str
    cpf: str
    rg: Optional[str] = ""
    email: Optional[str] = ""
    telefone: Optional[str] = ""
    method: str = "opcao1_touch_selfie"
    signature_base64: Optional[str] = None
    photo_base64: Optional[str] = None
    autoriza_imagem: bool = True

class SendWhatsAppRequest(BaseModel):
    telefone: str
    mensagem_personalizada: Optional[str] = None

@router.get("/assinar/{token}", response_class=HTMLResponse)
async def pagina_assinar_contrato(request: Request, token: str):
    contract = get_contract(token)
    if not contract:
        raise HTTPException(status_code=404, detail="Contrato não encontrado ou link expirado.")
    
    template = templates.get_template("assinar_contrato.html")
    html_content = template.render({"contract": contract, "request": request})
    return HTMLResponse(content=html_content)

@router.get("/api/contratos/{token}/pdf")
async def visualizar_pdf_original(token: str):
    contract = get_contract(token)
    if not contract:
        raise HTTPException(status_code=404, detail="Contrato não encontrado.")
    
    pdf_path = contract["original_pdf_path"]
    if not os.path.exists(pdf_path):
        raise HTTPException(status_code=404, detail="Arquivo PDF não encontrado.")
        
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=f"{contract.get('token')}_original.pdf"
    )

@router.get("/api/contratos/{token}/download-assinado")
async def download_pdf_assinado(token: str):
    contract = get_contract(token)
    if not contract or not contract.get("signed_pdf_path"):
        raise HTTPException(status_code=404, detail="Contrato assinado ainda não disponível.")
    
    pdf_path = contract["signed_pdf_path"]
    if not os.path.exists(pdf_path):
        raise HTTPException(status_code=404, detail="Arquivo PDF assinado não encontrado.")
        
    client_name_slug = contract.get("client_name", "cliente").replace(" ", "_")
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=f"Contrato_Assinado_Captur_{client_name_slug}_{token}.pdf"
    )

@router.post("/api/contratos/{token}/assinar")
async def assinar_contrato_endpoint(request: Request, token: str, payload: SignContractRequest):
    contract = get_contract(token)
    if not contract:
        raise HTTPException(status_code=404, detail="Contrato não encontrado.")

    # Extract client IP and User Agent
    client_ip = request.client.host if request.client else "127.0.0.1"
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        client_ip = forwarded_for.split(",")[0].strip()
    user_agent = request.headers.get("user-agent", "Navegador Web")

    signer_data = {
        "nome": payload.nome,
        "cpf": payload.cpf,
        "rg": payload.rg,
        "email": payload.email,
        "telefone": payload.telefone,
        "autoriza_imagem": payload.autoriza_imagem
    }

    try:
        updated_contract = finalize_signed_contract(
            token=token,
            signature_base64=payload.signature_base64,
            photo_base64=payload.photo_base64,
            signer_data=signer_data,
            method=payload.method,
            ip_address=client_ip,
            user_agent=user_agent
        )

        # Notify via WhatsApp if phone available
        if payload.telefone:
            msg_whatsapp = (
                f"🎉 *Contrato Assinado com Sucesso!*\n\n"
                f"Olá {payload.nome}, seu contrato para *{updated_contract['contract_title']}* foi assinado eletronicamente e registrado com segurança.\n\n"
                f"🔒 *Código de Autenticidade:* `{token}`\n"
                f"📄 *Hash SHA-256:* `{updated_contract['signed_sha256'][:16]}...`\n\n"
                f"Agradecemos pela confiança na *Captur Agência de Viagens*! Qualquer dúvida nossa equipe está à disposição."
            )
            try:
                await EvolutionService.enviar_mensagem(payload.telefone, msg_whatsapp)
            except Exception as e:
                print(f"[Aviso WhatsApp] Não foi possível enviar confirmação: {e}")

        return {
            "status": "ok",
            "message": "Contrato assinado e certificado com sucesso!",
            "contract": updated_contract
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/contratos/{token}/enviar-whatsapp")
async def enviar_link_whatsapp(token: str, payload: SendWhatsAppRequest):
    contract = get_contract(token)
    if not contract:
        raise HTTPException(status_code=404, detail="Contrato não encontrado.")

    from config.settings import settings
    base = settings.PUBLIC_BASE_URL.rstrip('/')
    link_assinatura = f"{base}/assinar/{token}"
    mensagem = payload.mensagem_personalizada or (
        f"Olá! Aqui é da *Captur Agência de Viagens* ✈️\n\n"
        f"Preparamos o seu contrato de viagem (*{contract['contract_title']}*).\n\n"
        f"Para sua comodidade e segurança, você pode assinar diretamente pelo celular em poucos segundos no link seguro abaixo:\n"
        f"👉 {link_assinatura}\n\n"
        f"Qualquer dúvida, estamos à disposição!"
    )

    resp = await EvolutionService.enviar_mensagem(payload.telefone, mensagem)
    return {
        "status": "ok",
        "link": link_assinatura,
        "whatsapp_response": resp
    }
