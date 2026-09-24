#!/usr/bin/env python3
"""
Réimporte les charges Stripe en résolvant le vrai nom du client.

Ordre de résolution du nom :
  1. charge.billing_details.name   (nom saisi au paiement)
  2. customer.name                 (objet Customer, via expand)
  3. customer.email / receipt_email
  4. charge.description
  5. 'Client Stripe inconnu'

Clés lues depuis l'environnement : STRIPE_API_KEY, STRIPE_API_KEY_SECONDARY
"""
import os
import sys
import sqlite3
import requests
from datetime import datetime
from collections import Counter

import database

DB = 'facturation.db'


def fetch_charges(api_key, label):
    """Récupère toutes les charges réussies, avec l'objet Customer développé."""
    print(f"\n=== {label} ===")
    headers = {'Authorization': f'Bearer {api_key}'}
    charges = []
    starting_after = None

    while True:
        params = {'limit': 100, 'expand[]': 'data.customer'}
        if starting_after:
            params['starting_after'] = starting_after

        r = requests.get('https://api.stripe.com/v1/charges',
                         headers=headers, params=params, timeout=30)
        if r.status_code != 200:
            print(f"  ERREUR {r.status_code}: {r.text[:200]}")
            break

        data = r.json()
        batch = data.get('data', [])
        charges.extend(batch)
        print(f"  ... {len(charges)} charges récupérées")

        if not data.get('has_more') or not batch:
            break
        starting_after = batch[-1]['id']

    return charges


def resolve_name(charge):
    """Trouve le meilleur nom disponible pour cette charge."""
    bd = charge.get('billing_details') or {}
    if bd.get('name'):
        return bd['name'].strip(), (bd.get('email') or '').strip()

    cust = charge.get('customer')
    if isinstance(cust, dict):
        if cust.get('name'):
            return cust['name'].strip(), (cust.get('email') or '').strip()
        if cust.get('email'):
            return cust['email'].strip(), cust['email'].strip()

    email = charge.get('receipt_email') or bd.get('email') or ''
    if email:
        return email.strip(), email.strip()

    if charge.get('description'):
        return charge['description'].strip()[:60], ''

    return 'Client Stripe inconnu', ''


def main():
    keys = [
        (os.getenv('STRIPE_API_KEY', ''), 'Stripe compte 1 (H2OCONSULTING)'),
        (os.getenv('STRIPE_API_KEY_SECONDARY', ''), 'Stripe compte 2 (ELITE-ASSISTANCE)'),
    ]
    if not any(k for k, _ in keys):
        print("Aucune clé Stripe dans l'environnement.")
        sys.exit(1)

    database.init_db()

    # On repart à zéro côté Stripe uniquement (GoCardless et Mollie sont conservés)
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("DELETE FROM paiements WHERE source = 'stripe'")
    removed = cur.rowcount
    conn.commit()
    conn.close()
    print(f"Anciennes lignes Stripe supprimées : {removed}")

    total = 0
    skipped = 0
    currencies = Counter()
    named = 0

    for api_key, label in keys:
        if not api_key:
            continue

        for charge in fetch_charges(api_key, label):
            # Uniquement les paiements réellement encaissés
            if charge.get('status') != 'succeeded':
                skipped += 1
                continue
            if charge.get('refunded'):
                skipped += 1
                continue
            # Montant net des remboursements partiels
            amount = (charge['amount'] - charge.get('amount_refunded', 0)) / 100
            if amount <= 0:
                skipped += 1
                continue

            nom, email = resolve_name(charge)
            if nom != 'Client Stripe inconnu':
                named += 1

            currencies[charge.get('currency', '?').upper()] += 1

            database.ajouter_paiement(
                date_paiement=datetime.fromtimestamp(charge['created']).strftime('%Y-%m-%d'),
                source='stripe',
                montant=amount,
                client_nom=nom,
                email=email,
                reference_externe=charge['id'],
            )
            total += 1

    database.regenerate_clients_summary()

    print("\n" + "=" * 60)
    print(f"Paiements Stripe importés : {total}")
    print(f"  dont avec un vrai nom   : {named} ({named * 100 // max(total, 1)} %)")
    print(f"Ignorés (échec/remboursé) : {skipped}")
    print(f"Devises : {dict(currencies)}")
    print("=" * 60)


if __name__ == '__main__':
    main()
