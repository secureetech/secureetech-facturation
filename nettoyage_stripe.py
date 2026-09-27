# -*- coding: utf-8 -*-
"""Nettoyage des paiements Stripe importés en masse.

Contexte : l'import historique interrogeait /v1/charges avec un paramètre
`status=succeeded` que cet endpoint n'accepte pas. Stripe ignorait
silencieusement le filtre et renvoyait TOUTES les tentatives (cartes
refusées, 3D Secure abandonnés, autorisations jamais capturées,
remboursements), toutes enregistrées comme payées. D'où des clients en
plusieurs exemplaires et un total très surévalué.

Ce module revérifie chaque ligne Stripe stockée, une par une, auprès de
l'API Stripe (charge par charge, en lecture seule) et classe chaque ligne :

  ok            paiement réellement encaissé              -> conservé
  partiel       encaissé puis partiellement remboursé     -> montant ajusté au net
  non_encaisse  carte refusée / 3DS abandonné / échec     -> supprimable
  non_capture   autorisation jamais débitée               -> supprimable
  rembourse     intégralement remboursé                   -> supprimable
  introuvable   référence inconnue des deux comptes       -> CONSERVÉ (prudence)
  ref_invalide  référence non Stripe (pas ch_/py_)        -> CONSERVÉ (prudence)
  erreur        Stripe injoignable pendant la vérif       -> CONSERVÉ (prudence)

Rien n'est modifié pendant la vérification : les verdicts sont stockés dans
une table de travail, un rapport est affiché, et la suppression n'a lieu
qu'après clic explicite sur « Appliquer ». En cas de doute (erreur réseau,
référence introuvable), la ligne est TOUJOURS conservée.

Clés attendues (variables Railway, clés restreintes en lecture seule) :
  STRIPE_API_KEY            compte n°1
  STRIPE_API_KEY_SECONDARY  compte n°2
La bonne clé est trouvée automatiquement pour chaque charge (une charge
n'existe que sur son compte) puis mémorisée par entité pour aller vite.
"""

import os
import time

import requests

import database

STRIPE_API_BASE = 'https://api.stripe.com/v1'

# Verdicts dont les lignes seront supprimées à l'application.
VERDICTS_SUPPRIMABLES = ('non_encaisse', 'non_capture', 'rembourse')

LIBELLES_VERDICTS = {
    'ok': 'Paiement réellement encaissé',
    'partiel': 'Encaissé, remboursé en partie (montant ajusté)',
    'non_encaisse': 'Jamais encaissé (carte refusée, 3D Secure abandonné…)',
    'non_capture': 'Autorisation jamais débitée',
    'rembourse': 'Intégralement remboursé',
    'introuvable': 'Référence inconnue des comptes Stripe (conservé)',
    'ref_invalide': 'Référence non Stripe (conservé)',
    'erreur': 'Stripe injoignable pendant la vérification (conservé)',
}


def cles_stripe():
    """Les clés API configurées, dans l'ordre d'essai."""
    cles = []
    for nom in ('STRIPE_API_KEY', 'STRIPE_API_KEY_SECONDARY'):
        valeur = (os.environ.get(nom) or '').strip()
        if valeur:
            cles.append(valeur)
    return cles


def _initialiser_table():
    conn = database.get_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS nettoyage_stripe (
            paiement_id INTEGER PRIMARY KEY,
            verdict TEXT NOT NULL,
            montant_avant REAL,
            montant_net REAL,
            detail TEXT
        )
    ''')
    conn.commit()
    conn.close()


def demarrer():
    """(Re)part de zéro : vide la table de travail."""
    _initialiser_table()
    conn = database.get_connection()
    conn.execute('DELETE FROM nettoyage_stripe')
    conn.commit()
    conn.close()


def _paiements_stripe_en_attente(limite):
    """Les prochaines lignes Stripe pas encore vérifiées."""
    conn = database.get_connection()
    lignes = conn.execute('''
        SELECT p.id, p.montant, p.reference_externe, p.entite
        FROM paiements p
        LEFT JOIN nettoyage_stripe n ON n.paiement_id = p.id
        WHERE p.source = 'stripe' AND n.paiement_id IS NULL
        ORDER BY p.id
        LIMIT ?
    ''', (limite,)).fetchall()
    conn.close()
    return [dict(l) for l in lignes]


def _verdict_depuis_charge(charge, montant_stocke):
    """Classe une charge Stripe. Renvoie (verdict, montant_net, detail)."""
    statut = charge.get('status')
    paye = bool(charge.get('paid'))
    capture = charge.get('captured')
    brut = charge.get('amount') or 0
    rembourse = charge.get('amount_refunded') or 0

    if statut != 'succeeded' or not paye:
        return 'non_encaisse', None, 'status=%s' % statut
    if capture is False:
        return 'non_capture', None, ''
    if charge.get('refunded') or (rembourse and rembourse >= brut):
        return 'rembourse', None, ''
    if rembourse:
        net = round((brut - rembourse) / 100.0, 2)
        if montant_stocke is not None and abs(net - float(montant_stocke)) < 0.01:
            # Le montant stocké est déjà le net : rien à ajuster.
            return 'ok', None, ''
        return 'partiel', net, 'remboursé %.2f' % (rembourse / 100.0)
    return 'ok', None, ''


def verifier_un_lot(taille_lot=80, pause=0.05):
    """Vérifie jusqu'à `taille_lot` lignes ; renvoie le nombre traité.

    Toute erreur individuelle donne le verdict prudent `erreur` (ligne
    conservée) : la vérification n'échoue jamais en bloc.
    """
    _initialiser_table()
    cles = cles_stripe()
    if not cles:
        return -1  # aucune clé configurée

    lot = _paiements_stripe_en_attente(taille_lot)
    if not lot:
        return 0

    http = requests.Session()
    # La bonne clé par entité, mémorisée au fil du lot pour éviter de
    # doubler les appels (une charge n'existe que sur son compte Stripe).
    cle_par_entite = {}
    resultats = []

    for p in lot:
        ref = (p.get('reference_externe') or '').strip()
        entite = p.get('entite') or ''
        montant_avant = p.get('montant')

        if not (ref.startswith('ch_') or ref.startswith('py_')):
            resultats.append((p['id'], 'ref_invalide', montant_avant, None, ref[:24]))
            continue

        ordre = list(cles)
        preferee = cle_par_entite.get(entite)
        if preferee in ordre:
            ordre.remove(preferee)
            ordre.insert(0, preferee)

        verdict, net, detail = 'introuvable', None, ''
        for cle in ordre:
            try:
                rep = http.get(
                    '%s/charges/%s' % (STRIPE_API_BASE, ref),
                    auth=(cle, ''),
                    timeout=20,
                )
            except requests.RequestException as exc:
                verdict, net, detail = 'erreur', None, type(exc).__name__
                break
            if rep.status_code == 200:
                cle_par_entite[entite] = cle
                verdict, net, detail = _verdict_depuis_charge(rep.json(), montant_avant)
                break
            if rep.status_code in (404, 403, 401):
                continue  # mauvaise clé pour cette charge : essayer l'autre
            verdict, net, detail = 'erreur', None, 'HTTP %s' % rep.status_code
            break

        resultats.append((p['id'], verdict, montant_avant, net, detail))
        if pause:
            time.sleep(pause)

    conn = database.get_connection()
    conn.executemany('''
        INSERT OR REPLACE INTO nettoyage_stripe
            (paiement_id, verdict, montant_avant, montant_net, detail)
        VALUES (?, ?, ?, ?, ?)
    ''', resultats)
    conn.commit()
    conn.close()
    return len(resultats)


def etat():
    """Où en est la vérification + rapport chiffré."""
    _initialiser_table()
    conn = database.get_connection()
    total_stripe = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(montant), 0) FROM paiements WHERE source = 'stripe'"
    ).fetchone()
    verifies = conn.execute('SELECT COUNT(*) FROM nettoyage_stripe').fetchone()[0]
    par_verdict = conn.execute('''
        SELECT n.verdict, COUNT(*), COALESCE(SUM(n.montant_avant), 0),
               COALESCE(SUM(CASE WHEN n.verdict = 'partiel'
                                 THEN n.montant_avant - n.montant_net END), 0)
        FROM nettoyage_stripe n
        GROUP BY n.verdict
    ''').fetchall()
    autres = conn.execute(
        "SELECT COALESCE(SUM(montant), 0) FROM paiements WHERE source != 'stripe'"
    ).fetchone()[0]
    conn.close()

    verdicts = {}
    montant_supprime = 0.0
    ajustement_partiels = 0.0
    for verdict, nombre, montant, ecart_partiel in par_verdict:
        verdicts[verdict] = {
            'libelle': LIBELLES_VERDICTS.get(verdict, verdict),
            'nombre': nombre,
            'montant': round(montant, 2),
        }
        if verdict in VERDICTS_SUPPRIMABLES:
            montant_supprime += montant
        if verdict == 'partiel':
            ajustement_partiels += ecart_partiel or 0

    total_avant = round(total_stripe[1] + autres, 2)
    total_apres = round(total_avant - montant_supprime - ajustement_partiels, 2)

    return {
        'total_stripe': total_stripe[0],
        'verifies': verifies,
        'restants': max(0, total_stripe[0] - verifies),
        'verdicts': verdicts,
        'lignes_a_supprimer': sum(
            v['nombre'] for k, v in verdicts.items() if k in VERDICTS_SUPPRIMABLES
        ),
        'montant_supprime': round(montant_supprime, 2),
        'ajustement_partiels': round(ajustement_partiels, 2),
        'total_avant': total_avant,
        'total_apres': total_apres,
    }


def appliquer():
    """Applique les verdicts : supprime les fausses lignes, ajuste les
    remboursements partiels, régénère la synthèse clients. Renvoie un
    récapitulatif. Les verdicts prudents (introuvable, erreur…) ne sont
    jamais touchés."""
    _initialiser_table()
    conn = database.get_connection()

    supprimees = conn.execute('''
        DELETE FROM paiements
        WHERE id IN (SELECT paiement_id FROM nettoyage_stripe
                     WHERE verdict IN (?, ?, ?))
    ''', VERDICTS_SUPPRIMABLES).rowcount

    ajustees = conn.execute('''
        UPDATE paiements
        SET montant = (SELECT n.montant_net FROM nettoyage_stripe n
                       WHERE n.paiement_id = paiements.id)
        WHERE id IN (SELECT paiement_id FROM nettoyage_stripe
                     WHERE verdict = 'partiel' AND montant_net IS NOT NULL)
    ''').rowcount

    conn.execute('DELETE FROM nettoyage_stripe')
    conn.commit()
    conn.close()

    database.regenerate_clients_summary()
    return {'supprimees': supprimees, 'ajustees': ajustees}
