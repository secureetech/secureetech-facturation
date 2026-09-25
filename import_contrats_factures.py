#!/usr/bin/env python3
"""
Relie au client ses contrats (Dropbox Sign, SignNow) et ses factures (Stripe).

Contrats — deux origines possibles :
  - dropbox_sign : ancien prestataire, récupéré par API
  - signnow      : prestataire actuel, contrats signés archivés en PDF

Factures — Stripe, sur les deux comptes.
"""
import os
import re
import sys
import shutil
import sqlite3
from datetime import datetime

import requests

import database

DB = 'facturation.db'
DOSSIER_PDF = 'contrats_signes'


# ---------- schéma ----------

def preparer_schema():
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    colonnes = [x[1] for x in c.execute('PRAGMA table_info(contrats)')]
    if 'source' not in colonnes:
        c.execute("ALTER TABLE contrats ADD COLUMN source TEXT DEFAULT 'dropbox_sign'")
    if 'fichier_local' not in colonnes:
        c.execute("ALTER TABLE contrats ADD COLUMN fichier_local TEXT DEFAULT ''")
    c.execute("UPDATE contrats SET source='dropbox_sign' WHERE IFNULL(source,'')=''")

    c.execute('''
    CREATE TABLE IF NOT EXISTS factures_externes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        facture_id TEXT UNIQUE,
        numero TEXT,
        source TEXT,
        client_email TEXT,
        client_nom TEXT,
        montant REAL,
        devise TEXT,
        statut TEXT,
        date_facture TEXT,
        lien_pdf TEXT,
        lien_portail TEXT
    )
    ''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_factures_email ON factures_externes(client_email)')
    conn.commit()
    conn.close()


# ---------- contrats SignNow archivés ----------

def importer_contrats_archives(dossier_source):
    """Les PDF sont nommés NomDuClient_email@domaine.pdf"""
    os.makedirs(DOSSIER_PDF, exist_ok=True)
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("DELETE FROM contrats WHERE source='signnow'")

    importes = 0
    for nom_fichier in sorted(os.listdir(dossier_source)):
        if not nom_fichier.lower().endswith('.pdf'):
            continue

        base = nom_fichier[:-4]
        if '_' not in base:
            continue

        brut_nom, brut_reste = base.split('_', 1)
        # Certains fichiers ont un suffixe _2 : on garde la partie email
        email = ''
        correspondance = re.search(r'[\w.+-]+@[\w.-]+\.\w+', brut_reste)
        if correspondance:
            email = correspondance.group(0).lower()

        # "COUVREUXMICHEL" ou "GarreBrigitte" -> lisible
        lisible = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', brut_nom).strip()

        shutil.copy2(os.path.join(dossier_source, nom_fichier),
                     os.path.join(DOSSIER_PDF, nom_fichier))

        c.execute('''
        INSERT OR REPLACE INTO contrats
        (signature_request_id, titre, objet, signataire_email, signataire_nom,
         statut, signe, date_creation, date_signature, source, fichier_local)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            'signnow:' + nom_fichier,
            'Contrat SecureeTech',
            'Contrat signé',
            email,
            lisible,
            'signed',
            1,
            '',
            '',
            'signnow',
            nom_fichier,
        ))
        importes += 1

    conn.commit()
    conn.close()
    return importes


# ---------- factures Stripe ----------

def importer_factures_stripe(cle, libelle):
    if not cle:
        return 0

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    total = 0
    starting_after = None
    while True:
        params = {'limit': 100}
        if starting_after:
            params['starting_after'] = starting_after

        r = requests.get('https://api.stripe.com/v1/invoices',
                         headers={'Authorization': f'Bearer {cle}'},
                         params=params, timeout=30)
        if r.status_code != 200:
            print(f'  {libelle} : erreur {r.status_code}')
            break

        data = r.json()
        lot = data.get('data', [])
        for f in lot:
            c.execute('''
            INSERT OR REPLACE INTO factures_externes
            (facture_id, numero, source, client_email, client_nom, montant,
             devise, statut, date_facture, lien_pdf, lien_portail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                f.get('id'),
                f.get('number') or '',
                'stripe',
                (f.get('customer_email') or '').strip().lower(),
                (f.get('customer_name') or '').strip(),
                (f.get('total') or 0) / 100,
                (f.get('currency') or 'eur').upper(),
                f.get('status') or '',
                datetime.fromtimestamp(f['created']).strftime('%Y-%m-%d') if f.get('created') else '',
                f.get('invoice_pdf') or '',
                f.get('hosted_invoice_url') or '',
            ))
            total += 1

        print(f'  {libelle} : {total} factures')
        if not data.get('has_more') or not lot:
            break
        starting_after = lot[-1]['id']

    conn.commit()
    conn.close()
    return total


def main():
    database.init_db()
    database.init_contrats()
    preparer_schema()

    source_pdf = sys.argv[1] if len(sys.argv) > 1 else '/tmp/cz'
    if os.path.isdir(source_pdf):
        n = importer_contrats_archives(source_pdf)
        print(f'Contrats SignNow archivés : {n}')

    print('Factures Stripe :')
    a = importer_factures_stripe(os.getenv('STRIPE_API_KEY', ''), 'compte 1')
    b = importer_factures_stripe(os.getenv('STRIPE_API_KEY_SECONDARY', ''), 'compte 2')

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT source, COUNT(*) FROM contrats GROUP BY source")
    print('\nContrats par origine :', dict(c.fetchall()))
    c.execute("SELECT COUNT(*) FROM factures_externes")
    print('Factures en base :', c.fetchone()[0])
    c.execute('''SELECT COUNT(DISTINCT f.facture_id) FROM factures_externes f
                 JOIN paiements p ON LOWER(TRIM(p.email)) = f.client_email
                 WHERE f.client_email <> ''  ''')
    print('Factures reliées à un client :', c.fetchone()[0])
    conn.close()


if __name__ == '__main__':
    main()
