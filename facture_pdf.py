#!/usr/bin/env python3
"""Génération du PDF de facture aux couleurs SecureeTech."""
import io
import os
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

BLEU = colors.HexColor('#0967a0')
BLEU_CLAIR = colors.HexColor('#eef2f5')
BORDURE = colors.HexColor('#dde5ea')
GRIS = colors.HexColor('#6b7684')

# Société émettrice. Modifiable par variables d'environnement.
EMETTEUR = {
    'raison_sociale': os.getenv('SOCIETE_NOM', 'ELITE-ASSISTANCE S.L.'),
    'marque': os.getenv('SOCIETE_MARQUE', 'SecureeTech'),
    'adresse': os.getenv('SOCIETE_ADRESSE',
                         'La Reserva de Marbella, Manzana 6, Bloque 3<br/>29604 Marbella, Málaga, Espagne'),
    'identifiant': os.getenv('SOCIETE_IDENTIFIANT', 'CIF B01773654'),
    'email': os.getenv('SOCIETE_EMAIL', 'contact@secureetech.com'),
    'telephone': os.getenv('SOCIETE_TELEPHONE', '05 54 54 23 43'),
    'site': os.getenv('SOCIETE_SITE', 'secureetech.com'),
}


def _fr(montant):
    """1234.5 -> « 1 234,50 € »"""
    return f"{montant:,.2f} €".replace(',', ' ').replace('.', ',').replace(' ', ' ')


def _date_fr(texte):
    try:
        return datetime.strptime(texte, '%Y-%m-%d').strftime('%d/%m/%Y')
    except (ValueError, TypeError):
        return texte or ''


def construire(facture, taux_tva=0.21):
    """Retourne les octets du PDF pour une ligne de la table factures."""
    tampon = io.BytesIO()
    doc = SimpleDocTemplate(tampon, pagesize=A4,
                            leftMargin=1.8 * cm, rightMargin=1.8 * cm,
                            topMargin=1.6 * cm, bottomMargin=1.6 * cm,
                            title=f"Facture {facture.get('numero_facture', '')}")

    styles = getSampleStyleSheet()
    normal = ParagraphStyle('N', parent=styles['Normal'], fontSize=9, leading=13)
    petit = ParagraphStyle('P', parent=normal, fontSize=8, textColor=GRIS, leading=11)
    titre = ParagraphStyle('T', parent=styles['Heading1'], fontSize=22,
                           textColor=BLEU, spaceAfter=0, leading=25)
    droite = ParagraphStyle('D', parent=normal, alignment=2)
    droite_titre = ParagraphStyle('DT', parent=titre, alignment=2)

    ht = float(facture.get('montant_ht') or 0)
    tva = float(facture.get('tva') or 0)
    ttc = float(facture.get('montant') or 0)

    # Anciennes factures sans détail : on reconstitue
    if ht <= 0 and ttc > 0:
        ht = round(ttc / (1 + taux_tva), 2)
        tva = round(ttc - ht, 2)

    elements = []

    # --- En-tête : émetteur à gauche, titre à droite ---
    entete = Table([[
        Paragraph(f"<b><font size=15 color='#0b2233'>{EMETTEUR['marque']}</font></b><br/>"
                  f"<font size=8 color='#6b7684'>{EMETTEUR['raison_sociale']}<br/>"
                  f"{EMETTEUR['adresse']}<br/>{EMETTEUR['identifiant']}<br/>"
                  f"{EMETTEUR['email']} · {EMETTEUR['telephone']}</font>", normal),
        Paragraph("FACTURE", droite_titre),
    ]], colWidths=[10.5 * cm, 6.5 * cm])
    entete.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'),
                                ('LEFTPADDING', (0, 0), (-1, -1), 0),
                                ('RIGHTPADDING', (0, 0), (-1, -1), 0)]))
    elements += [entete, Spacer(1, 0.4 * cm)]

    # Filet de couleur
    filet = Table([['']], colWidths=[17 * cm], rowHeights=[3])
    filet.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), BLEU)]))
    elements += [filet, Spacer(1, 0.6 * cm)]

    # --- Références et client ---
    client = facture.get('client_nom') or ''
    email = facture.get('email') or ''
    bloc = Table([[
        Paragraph(f"<font size=8 color='#6b7684'>FACTURÉ À</font><br/>"
                  f"<b>{client}</b>" + (f"<br/><font size=8 color='#6b7684'>{email}</font>" if email else ''),
                  normal),
        Paragraph(f"<font size=8 color='#6b7684'>NUMÉRO</font><br/><b>{facture.get('numero_facture','')}</b>"
                  f"<br/><br/><font size=8 color='#6b7684'>DATE</font><br/>"
                  f"{_date_fr(facture.get('date_facture'))}", droite),
    ]], colWidths=[10.5 * cm, 6.5 * cm])
    bloc.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'),
                              ('LEFTPADDING', (0, 0), (-1, -1), 0),
                              ('RIGHTPADDING', (0, 0), (-1, -1), 0)]))
    elements += [bloc, Spacer(1, 0.8 * cm)]

    # --- Ligne de prestation ---
    designation = facture.get('description') or facture.get('formule') or 'Prestation SecureeTech'
    lignes = [
        ['Désignation', 'Qté', 'Prix unitaire HT', 'Total HT'],
        [Paragraph(designation, normal), '1', _fr(ht), _fr(ht)],
    ]
    tableau = Table(lignes, colWidths=[9.5 * cm, 1.5 * cm, 3 * cm, 3 * cm])
    tableau.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), BLEU),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
        ('ALIGN', (1, 0), (1, -1), 'CENTER'),
        ('GRID', (0, 0), (-1, -1), 0.4, BORDURE),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 7),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
    ]))
    elements += [tableau, Spacer(1, 0.5 * cm)]

    # --- Totaux ---
    totaux = Table([
        ['Total HT', _fr(ht)],
        [f'TVA {int(taux_tva * 100)} %', _fr(tva)],
        ['Total TTC', _fr(ttc)],
    ], colWidths=[3.5 * cm, 3.5 * cm])
    totaux.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('LINEABOVE', (0, 2), (-1, 2), 0.8, BLEU),
        ('BACKGROUND', (0, 2), (-1, 2), BLEU_CLAIR),
        ('FONTNAME', (0, 2), (-1, 2), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 2), (-1, 2), 11),
        ('TEXTCOLOR', (0, 2), (-1, 2), BLEU),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    enveloppe = Table([['', totaux]], colWidths=[10 * cm, 7 * cm])
    enveloppe.setStyle(TableStyle([('LEFTPADDING', (0, 0), (-1, -1), 0),
                                   ('RIGHTPADDING', (0, 0), (-1, -1), 0)]))
    elements += [enveloppe, Spacer(1, 1.2 * cm)]

    # --- Pied de page ---
    formule = facture.get('formule') or ''
    duree = facture.get('duree') or 0
    if formule:
        mention = f"Formule {formule}" + (f", engagement de {duree} mois." if duree else ", paiement unique.")
        elements += [Paragraph(mention, petit), Spacer(1, 0.3 * cm)]

    elements.append(Paragraph(
        f"{EMETTEUR['raison_sociale']} — {EMETTEUR['identifiant']} — {EMETTEUR['site']}<br/>"
        f"TVA appliquée au taux de {int(taux_tva * 100)} %. Facture payable à réception.", petit))

    doc.build(elements)
    tampon.seek(0)
    return tampon.getvalue()
