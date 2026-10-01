# -*- coding: utf-8 -*-
"""Paiements NON valides : litiges et prelevements SEPA en attente.

Ces paiements sont lus en direct chez Stripe (les deux comptes) et Mollie
(litiges / chargebacks), en lecture seule. Ils ne sont JAMAIS ecrits dans
la table paiements : ils n'entrent donc ni dans les totaux, ni dans le
chiffre des ventes. Ils servent uniquement a afficher un statut quand on
recherche un client.
"""

import os
import time
from datetime import datetime

import requests

STRIPE = 'https://api.stripe.com/v1'
MOLLIE = 'https://api.mollie.com/v2'
COMPTE_H2O = 'acct_1NLr5uKlj7JBTYX0'
DUREE_CACHE = 600  # secondes

_cache = {'quand': 0, 'lignes': []}

LIBELLES_LITIGE = {
    'warning_needs_response': 'Alerte - reponse requise',
    'warning_under_review': 'Alerte - en examen',
    'warning_closed': 'Alerte - close',
    'needs_response': 'Ouvert - reponse requise',
    'under_review': 'En examen',
    'won': 'Gagne',
    'lost': 'Perdu',
    'charge_refunded': 'Rembourse',
}


def _date(ts):
    try:
        return datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d')
    except Exception:
        return ''


def _nom_compte(cle, rang):
    try:
        rep = requests.get(f'{STRIPE}/account', auth=(cle, ''), timeout=10)
        if rep.status_code == 200:
            ident = rep.json().get('id') or ''
            if ident == COMPTE_H2O:
                return 'Stripe H2O'
            if ident:
                return 'Stripe Elite Assistance'
    except Exception:
        pass
    return f'Stripe compte {rang}'


def _client_charge(charge):
    details = (charge or {}).get('billing_details') or {}
    return (details.get('name') or '',
            details.get('email') or (charge or {}).get('receipt_email') or '')


def _stripe(cle, rang):
    lignes = []
    compte = _nom_compte(cle, rang)
    auth = (cle, '')

    # Litiges (disputes), avec la charge pour retrouver le client.
    apres = None
    for _ in range(5):
        params = {'limit': 100, 'expand[]': 'data.charge'}
        if apres:
            params['starting_after'] = apres
        try:
            rep = requests.get(f'{STRIPE}/disputes', auth=auth, params=params, timeout=20)
            if rep.status_code != 200:
                break
            corps = rep.json()
        except Exception as exc:
            print(f"Statuts : litiges {compte} : {exc}")
            break
        for d in corps.get('data') or []:
            charge = d.get('charge') if isinstance(d.get('charge'), dict) else {}
            nom, email = _client_charge(charge)
            lignes.append({
                'type': 'litige',
                'plateforme': compte,
                'montant': (d.get('amount') or 0) / 100.0,
                'date': _date(d.get('created')),
                'client_nom': nom,
                'email': email,
                'statut': LIBELLES_LITIGE.get(d.get('status') or '', d.get('status') or ''),
                'motif': d.get('reason') or '',
                'reference': d.get('id') or '',
            })
        if not corps.get('has_more') or not corps.get('data'):
            break
        apres = corps['data'][-1].get('id')

    # Prelevements SEPA pas encore encaisses (charge en statut pending).
    try:
        rep = requests.get(f'{STRIPE}/charges/search', auth=auth,
                           params={'query': "status:'pending'", 'limit': 100}, timeout=20)
        if rep.status_code == 200:
            for c in rep.json().get('data') or []:
                type_pm = ((c.get('payment_method_details') or {}).get('type') or '')
                if type_pm != 'sepa_debit':
                    continue
                nom, email = _client_charge(c)
                lignes.append({
                    'type': 'sepa_attente',
                    'plateforme': compte,
                    'montant': (c.get('amount') or 0) / 100.0,
                    'date': _date(c.get('created')),
                    'client_nom': nom,
                    'email': email,
                    'statut': 'Prelevement SEPA en attente',
                    'motif': '',
                    'reference': c.get('id') or '',
                })
    except Exception as exc:
        print(f"Statuts : SEPA {compte} : {exc}")
    return lignes


def _mollie(cle):
    lignes = []
    entetes = {'Authorization': f'Bearer {cle}'}
    try:
        rep = requests.get(f'{MOLLIE}/chargebacks', headers=entetes,
                           params={'limit': 250}, timeout=20)
        if rep.status_code != 200:
            return lignes
        retours = (rep.json().get('_embedded') or {}).get('chargebacks') or []
    except Exception as exc:
        print(f"Statuts : litiges Mollie : {exc}")
        return lignes
    for cb in retours[:100]:
        nom, email = '', ''
        try:
            p = requests.get(f"{MOLLIE}/payments/{cb.get('paymentId')}",
                             headers=entetes, timeout=15).json()
            det = p.get('details') or {}
            nom = det.get('consumerName') or det.get('cardHolder') or ''
            meta = p.get('metadata') if isinstance(p.get('metadata'), dict) else {}
            email = p.get('billingEmail') or meta.get('email') or ''
            if not nom:
                nom = (p.get('description') or '')
        except Exception:
            pass
        montant = cb.get('amount') or {}
        try:
            valeur = float(montant.get('value') or 0)
        except Exception:
            valeur = 0.0
        lignes.append({
            'type': 'litige',
            'plateforme': 'Mollie',
            'montant': valeur,
            'date': str(cb.get('createdAt') or '')[:10],
            'client_nom': nom,
            'email': email,
            'statut': 'Retour de paiement (chargeback)'
                      + (' - annule' if cb.get('reversedAt') else ''),
            'motif': ((cb.get('reason') or {}).get('description') or ''),
            'reference': cb.get('id') or '',
        })
    return lignes


def tous(forcer=False):
    """Toutes les lignes litiges + SEPA en attente (cache 10 minutes)."""
    if not forcer and _cache['quand'] and time.time() - _cache['quand'] < DUREE_CACHE:
        return _cache['lignes']
    lignes = []
    cles = [c for c in ((os.environ.get('STRIPE_API_KEY') or '').strip(),
                        (os.environ.get('STRIPE_API_KEY_SECONDARY') or '').strip()) if c]
    for rang, cle in enumerate(cles, start=1):
        try:
            lignes.extend(_stripe(cle, rang))
        except Exception as exc:
            print(f"Statuts Stripe : {exc}")
    cle_mollie = (os.environ.get('MOLLIE_API_KEY') or '').strip()
    if cle_mollie:
        try:
            lignes.extend(_mollie(cle_mollie))
        except Exception as exc:
            print(f"Statuts Mollie : {exc}")
    lignes.sort(key=lambda l: l.get('date') or '', reverse=True)
    _cache['lignes'] = lignes
    _cache['quand'] = time.time()
    return lignes


def pour_client(nom, email=''):
    """Lignes du client (email exact, sinon nom identique)."""
    nom_bas = (nom or '').strip().lower()
    email_bas = (email or '').strip().lower()
    try:
        lignes = tous()
    except Exception:
        return []
    resultat = []
    for l in lignes:
        if email_bas and (l.get('email') or '').strip().lower() == email_bas:
            resultat.append(l)
        elif nom_bas and (l.get('client_nom') or '').strip().lower() == nom_bas:
            resultat.append(l)
    return resultat


def pour_terme(terme):
    """Lignes dont le nom ou l'email contient le terme recherche."""
    t = (terme or '').strip().lower()
    if not t:
        return []
    try:
        lignes = tous()
    except Exception:
        return []
    return [l for l in lignes
            if t in (l.get('client_nom') or '').lower() or t in (l.get('email') or '').lower()]
