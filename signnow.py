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


# ---------------------------------------------------------------------------
# Création d'un contrat prérempli à partir du modèle SecureeTech.
#
# Enchaînement (endpoints officiels SignNow) :
#   1. POST /template/{id}/copy                  -> document signable
#   2. GET  /document/{id}                       -> noms réels des champs
#   3. PUT  /v2/documents/{id}/prefill-texts     -> valeurs préremplies
#   4. POST /link                                -> lien d'invitation
# ---------------------------------------------------------------------------

# Correspondance EXPLICITE entre les champs du modèle SecureeTech et nos
# données. SignNow nomme ses champs automatiquement ("Full Name 1",
# "Text Field 1"...) : un nom générique comme "Text Field 1" ne peut pas
# être deviné par mot-clé, il doit être listé ici.
# Relevé sur le modèle Contrat_Secureetech_modele (4 pages, 19 champs).
# Releve sur le NOUVEAU modele Contrat_secureetech (27/09/2026, 7 pages,
# 39 champs dont 9 champs texte). Les entrees de l'ancien modele sont
# conservees : un nom absent du document est simplement ignore.
MAPPING_MODELE = {
    'Full Name 1': 'client_nom',
    'Full Name 2': 'client_nom',
    'nom_client': 'client_nom',
    'Text Field 1': 'adresse',       # label « Adresse »
    'Text Field 2': 'telephone',     # label « Portable »
    'Phone Number 1': 'telephone',   # ancien modele
    'Email 1': 'email',
    # Champs date du modele : validation stricte cote SignNow (format impose),
    # et ils se remplissent au moment de la signature -> on ne les prefixe pas.
    'Date and Time 1': '',
    'Date and Time 2': '',
}

# Repli par mot-clé, pour les champs nommés lisiblement (et si le modèle
# évolue). Appliqué seulement si le nom n'est pas dans MAPPING_MODELE.
CORRESPONDANCES = (
    ('client_nom', ('nom_complet', 'nom complet', 'full name', 'client', 'nom', 'name')),
    ('adresse', ('adresse', 'address')),
    ('email', ('email', 'mail', 'courriel')),
    ('telephone', ('telephone', 'téléphone', 'phone', 'tel')),
    ('formule', ('formule', 'offre', 'service', 'abonnement', 'plan')),
    ('duree', ('duree', 'durée', 'mois', 'duration')),
    ('montant', ('montant', 'prix', 'total', 'ttc', 'amount')),
    ('date', ('date',)),
)


def _appel_ecriture(methode, chemin, corps=None):
    """POST/PUT sur l'API, avec rafraîchissement du jeton si nécessaire."""
    ids = _identifiants()
    if not configure():
        raise SignNowIndisponible("Identifiants SignNow absents du serveur.")

    jeton = ids['access_token']
    for tentative in (1, 2):
        reponse = requests.request(
            methode, f'{BASE}{chemin}',
            headers={'Authorization': f'Bearer {jeton}',
                     'Content-Type': 'application/json'},
            json=corps if corps is not None else {},
            timeout=45)
        if reponse.status_code == 401 and tentative == 1:
            jeton = _rafraichir_jeton(ids)
            continue
        if reponse.status_code not in (200, 201, 204):
            raise SignNowIndisponible(
                f"SignNow a répondu {reponse.status_code} sur {chemin} "
                f"({reponse.text[:200]}).")
        if reponse.status_code == 204 or not reponse.content:
            return {}
        try:
            return reponse.json()
        except ValueError:
            return {}

    raise SignNowIndisponible("Authentification SignNow impossible.")


def champs_du_document(document_id):
    """Noms des champs texte d'un document, tels que SignNow les connaît."""
    document = _appel(f'/document/{document_id}')
    noms = []
    for champ in (document.get('fields') or []):
        nom = (champ.get('json_attributes') or {}).get('name') or champ.get('name')
        type_champ = (champ.get('type') or '').lower()
        if nom and type_champ in ('text', ''):
            noms.append(nom)
    return noms


def _valeurs_a_prefixer(noms_champs, donnees):
    """Associe les champs réels du document à nos données.

    Le mapping explicite du modèle prime ; sinon on tente les mots-clés.
    Un champ qu'on ne sait pas identifier est laissé vide, jamais rempli
    au hasard.
    """
    fields = []
    for nom in noms_champs:
        cle = MAPPING_MODELE.get(nom)
        if cle is None:
            nom_bas = nom.lower()
            for cle_test, motifs in CORRESPONDANCES:
                if any(motif in nom_bas for motif in motifs):
                    cle = cle_test
                    break
        if not cle:
            continue
        valeur = donnees.get(cle)
        if valeur in (None, ''):
            continue
        fields.append({'field_name': nom, 'prefilled_text': str(valeur)})
    return fields


def champs_du_modele(template_id=None):
    """Inventaire des champs du modèle : nom, type, page.

    Sert au diagnostic : c'est ce qui permet de compléter MAPPING_MODELE
    sans avoir à ouvrir l'éditeur SignNow.
    """
    template_id = (template_id or os.getenv('SIGNNOW_TEMPLATE_ID', '')).strip()
    if not template_id:
        return {'ok': False, 'erreur': "SIGNNOW_TEMPLATE_ID absent du serveur."}
    if not configure():
        return {'ok': False, 'erreur': "Identifiants SignNow absents du serveur."}
    try:
        document = _appel(f'/document/{template_id}')
    except SignNowIndisponible as exc:
        return {'ok': False, 'erreur': str(exc)}

    champs = []
    for champ in (document.get('fields') or []):
        attributs = champ.get('json_attributes') or {}
        nom = attributs.get('name') or champ.get('name') or ''
        champs.append({
            'nom': nom,
            'type': champ.get('type') or '',
            'page': attributs.get('page_number'),
            'etiquette': attributs.get('label') or '',
            'associe_a': MAPPING_MODELE.get(nom, ''),
        })
    return {'ok': True, 'document': document.get('document_name') or '',
            'total': len(champs), 'champs': champs}


def creer_contrat(client_nom, email, adresse='', telephone='', formule='',
                  duree=0, montant_ttc=None, template_id=None):
    """Crée un contrat prérempli pour un client et renvoie son lien de signature.

    Renvoie un dict : {'ok': True, 'lien': ..., 'document_id': ...,
                       'champs_remplis': [...]} ou {'ok': False, 'erreur': ...}.
    Ne lève jamais : l'appelant garde son lien de repli en cas d'échec.
    """
    template_id = (template_id or os.getenv('SIGNNOW_TEMPLATE_ID', '')).strip()
    if not template_id:
        return {'ok': False,
                'erreur': "SIGNNOW_TEMPLATE_ID absent des variables du serveur."}
    if not configure():
        return {'ok': False, 'erreur': "Identifiants SignNow absents du serveur."}

    donnees = {
        'client_nom': client_nom or '',
        'adresse': adresse or '',
        'email': email or '',
        'telephone': telephone or '',
        'formule': formule or '',
        'duree': (f"{duree} mois" if duree else ''),
        'montant': (f"{float(montant_ttc):.2f} EUR" if montant_ttc else ''),
        'date': datetime.now().strftime('%d/%m/%Y'),
    }

    try:
        nom_doc = f"Contrat - {client_nom} - {datetime.now().strftime('%Y-%m-%d')}"
        copie = _appel_ecriture('POST', f'/template/{template_id}/copy',
                                {'document_name': nom_doc})
        document_id = copie.get('id') or copie.get('document_id')
        if not document_id:
            return {'ok': False,
                    'erreur': "SignNow n'a pas renvoyé d'identifiant de document."}

        champs_remplis = []
        try:
            noms = champs_du_document(document_id)
            fields = _valeurs_a_prefixer(noms, donnees)
            if fields:
                try:
                    _appel_ecriture('PUT', f'/v2/documents/{document_id}/prefill-texts',
                                    {'fields': fields})
                    champs_remplis = [f['field_name'] for f in fields]
                except SignNowIndisponible as exc_bloc:
                    # Un seul champ invalide rejette tout le lot : on remplit
                    # alors champ par champ pour garder le maximum.
                    print(f"Préremplissage en bloc refusé ({exc_bloc}) ;"
                          " nouvel essai champ par champ.")
                    for champ_seul in fields:
                        try:
                            _appel_ecriture(
                                'PUT',
                                f'/v2/documents/{document_id}/prefill-texts',
                                {'fields': [champ_seul]})
                            champs_remplis.append(champ_seul['field_name'])
                        except SignNowIndisponible as exc_champ:
                            print(f"Champ {champ_seul['field_name']}"
                                  f" refusé : {exc_champ}")
        except SignNowIndisponible as exc:
            # Document créé mais préremplissage refusé : on garde le document.
            print(f"Préremplissage SignNow : {exc}")

        lien = _appel_ecriture('POST', '/link', {'document_id': document_id})
        url = lien.get('url_no_signup') or lien.get('url') or ''
        if not url:
            return {'ok': False, 'document_id': document_id,
                    'erreur': "SignNow n'a pas renvoyé de lien de signature."}

        return {'ok': True, 'lien': url, 'document_id': document_id,
                'champs_remplis': champs_remplis}

    except SignNowIndisponible as exc:
        return {'ok': False, 'erreur': str(exc)}
    except Exception as exc:  # pragma: no cover
        return {'ok': False, 'erreur': f"Erreur inattendue : {exc}"}
