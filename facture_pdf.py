#!/usr/bin/env python3
"""Facture SecureeTech, reprise du modèle ELITE-2026-XXXX."""
import io
import os
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.platypus import (Image, Paragraph, SimpleDocTemplate, Spacer,
                                Table, TableStyle)

ORANGE = colors.HexColor('#f07800')
ORANGE_FONCE = colors.HexColor('#c05a00')
VIOLET = colors.HexColor('#2a0c52')
VIOLET_NUIT = colors.HexColor('#0e011d')
ENCRE = colors.HexColor('#201620')
GRIS = colors.HexColor('#8a8590')
CREME = colors.HexColor('#fbf1e7')

RACINE = os.path.dirname(os.path.abspath(__file__))
LOGO = os.path.join(RACINE, 'static', 'logo-secureetech.png')

EMETTEUR = {
    'raison_sociale': os.getenv('SOCIETE_NOM', 'ELITE-ASSISTANCE.SL'),
    'marque': os.getenv('SOCIETE_MARQUE', 'Secureetech'),
    'adresse_1': os.getenv('SOCIETE_ADRESSE_1', 'La Reserva de Marbella, Manzana 6, Bloque 3'),
    'adresse_2': os.getenv('SOCIETE_ADRESSE_2', '29604 Marbella, Malaga, Espagne'),
    'identifiant': os.getenv('SOCIETE_IDENTIFIANT', 'CIF : B01773654'),
    'email': os.getenv('SOCIETE_EMAIL', 'contact@secureetech.com'),
    'telephone': os.getenv('SOCIETE_TELEPHONE', '09 80 80 17 59'),
    'site': os.getenv('SOCIETE_SITE', 'secureetech.com'),
}

# Prestations détaillées sous la désignation, comme sur le modèle.
DETAILS_FORMULE = {
    'Basic': ("Service client 10h00–16h00 (lun-ven) • Intervention sous 48h • "
              "3h d'assistance mensuelle • Maintenance de base • "
              "Accompagnement adapté aux seniors • Protection anti-fraude"),
    'Sérénité': ("Service client 10h00–18h00 (lun-ven) • Intervention sous 24h • "
                 "5h d'assistance mensuelle • Contrôle des mises à jour et stabilité "
                 "Windows • Installation et retrait d'applications • Protection "
                 "anti-fraude • Assistance imprimante • Abonnement transférable"),
    'Privilège': ("Service client 10h00–18h00 (lun-ven) • Intervention sous 1h • "
                  "Aide et assistance illimitées • Mises à jour bimensuelles • "
                  "Protection anti-fraude • Récupération de fichiers supprimés • "
                  "Réinitialisation mots de passe • VPN • Office 365 • "
                  "Protection bancaire et Wi-Fi • Accompagnement personnel VIP"),
    'Excellence': ("Tous les avantages Privilège pour toute la famille • "
                   "Jusqu'à 5 appareils (PC, Mac, téléphones, tablettes) • "
                   "Tarification avantageuse par appareil"),
    'Infinity': ("Protection numérique à vie • 5 appareils inclus • VPN Pro • "
                 "Office 365 • Logiciel d'optimisation version Pro • Anti-arnaques • "
                 "Transfert et migration au changement d'appareil • VIP illimité"),
}


def _fr(montant):
    return f"{float(montant):,.2f} €".replace(',', ' ').replace('.', ',').replace(' ', ' ')


def _date(texte):
    try:
        return datetime.strptime(texte, '%Y-%m-%d').strftime('%Y-%m-%d')
    except (ValueError, TypeError):
        return texte or ''


def _designation(facture):
    """« Serveur Cloud BASIC 36 » à partir de la formule et de la durée."""
    formule = (facture.get('formule') or '').strip()
    duree = facture.get('duree') or 0
    if not formule:
        return facture.get('description') or 'Prestation Secureetech', ''

    intitule = f"Serveur Cloud {formule.upper()}"
    if duree:
        intitule += f" {duree}"
    return intitule, DETAILS_FORMULE.get(formule, '')


def construire(facture, taux_tva=0.21):
    tampon = io.BytesIO()
    doc = SimpleDocTemplate(
        tampon, pagesize=A4,
        leftMargin=1.6 * cm, rightMargin=1.6 * cm,
        topMargin=0, bottomMargin=1.4 * cm,
        title=f"Facture {facture.get('numero_facture', '')}")

    styles = getSampleStyleSheet()
    normal = ParagraphStyle('N', parent=styles['Normal'], fontSize=9, leading=13, textColor=ENCRE)
    gris = ParagraphStyle('G', parent=normal, textColor=GRIS)
    petit = ParagraphStyle('P', parent=normal, fontSize=7.5, textColor=GRIS, leading=11)
    etiquette = ParagraphStyle('E', parent=normal, fontSize=7.5, textColor=ORANGE_FONCE,
                               leading=11, spaceAfter=2)
    droite = ParagraphStyle('D', parent=normal, alignment=TA_RIGHT)
    pied = ParagraphStyle('F', parent=petit, alignment=TA_CENTER)

    ht = float(facture.get('montant_ht') or 0)
    tva = float(facture.get('tva') or 0)
    ttc = float(facture.get('montant') or 0)
    if ht <= 0 and ttc > 0:
        ht = round(ttc / (1 + taux_tva), 2)
        tva = round(ttc - ht, 2)

    largeur = doc.width
    elements = []

    # ---------- Bandeau ----------
    contenu_bandeau = []
    if os.path.isfile(LOGO):
        contenu_bandeau.append(Image(LOGO, width=3.2 * cm, height=1.99 * cm))
    contenu_bandeau.append(Paragraph(
        "<font size=21 color='#ffffff'><b>S E C U R E E T E C H</b></font>",
        ParagraphStyle('M', parent=normal, alignment=TA_CENTER, leading=26)))

    bandeau = Table([[c] for c in contenu_bandeau], colWidths=[largeur + 3.2 * cm])
    bandeau.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), VIOLET_NUIT),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, 0), 14),
        ('BOTTOMPADDING', (0, -1), (-1, -1), 12),
        ('LINEBELOW', (0, -1), (-1, -1), 3, ORANGE),
    ]))
    elements += [bandeau, Spacer(1, 0.9 * cm)]

    # ---------- Titre et références ----------
    statut = (facture.get('statut_paiement') or 'À RÉGLER').upper()
    badge = Table([[Paragraph(f"<font color='#ffffff' size=8><b>{statut}</b></font>", droite)]],
                  colWidths=[3.2 * cm])
    badge.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), ORANGE_FONCE),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))

    references = Table([
        [Paragraph(f"<font size=11><b>N° {facture.get('numero_facture','')}</b></font>", droite)],
        [Paragraph(f"<font size=8 color='#8a8590'>Date : {_date(facture.get('date_facture'))}</font>", droite)],
        [Spacer(1, 4)],
        [badge],
    ], colWidths=[6.2 * cm])
    references.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))

    titre = Table([[
        Paragraph("<font size=26 color='#c05a00'><b>FACTURE</b></font>", normal),
        references,
    ]], colWidths=[largeur - 6.2 * cm, 6.2 * cm])
    titre.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'),
                               ('LEFTPADDING', (0, 0), (-1, -1), 0),
                               ('RIGHTPADDING', (0, 0), (-1, -1), 0)]))
    elements += [titre, Spacer(1, 1 * cm)]

    # ---------- Émetteur / client ----------
    client = facture.get('client_nom') or ''
    adresse_client = facture.get('client_adresse') or ''
    email_client = facture.get('email') or ''

    bloc_client = f"<b><font size=11>{client}</font></b>"
    if adresse_client:
        bloc_client += f"<br/><font color='#8a8590'>{adresse_client}</font>"
    if email_client:
        bloc_client += f"<br/><font color='#8a8590'>{email_client}</font>"

    parties = Table([[
        Paragraph("ÉMIS PAR", etiquette),
        Paragraph("FACTURÉ À", etiquette),
    ], [
        Paragraph(f"<b><font size=11>{EMETTEUR['raison_sociale']}</font></b><br/>"
                  f"<font color='#8a8590'>{EMETTEUR['marque']}<br/>"
                  f"{EMETTEUR['adresse_1']}<br/>{EMETTEUR['adresse_2']}<br/>"
                  f"{EMETTEUR['identifiant']}<br/>{EMETTEUR['email']}</font>", normal),
        Paragraph(bloc_client, normal),
    ]], colWidths=[largeur / 2, largeur / 2])
    parties.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'),
                                 ('LEFTPADDING', (0, 0), (-1, -1), 0),
                                 ('RIGHTPADDING', (0, 0), (-1, -1), 6)]))
    elements += [parties, Spacer(1, 1 * cm)]

    # ---------- Prestation ----------
    intitule, details = _designation(facture)
    cellule = f"<b><font size=10>{intitule}</font></b>"
    if details:
        cellule += f"<br/><font size=8 color='#8a8590'>{details}</font>"

    prestation = Table([
        [Paragraph("<font color='#ffffff' size=7.5><b>QTÉ</b></font>", normal),
         Paragraph("<font color='#ffffff' size=7.5><b>DÉSIGNATION</b></font>", normal),
         Paragraph("<font color='#ffffff' size=7.5><b>PRIX HT</b></font>", droite)],
        [Paragraph('1', normal), Paragraph(cellule, normal), Paragraph(_fr(ht), droite)],
    ], colWidths=[1.3 * cm, largeur - 5.1 * cm, 3.8 * cm])
    prestation.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), ENCRE),
        ('LINEABOVE', (0, 0), (-1, 0), 2.5, ORANGE),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('VALIGN', (0, 0), (-1, 0), 'MIDDLE'),
        ('BOX', (0, 1), (-1, 1), 0.5, colors.HexColor('#e6e0ea')),
        ('LINEBELOW', (0, 1), (-1, 1), 0.5, colors.HexColor('#e6e0ea')),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
    ]))
    elements += [prestation, Spacer(1, 0.9 * cm)]

    # ---------- Totaux ----------
    ligne_ttc = Table([[
        Paragraph("<font color='#ffffff' size=12><b>TOTAL TTC</b></font>", normal),
        Paragraph(f"<font color='#f07800' size=14><b>{_fr(ttc)}</b></font>", droite),
    ]], colWidths=[4.3 * cm, 4.3 * cm])
    ligne_ttc.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), ENCRE),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 11),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 11),
        ('LEFTPADDING', (0, 0), (-1, -1), 12),
        ('RIGHTPADDING', (0, 0), (-1, -1), 12),
    ]))

    totaux = Table([
        [Paragraph('Total Hors Taxes', normal), Paragraph(_fr(ht), droite)],
        [Paragraph(f'T.V.A / I.V.A ({int(taux_tva * 100)}%)', normal), Paragraph(_fr(tva), droite)],
        [ligne_ttc, ''],
    ], colWidths=[4.3 * cm, 4.3 * cm])
    totaux.setStyle(TableStyle([
        ('SPAN', (0, 2), (1, 2)),
        ('TOPPADDING', (0, 0), (-1, 1), 5), ('BOTTOMPADDING', (0, 0), (-1, 1), 5),
        ('LEFTPADDING', (0, 2), (-1, 2), 0), ('RIGHTPADDING', (0, 2), (-1, 2), 0),
        ('TOPPADDING', (0, 2), (-1, 2), 8),
    ]))

    enveloppe = Table([['', totaux]], colWidths=[largeur - 8.6 * cm, 8.6 * cm])
    enveloppe.setStyle(TableStyle([('LEFTPADDING', (0, 0), (-1, -1), 0),
                                   ('RIGHTPADDING', (0, 0), (-1, -1), 0)]))
    elements += [enveloppe, Spacer(1, 1.1 * cm)]

    # ---------- Mention ----------
    mention = Table([[Paragraph(
        f"<font color='#8a8590'>Statut du paiement : {statut.lower()}.<br/>"
        "Cette facture est générée électroniquement et valable sans signature manuscrite.</font>",
        normal)]], colWidths=[largeur])
    mention.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), CREME),
        ('LINEBEFORE', (0, 0), (0, -1), 3, ORANGE),
        ('TOPPADDING', (0, 0), (-1, -1), 12),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
        ('LEFTPADDING', (0, 0), (-1, -1), 14),
    ]))
    elements.append(mention)

    def _pied_de_page(canevas, document):
        canevas.saveState()
        y = 1.3 * cm
        canevas.setStrokeColor(colors.HexColor('#e6e0ea'))
        canevas.setLineWidth(0.5)
        canevas.line(doc.leftMargin, y + 22, A4[0] - doc.rightMargin, y + 22)
        canevas.setFont('Helvetica', 7.5)
        canevas.setFillColor(GRIS)
        canevas.drawCentredString(A4[0] / 2, y + 10,
                                  f"{EMETTEUR['raison_sociale']} • {EMETTEUR['site']} • "
                                  f"{EMETTEUR['email']} • {EMETTEUR['telephone']}")
        canevas.setFillColor(ORANGE_FONCE)
        canevas.drawCentredString(A4[0] / 2, y,
                                  "Merci de votre confiance — Secureetech, assistance informatique premium")
        canevas.restoreState()

    doc.build(elements, onFirstPage=_pied_de_page, onLaterPages=_pied_de_page)
    tampon.seek(0)
    return tampon.getvalue()
