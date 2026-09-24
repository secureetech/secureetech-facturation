#!/usr/bin/env python3
"""
Importe les paiements Mollie encaissés.

Corrige le bug de pagination du premier script : Mollie renvoie
_links.next = null en fin de liste, ce qui faisait planter la boucle
après la première page.

Seuls les paiements au statut 'paid' sont retenus, et le montant
est net des remboursements éventuels.
"""
import os
import sys
import sqlite3
from collections import Counter

import requests
import database

CLE = os.getenv("MOLLIE_API_KEY", "")


def nom_client(p):
    details = p.get("details") or {}
    for candidat in (details.get("consumerName"),
                     details.get("cardHolder"),
                     (p.get("billingAddress") or {}).get("givenName"),
                     p.get("description")):
        if candidat and str(candidat).strip():
            return str(candidat).strip()[:60]
    return "Client Mollie inconnu"


def email_client(p):
    details = p.get("details") or {}
    return (details.get("consumerAccount") and "" or
            (p.get("billingEmail") or details.get("billingEmail") or ""))


def montant_net(p):
    brut = float((p.get("amount") or {}).get("value", 0))
    rembourse = float((p.get("amountRefunded") or {}).get("value", 0) or 0)
    return round(brut - rembourse, 2)


def main():
    if not CLE:
        print("MOLLIE_API_KEY absente de l'environnement.")
        sys.exit(1)

    database.init_db()

    conn = sqlite3.connect("facturation.db")
    cur = conn.cursor()
    cur.execute("DELETE FROM paiements WHERE source = 'mollie'")
    print(f"Anciennes lignes Mollie supprimées : {cur.rowcount}")
    conn.commit()
    conn.close()

    headers = {"Authorization": f"Bearer {CLE}"}
    url = "https://api.mollie.com/v2/payments?limit=250"

    tous = []
    pages = 0
    while url:
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code != 200:
            print(f"  ERREUR {r.status_code}: {r.text[:200]}")
            break

        data = r.json()
        lot = (data.get("_embedded") or {}).get("payments", [])
        tous.extend(lot)
        pages += 1
        print(f"  page {pages} : {len(lot)} paiements (cumul {len(tous)})")

        # Le correctif : _links.next peut être null
        suivant = (data.get("_links") or {}).get("next")
        url = suivant.get("href") if isinstance(suivant, dict) else None

    statuts = Counter(p.get("status", "?") for p in tous)
    print(f"\nRépartition des statuts : {dict(statuts)}")

    importes = ignores = 0
    for p in tous:
        if p.get("status") != "paid":
            ignores += 1
            continue

        m = montant_net(p)
        if m <= 0:
            ignores += 1
            continue

        database.ajouter_paiement(
            date_paiement=(p.get("paidAt") or p.get("createdAt", ""))[:10],
            source="mollie",
            montant=m,
            client_nom=nom_client(p),
            email=email_client(p),
            reference_externe=p.get("id", ""),
        )
        importes += 1

    database.regenerate_clients_summary()

    print("\n" + "=" * 55)
    print(f"Paiements Mollie importés : {importes}")
    print(f"Ignorés (non encaissés / remboursés) : {ignores}")
    print("=" * 55)


if __name__ == "__main__":
    main()
