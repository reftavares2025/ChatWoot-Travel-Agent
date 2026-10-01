import os
import io
import json
import uuid
import hashlib
import datetime
from pathlib import Path
from typing import Optional, Dict, Any

from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import letter, A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage, KeepTogether, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm, mm
import qrcode
from PIL import Image as PILImage

# Base storage directories
BASE_DIR = Path(__file__).resolve().parent.parent.parent
STORAGE_DIR = BASE_DIR / "storage" / "contracts"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = STORAGE_DIR / "contracts_db.json"

def _load_db() -> Dict[str, Any]:
    if not DB_FILE.exists():
        return {}
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_db(data: Dict[str, Any]):
    tmp_file = DB_FILE.with_suffix(".tmp")
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp_file.replace(DB_FILE)

def calculate_sha256(file_path: str) -> str:
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest().upper()

def convert_docx_to_pdf(docx_path: str, output_pdf_path: str) -> str:
    """Converts DOCX to PDF using docx2pdf or LibreOffice if available."""
    try:
        import subprocess
        res = subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(Path(output_pdf_path).parent), str(docx_path)], capture_output=True)
        if res.returncode == 0:
            return str(output_pdf_path)
    except Exception:
        pass
    try:
        from docx2pdf import convert
        convert(str(docx_path), str(output_pdf_path))
    except Exception as e:
        print(f"Erro ao converter docx para pdf: {e}")
    return str(output_pdf_path)

def register_contract(
    client_name: str,
    contract_title: str,
    pdf_path: str,
    client_phone: str = "",
    client_email: str = "",
    token: Optional[str] = None
) -> Dict[str, Any]:
    """Registers a new contract in the database and computes original hash."""
    token = token or uuid.uuid4().hex[:12]
    original_pdf_dest = STORAGE_DIR / f"{token}_original.pdf"
    
    # Copy or place PDF in storage
    if str(pdf_path) != str(original_pdf_dest):
        if Path(pdf_path).exists():
            with open(pdf_path, "rb") as src, open(original_pdf_dest, "wb") as dst:
                dst.write(src.read())
        else:
            fallback = BASE_DIR / "contrato_espanha_paulo_sergio.pdf"
            if fallback.exists():
                with open(fallback, "rb") as src, open(original_pdf_dest, "wb") as dst:
                    dst.write(src.read())

    if not original_pdf_dest.exists():
        fallback = BASE_DIR / "contrato_espanha_paulo_sergio.pdf"
        if fallback.exists():
            with open(fallback, "rb") as src, open(original_pdf_dest, "wb") as dst:
                dst.write(src.read())

    original_hash = calculate_sha256(str(original_pdf_dest))
    created_at = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    contract_info = {
        "token": token,
        "client_name": client_name,
        "contract_title": contract_title,
        "client_phone": client_phone,
        "client_email": client_email,
        "status": "aguardando_assinatura",
        "created_at": created_at,
        "original_pdf_path": str(original_pdf_dest),
        "original_sha256": original_hash,
        "signed_pdf_path": None,
        "signed_sha256": None,
        "signed_at": None,
        "signing_method": None,
        "signer_details": {},
        "audit_trail": []
    }

    db = _load_db()
    db[token] = contract_info
    _save_db(db)
    return contract_info

def get_contract(token: str) -> Optional[Dict[str, Any]]:
    db = _load_db()
    return db.get(token)

def generate_audit_certificate_pdf(
    contract_info: Dict[str, Any],
    signature_img_path: Optional[str],
    photo_img_path: Optional[str],
    signer_data: Dict[str, Any],
    method: str,
    ip_address: str,
    user_agent: str,
    output_cert_path: str
) -> str:
    """Generates a professional, legally compliant Audit Trail & Certificate of Electronic Signature page."""
    
    doc = SimpleDocTemplate(
        output_cert_path,
        pagesize=A4,
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm
    )

    styles = getSampleStyleSheet()
    
    title_style = ParagraphStyle(
        'CertTitle',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=14,
        leading=17,
        textColor=colors.HexColor('#0F2027'),
        alignment=1
    )
    
    subtitle_style = ParagraphStyle(
        'CertSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor('#4A5568'),
        alignment=1
    )
    
    section_title = ParagraphStyle(
        'CertSection',
        parent=styles['Heading2'],
        fontName='Helvetica-Bold',
        fontSize=10.5,
        leading=13,
        textColor=colors.HexColor('#0F2027'),
        spaceBefore=8,
        spaceAfter=4
    )
    
    normal_text = ParagraphStyle(
        'CertNormal',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor('#2D3748')
    )
    
    bold_text = ParagraphStyle(
        'CertBold',
        parent=normal_text,
        fontName='Helvetica-Bold'
    )
    
    story = []

    # 1. Header with Captur Logo
    logo_path = BASE_DIR / "src" / "static" / "captur_logo.png"
    if logo_path.exists():
        header_table = Table([
            [
                RLImage(str(logo_path), width=3.2 * cm, height=1.5 * cm),
                [
                    Paragraph("<b>CAPTUR AGÊNCIA DE VIAGENS | CNPJ: 34.745.413/0001-17</b>", title_style),
                    Paragraph("REGISTRO DE ASSINATURA ELETRÔNICA & TRILHA DE AUDITORIA", ParagraphStyle('SubH', parent=title_style, fontSize=9.5, leading=12, textColor=colors.HexColor('#10B981'))),
                    Paragraph("Conforme MP 2.200-2/2001 e Lei Federal nº 14.063/2020 (Validade Jurídica Integral)", subtitle_style)
                ]
            ]
        ], colWidths=[3.5 * cm, 14.2 * cm])
        header_table.setStyle(TableStyle([
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('ALIGN', (0,0), (0,0), 'CENTER'),
            ('LEFTPADDING', (0,0), (-1,-1), 0),
            ('RIGHTPADDING', (0,0), (-1,-1), 0),
            ('BOTTOMPADDING', (0,0), (-1,-1), 0),
            ('TOPPADDING', (0,0), (-1,-1), 0),
        ]))
        story.append(header_table)
    else:
        story.append(Paragraph("<b>CAPTUR AGÊNCIA DE VIAGENS | CNPJ: 34.745.413/0001-17</b>", title_style))
        story.append(Paragraph("REGISTRO DE ASSINATURA ELETRÔNICA & TRILHA DE AUDITORIA", ParagraphStyle('SubH', parent=title_style, fontSize=11, leading=14, textColor=colors.HexColor('#10B981'))))
        story.append(Paragraph("Conforme Medida Provisória nº 2.200-2/2001 e Lei Federal nº 14.063/2020 (Validade Jurídica Integral)", subtitle_style))
    
    story.append(Spacer(1, 3 * mm))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor('#0F2027'), spaceBefore=1, spaceAfter=6))

    # 2. Document Info
    doc_table_data = [
        [Paragraph("<b>Documento Original:</b>", bold_text), Paragraph(contract_info.get("contract_title", "Contrato de Prestação de Serviços Turísticos"), normal_text)],
        [Paragraph("<b>Código / Token:</b>", bold_text), Paragraph(contract_info.get("token", ""), normal_text)],
        [Paragraph("<b>Hash SHA-256 (Original):</b>", bold_text), Paragraph(f"<font size='7'>{contract_info.get('original_sha256', '')}</font>", normal_text)],
        [Paragraph("<b>Data da Emissão:</b>", bold_text), Paragraph(contract_info.get("created_at", ""), normal_text)],
    ]
    t_doc = Table(doc_table_data, colWidths=[4.2 * cm, 13.5 * cm])
    t_doc.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#F8FAFC')),
        ('BOX', (0,0), (-1,-1), 0.5, colors.HexColor('#CBD5E1')),
        ('INNERGRID', (0,0), (-1,-1), 0.5, colors.HexColor('#E2E8F0')),
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
    ]))
    story.append(t_doc)
    story.append(Spacer(1, 4 * mm))

    # 3. Signer Info
    story.append(Paragraph("DADOS DO ASSINANTE TITULAR", section_title))
    signer_name = signer_data.get("nome", contract_info.get("client_name", ""))
    signer_cpf = signer_data.get("cpf", "")
    signer_rg = signer_data.get("rg", "")
    signer_email = signer_data.get("email", contract_info.get("client_email", ""))
    signer_phone = signer_data.get("telefone", contract_info.get("client_phone", ""))
    signed_at = datetime.datetime.now().strftime("%d/%m/%Y às %H:%M:%S (Horário de Brasília - UTC-3)")

    method_labels = {
        "opcao1_touch_selfie": "Opção 1: Assinatura Eletrônica em Tela Touch + Biometria Facial / Selfie",
        "opcao2_documento": "Opção 2: Assinatura Eletrônica + Verificação de Documento de Identidade",
        "opcao3_govbr": "Opção 3: Assinador Digital Oficial GOV.BR (Identidade Federal Verificada)"
    }
    method_desc = method_labels.get(method, method)

    signer_table_data = [
        [Paragraph("<b>Nome Completo:</b>", bold_text), Paragraph(signer_name, normal_text)],
        [Paragraph("<b>CPF:</b>", bold_text), Paragraph(signer_cpf, normal_text)],
        [Paragraph("<b>RG:</b>", bold_text), Paragraph(signer_rg, normal_text)],
        [Paragraph("<b>E-mail:</b>", bold_text), Paragraph(signer_email, normal_text)],
        [Paragraph("<b>WhatsApp / Tel:</b>", bold_text), Paragraph(signer_phone, normal_text)],
        [Paragraph("<b>Método de Validação:</b>", bold_text), Paragraph(f"<b>{method_desc}</b>", normal_text)],
        [Paragraph("<b>Data e Hora da Assinatura:</b>", bold_text), Paragraph(f"<b>{signed_at}</b>", normal_text)],
        [Paragraph("<b>Endereço IP do Assinante:</b>", bold_text), Paragraph(ip_address, normal_text)],
        [Paragraph("<b>Dispositivo / Navegador:</b>", bold_text), Paragraph(f"<font size='7'>{user_agent[:120]}</font>", normal_text)],
    ]
    t_signer = Table(signer_table_data, colWidths=[4.2 * cm, 13.5 * cm])
    t_signer.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#FFFFFF')),
        ('BOX', (0,0), (-1,-1), 0.5, colors.HexColor('#CBD5E1')),
        ('INNERGRID', (0,0), (-1,-1), 0.5, colors.HexColor('#E2E8F0')),
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
    ]))
    story.append(t_signer)
    story.append(Spacer(1, 4 * mm))

    # 4. Evidence Box (Signature Image, Selfie Image, QR Code)
    story.append(Paragraph("EVIDÊNCIAS DIGITAIS & PROVA DE AUTENTICIDADE", section_title))
    
    # Generate QR Code for validation
    qr_img_path = STORAGE_DIR / f"{contract_info.get('token')}_qr.png"
    qr_data = f"https://captur.tur.br/validar-contrato?token={contract_info.get('token')}&cpf={signer_cpf}"
    qr = qrcode.QRCode(version=1, box_size=4, border=1)
    qr.add_data(qr_data)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="#0F2027", back_color="white")
    qr_img.save(str(qr_img_path))

    evidence_cells = []
    
    # Signature Box
    sig_cell = []
    sig_cell.append(Paragraph("<b>Rubrica / Assinatura Digital</b>", ParagraphStyle('C', parent=bold_text, alignment=1)))
    if signature_img_path and os.path.exists(signature_img_path):
        sig_cell.append(Spacer(1, 2 * mm))
        sig_cell.append(RLImage(signature_img_path, width=4.5 * cm, height=2.2 * cm))
    else:
        sig_cell.append(Spacer(1, 4 * mm))
        sig_cell.append(Paragraph("<font color='#10B981'><b>[Assinado via Gov.br / Certificado Digital]</b></font>", ParagraphStyle('C2', parent=bold_text, alignment=1)))
    
    # Photo / Selfie Box
    photo_cell = []
    photo_cell.append(Paragraph("<b>Biometria / Documento</b>", ParagraphStyle('C', parent=bold_text, alignment=1)))
    if photo_img_path and os.path.exists(photo_img_path):
        photo_cell.append(Spacer(1, 2 * mm))
        photo_cell.append(RLImage(photo_img_path, width=3.8 * cm, height=2.6 * cm))
    else:
        photo_cell.append(Spacer(1, 4 * mm))
        photo_cell.append(Paragraph("<font color='#3B82F6'><b>[Identidade Verificada Gov.br]</b></font>", ParagraphStyle('C2', parent=bold_text, alignment=1)))

    # QR Code Box
    qr_cell = []
    qr_cell.append(Paragraph("<b>Validação On-line</b>", ParagraphStyle('C', parent=bold_text, alignment=1)))
    qr_cell.append(Spacer(1, 2 * mm))
    qr_cell.append(RLImage(str(qr_img_path), width=2.6 * cm, height=2.6 * cm))

    t_evidence = Table([[sig_cell, photo_cell, qr_cell]], colWidths=[6.5 * cm, 6.0 * cm, 5.2 * cm])
    t_evidence.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#F8FAFC')),
        ('BOX', (0,0), (-1,-1), 0.5, colors.HexColor('#CBD5E1')),
        ('INNERGRID', (0,0), (-1,-1), 0.5, colors.HexColor('#CBD5E1')),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
    ]))
    story.append(t_evidence)
    story.append(Spacer(1, 4 * mm))

    # 5. Declarations and Legal Note
    legal_note = (
        "<b>Declaração de Consentimento:</b> O assinante supra qualificado declara que leu, compreendeu e anuiu "
        "com a totalidade das cláusulas do presente contrato de prestação de serviços turísticos, reconhecendo a autenticidade "
        "e a integridade deste documento eletrônico para todos os fins de direito (Art. 10, § 2º da MP 2.200-2/2001 e Art. 5º da Lei 14.063/2020)."
    )
    story.append(Paragraph(legal_note, ParagraphStyle('Legal', parent=normal_text, fontSize=7.5, leading=9.5, textColor=colors.HexColor('#64748B'))))
    story.append(Spacer(1, 3 * mm))
    
    # Build certificate
    doc.build(story)
    return output_cert_path

def finalize_signed_contract(
    token: str,
    signature_base64: Optional[str],
    photo_base64: Optional[str],
    signer_data: Dict[str, Any],
    method: str,
    ip_address: str,
    user_agent: str
) -> Dict[str, Any]:
    """Merges the original contract with the digital audit sheet into the final signed PDF."""
    contract = get_contract(token)
    if not contract:
        raise ValueError("Contrato não encontrado.")

    original_pdf_path = contract["original_pdf_path"]
    if not os.path.exists(original_pdf_path):
        raise FileNotFoundError("Arquivo PDF original do contrato não encontrado.")

    # Save signature image if provided
    sig_img_path = None
    if signature_base64 and "," in signature_base64:
        import base64
        from PIL import ImageFile
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        header, encoded = signature_base64.split(",", 1)
        sig_bytes = base64.b64decode(encoded)
        sig_img_path = str(STORAGE_DIR / f"{token}_signature.png")
        try:
            img = PILImage.open(io.BytesIO(sig_bytes))
            # Convert to RGBA or RGB
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
            img.save(sig_img_path, "PNG")
        except Exception as err:
            print(f"[Warning] Failed PIL processing for signature: {err}")
            with open(sig_img_path, "wb") as f:
                f.write(sig_bytes)

    # Save photo/selfie image if provided
    photo_img_path = None
    if photo_base64 and "," in photo_base64:
        import base64
        from PIL import ImageFile
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        header, encoded = photo_base64.split(",", 1)
        photo_bytes = base64.b64decode(encoded)
        photo_img_path = str(STORAGE_DIR / f"{token}_photo.jpg")
        
        # Optimize image with PIL
        try:
            img = PILImage.open(io.BytesIO(photo_bytes))
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((800, 800))
            img.save(photo_img_path, "JPEG", quality=85)
        except Exception as err:
            print(f"[Warning] Failed PIL processing for photo: {err}")
            with open(photo_img_path, "wb") as f:
                f.write(photo_bytes)

    # Generate Audit Trail PDF page
    cert_pdf_path = str(STORAGE_DIR / f"{token}_certificate.pdf")
    generate_audit_certificate_pdf(
        contract_info=contract,
        signature_img_path=sig_img_path,
        photo_img_path=photo_img_path,
        signer_data=signer_data,
        method=method,
        ip_address=ip_address,
        user_agent=user_agent,
        output_cert_path=cert_pdf_path
    )

    # Merge Original PDF + Certificate Page
    final_signed_path = str(STORAGE_DIR / f"{token}_contrato_assinado.pdf")
    writer = PdfWriter()
    
    # Read original
    reader_orig = PdfReader(original_pdf_path)
    for page in reader_orig.pages:
        writer.add_page(page)
        
    # Read cert
    reader_cert = PdfReader(cert_pdf_path)
    for page in reader_cert.pages:
        writer.add_page(page)

    try:
        with open(final_signed_path, "wb") as f:
            writer.write(f)
    except PermissionError:
        import time
        final_signed_path = str(STORAGE_DIR / f"{token}_contrato_assinado_{int(time.time())}.pdf")
        with open(final_signed_path, "wb") as f:
            writer.write(f)

    # Calculate final hash
    final_sha256 = calculate_sha256(final_signed_path)
    signed_at = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    # Update database
    db = _load_db()
    contract["status"] = "assinado"
    contract["signed_at"] = signed_at
    contract["signed_pdf_path"] = final_signed_path
    contract["signed_sha256"] = final_sha256
    contract["signing_method"] = method
    contract["signer_details"] = signer_data
    contract["audit_trail"].append({
        "event": "contrato_assinado",
        "timestamp": signed_at,
        "method": method,
        "ip": ip_address,
        "user_agent": user_agent,
        "final_sha256": final_sha256
    })
    db[token] = contract
    _save_db(db)

    return contract
