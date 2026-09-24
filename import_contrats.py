#!/usr/bin/env python3
"""
Importe les contrats Dropbox Sign et les relie aux clients par email.

La clé est lue dans DROPBOX_SIGN_API_KEY (jamais écrite dans le dépôt).
"""
import os
import sys
import sqlite3
from collections import Counter

import requests

import database

CLE = os.getenv('DROPBOX_SIGN_API_KEY', '')
BASE = 'https://api.hellosign.com/v3'
DB = 'facturation.db'


def creer_table():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute('''
    CREATE TABLE IF NOT EXISTS contrats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signature_request_id TEXT UNIQUE,
        titre TEXT,
        objet TEXT,
        signataire_email TEXT,
        signataire_nom TEXT,
        statut TEXT,
        signe INTEGER,
        date_creation TEXT,
        date_signature TEXT
    )
    ''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_contrats_email ON contrats(signataire_email)')
    conn.commit()
    conn.close()


def recuperer_contrats():
    contrats = []
    page = 1
    while True:
        r = requests.get(f'{BASE}/signature_request/list',
                         auth=(CLE, ''),
                         params={'page': page, 'page_size': 100},
                         timeout=30)
        if r.status_code != 200:
            print(f'  ERREUR {r.status_code}: {r.text[:200]}')
            break

        data = r.json()
        lot = data.get('signature_requests', [])
        contrats.extend(lot)
        infos = data.get('list_info', {})
        print(f'  page {page}/{infos.get("num_pages")} — {len(contrats)} contrats')

        if page >= infos.get('num_pages', 1):
            break
        page += 1

    return contrats


def horodatage(valeur):
    """Convertit un timestamp Unix en date lisible."""
    if not valeur:
        return ''
    from datetime import datetime
    try:
        return datetime.fromtimestamp(int(valeur)).strftime('%Y-%m-%d')
    except (ValueError, OSError, TypeError):
        return ''


def main():
    if not CLE:
        print("DROPBOX_SIGN_API_KEY absente de l'environnement.")
        sys.exit(1)

    database.init_db()
    creer_table()

    print('Récupération des contrats Dropbox Sign :')
    contrats = recuperer_contrats()

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute('DELETE FROM contrats')

    statuts = Counter()
    for sr in contrats:
        signatures = sr.get('signatures') or [{}]
        premiere = signatures[0]
        statut = premiere.get('status_code', 'inconnu')
        statuts[statut] += 1

        c.execute('''
        INSERT OR REPLACE INTO contrats
        (signature_request_id, titre, objet, signataire_email, signataire_nom,
         statut, signe, date_creation, date_signature)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            sr.get('signature_request_id'),
            sr.get('title', ''),
            sr.get('subject', ''),
            (premiere.get('signer_email_address') or '').strip().lower(),
            (premiere.get('signer_name') or '').strip(),
            statut,
            1 if sr.get('is_complete') else 0,
            horodatage(sr.get('created_at')),
            horodatage(premiere.get('signed_at')),
        ))

    conn.commit()

    # Liaison aux clients existants, par email
    c.execute('''
    SELECT COUNT(DISTINCT ct.signature_request_id)
    FROM contrats ct
    JOIN paiements p ON LOWER(TRIM(p.email)) = ct.signataire_email
    WHERE ct.signataire_email <> ''
    ''')
    relies = c.fetchone()[0]

    c.execute('SELECT COUNT(*) FROM contrats')
    total = c.fetchone()[0]

    print(f'\n{"=" * 55}')
    print(f'Contrats importés : {total}')
    print(f'Reliés à un client par email : {relies}')
    print(f'Statuts : {dict(statuts)}')
    print('=' * 55)

    conn.close()


if __name__ == '__main__':
    main()
