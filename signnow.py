#!/usr/bin/env python3
"""
Synchronisation des contrats depuis l'API SignNow.

Les identifiants sont lus dans l'environnement (déjà présents sur Railway) :
  SIGNNOW_CLIENT_ID, SIGNNOW_CLIENT_SECRET,
  SIGNNOW_INITIAL_ACCESS_TOKEN, SIGNNOW_INITIAL_REFRESH_TOKEN

Le jeton d'accès est utilisé tel quel, et rafraîchi automatiquement
avec le jeton de rafraîchissement s'il est expiré.
"""
import base64
import os
import re
import sqlite3
from datetime import datetime

import requests

BASE = 'https://api.signnow.com'
DB = 'facturation.db'

# Un document d'essai ne doit pas polluer le fichier client.
EMAILS_ESSAI = re.compile(
    r'@(example\.com|test\.com|test\d*\.fr|no\.reply)$|^(guest_signer_|diag|test)',
    re.IGNORECASE)
NOMS_ESSAI = re.compile(
    r'\b(test|diag|regression|verif|check|essai|demo|repeat|curltest|timing|'
    r'directajax|reqlog|hidebox|singlelink|cleane2e|emailtest|finalcheck|'
    r'modele|layout|normalize|wiring|split)\b',
    re.IGNORECASE)


class SignNowIndisponible(Exception):
    """Identifiants absents ou refusés."""


def _identifiants():
    return {
        'client_id': os.getenv('SIGNNOW_CLIENT_ID', ''),
        'client_secret': os.getenv('SIGNNOW_CLIENT_SECRET', ''),
        'access_token': os.getenv('SIGNNOW_INITIAL_ACCESS_TOKEN', ''),
        'refresh_token': os.getenv('SIGNNOW_INITIAL_REFRESH_TOKEN', ''),
    }


def configure():
    """Vrai si l'application dispose de quoi appeler SignNow."""
    ids = _identifiants()
    return bool(ids['access_token'] or (ids['client_id'] and ids['refresh_token']))


def _rafraichir_jeton(ids):
    """Échange le jeton de rafraîchissement contre un nouveau jeton d'accès."""
    if not (ids['client_id'] and ids['client_secret'] and ids['refresh_token']):
        raise SignNowIndisponible(
            "Jeton expiré et impossible à rafraîchir : "
            "SIGNNOW_CLIENT_ID, SIGNNOW_CLIENT_SECRET ou "
            "SIGNNOW_INITIAL_REFRESH_TOKEN manquant.")

    autorisation = base64.b64encode(
        f"{ids['client_id']}:{ids['client_secret']}".encode()).decode()

    reponse = requests.post(
        f'{BASE}/oauth2/token',
        headers={'Authorization': f'Basic {autorisation}'},
        data={'grant_type': 'refresh_token', 'refresh_token': ids['refresh_token']},
        timeout=30)

    if reponse.status_code != 200:
        raise SignNowIndisponible(
            f"Rafraîchissement refusé par SignNow (code {reponse.status_code}).")

    return reponse.json().get('access_token', '')


def _appel(chemin, params=None, binaire=False):
    """Appelle l'API, en rafraîchissant le jeton une fois si nécessaire."""
    ids = _identifiants()
    if not configure():
        raise SignNowIndisponible("Identifiants SignNow absents du serveur.")

    jeton = ids['access_token']
    for tentative in (1, 2):
        reponse = requests.get(f'{BASE}{chemin}',
                               headers={'Authorization': f'Bearer {jeton}'},
                               params=params or {}, timeout=45)
        if reponse.status_code == 401 and tentative == 1:
            jeton = _rafraichir_jeton(ids)
            continue
        if reponse.status_code != 200:
            raise SignNowIndisponible(
                f"SignNow a répondu {reponse.status_code} sur {chemin}.")
        return reponse.content if binaire else reponse.json()

    raise SignNowIndisponible("Authentification SignNow impossible.")


def est_un_essai(nom, email):
    """Écarte les documents de test et les brouillons sans destinataire réel."""
    if email and EMAILS_ESSAI.search(email):
        return True
    if nom and NOMS_ESSAI.search(nom):
        return True
    return False


def _nom_client(nom_document):
    """« Contrat - Jean Dupont - 2026-09-24 » -> « Jean Dupont »"""
    morceaux = [m.strip() for m in (nom_document or '').split(' - ')]
    if len(morceaux) >= 2:
        return morceaux[1]
    return (nom_document or '').strip()


def lister_documents(maximum=400):
    """Parcourt les documents du compte, du plus récent au plus ancien."""
    documents = []
    page = 1
    while len(documents) < maximum:
        lot = _appel('/user/documentsv2',
                     {'per_page': 100, 'page': page,
                      'sortby': 'created', 'order': 'desc'})
        if isinstance(lot, dict):
            lot = lot.get('data') or lot.get('documents') or []
        if not lot:
            break
        documents.extend(lot)
        if len(lot) < 100:
            break
        page += 1
    return documents[:maximum]


def _signataire(document):
    """Email et statut du premier destinataire réel."""
    for cle in ('field_invites', 'requests', 'invites'):
        for invite in document.get(cle) or []:
            email = (invite.get('email') or invite.get('signer_email') or '').strip().lower()
            email = email.split(' ')[0]
            if email:
                return email, (invite.get('status') or '').lower()
    return '', ''


def synchroniser():
    """Met à jour la table des contrats avec les documents SignNow.

    Tout l'historique est conservé : un client ayant signé plusieurs
    contrats les voit tous apparaître, chacun avec sa date.

    Ne touche ni aux contrats Dropbox Sign, ni aux contrats archivés :
    seuls ceux marqués « signnow_api » sont remplacés.
    """
    documents = lister_documents()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("DELETE FROM contrats WHERE source = 'signnow_api'")

    retenus = 0
    ecartes = 0
    for doc in documents:
        nom_doc = doc.get('document_name') or doc.get('name') or ''
        email, statut_invite = _signataire(doc)
        nom_client = _nom_client(nom_doc)

        if est_un_essai(nom_client, email) or not email:
            ecartes += 1
            continue

        cree = doc.get('created')
        date_creation = ''
        if cree:
            try:
                date_creation = datetime.fromtimestamp(int(cree)).strftime('%Y-%m-%d')
            except (ValueError, OSError, TypeError):
                date_creation = ''

        signe = 1 if statut_invite in ('fulfilled', 'completed', 'signed') else 0

        c.execute('''
        INSERT OR REPLACE INTO contrats
        (signature_request_id, titre, objet, signataire_email, signataire_nom,
         statut, signe, date_creation, date_signature, source, fichier_local)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            'signnow_api:' + doc.get('id', ''),
            nom_doc or 'Contrat SecureeTech',
            'Contrat',
            email,
            nom_client,
            statut_invite or ('signé' if signe else 'en attente'),
            signe,
            date_creation,
            date_creation if signe else '',
            'signnow_api',
            '',
        ))
        retenus += 1

    conn.commit()
    conn.close()

    return {
        'examines': len(documents),
        'retenus': retenus,
        'ecartes_essais': ecartes,
    }


def telecharger(document_id):
    """PDF signé d'un document SignNow."""
    return _appel(f'/document/{document_id}/download',
                  {'type': 'collapsed'}, binaire=True)
