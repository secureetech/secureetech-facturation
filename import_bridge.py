#!/usr/bin/env python3
"""
Importe les paiements Bridge (API Paiements v3).

N'importe que les virements réellement exécutés :
  ACSC / ACCC / ACSP = settlement accepté
Les RJCT (rejetés), PDNG (en attente) et abandons sont ignorés.

Les IBAN ne sont pas stockés : seuls le nom du payeur, le montant,
la date et l'identifiant Bridge sont conservés.
"""
import os
import sys
import sqlite3
from collections import Counter

import requests
import database

BASE = "https://api.bridgeapi.io"
STATUTS_OK = {"ACSC", "ACCC", "ACSP"}

HEADERS = {
    "Bridge-Version": "2025-01-15",
    "Content-Type": "application/json",
    "Client-Id": os.getenv("BRIDGE_CLIENT_ID", ""),
    "Client-Secret": os.getenv("BRIDGE_CLIENT_SECRET", ""),
}


def recuperer_tout(chemin):
    """Parcourt toutes les pages d'une ressource Bridge v3."""
    url = f"{BASE}{chemin}?limit=100"
    resultats = []

    while url:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            print(f"  ERREUR {r.status_code} sur {url}: {r.text[:200]}")
            break

        data = r.json()
        lot = data.get("resources", [])
        resultats.extend(lot)
        print(f"  ... {len(resultats)} enregistrements")

        suivant = (data.get("pagination") or {}).get("next_uri")
        url = f"{BASE}{suivant}" if suivant else None

    return resultats


def nom_payeur(pr):
    sender = pr.get("sender") or {}
    user = pr.get("user") or {}
    for candidat in (sender.get("name"), user.get("company_name"), pr.get("client_reference")):
        if candidat and str(candidat).strip():
            return str(candidat).strip()
    return "Client Bridge inconnu"


def montant(pr):
    """Le montant peut être sur la demande ou sur ses transactions."""
    if pr.get("amount") is not None:
        return float(pr["amount"])
    total = 0.0
    for t in pr.get("transactions") or []:
        if t.get("amount") is not None:
            total += float(t["amount"])
    return total


def date_paiement(pr):
    brut = (pr.get("executed_at") or pr.get("updated_at")
            or pr.get("created_at") or "")
    return brut[:10]


def main():
    if not HEADERS["Client-Id"] or not HEADERS["Client-Secret"]:
        print("Identifiants Bridge absents de l'environnement.")
        sys.exit(1)

    database.init_db()

    conn = sqlite3.connect("facturation.db")
    cur = conn.cursor()
    cur.execute("DELETE FROM paiements WHERE source = 'bridge'")
    print(f"Anciennes lignes Bridge supprimées : {cur.rowcount}")
    conn.commit()
    conn.close()

    print("\nRécupération des demandes de paiement Bridge :")
    demandes = recuperer_tout("/v3/payment/payment-requests")

    statuts = Counter(d.get("status", "?") for d in demandes)
    print(f"\nRépartition des statuts : {dict(statuts)}")

    importes = 0
    ignores = 0
    for pr in demandes:
        if pr.get("status") not in STATUTS_OK:
            ignores += 1
            continue

        m = montant(pr)
        if m <= 0:
            ignores += 1
            continue

        database.ajouter_paiement(
            date_paiement=date_paiement(pr),
            source="bridge",
            montant=m,
            client_nom=nom_payeur(pr),
            email="",
            reference_externe=pr.get("id", ""),
        )
        importes += 1

    database.regenerate_clients_summary()

    print("\n" + "=" * 55)
    print(f"Paiements Bridge importés : {importes}")
    print(f"Ignorés (rejetés/en attente/nuls) : {ignores}")
    print("=" * 55)


if __name__ == "__main__":
    main()
