#!/usr/bin/env python3
import sqlite3

conn = sqlite3.connect('facturation.db')
cursor = conn.cursor()

# Voir les patterns de noms manquants
cursor.execute('''
SELECT client_nom, COUNT(*) as cnt FROM paiements WHERE client_nom IS NULL OR client_nom = '' GROUP BY client_nom
''')

print("Missing or NULL client names:")
for row in cursor.fetchall():
    print(f"  '{row[0]}': {row[1]} payments")

# Voir les noms qui sont juste des IDs Stripe
cursor.execute('''
SELECT DISTINCT client_nom FROM paiements 
WHERE client_nom LIKE 'cus_%' 
LIMIT 20
''')

print("\nStipe customer IDs as names:")
for row in cursor.fetchall():
    print(f"  {row[0]}")

# Regrouper par source
cursor.execute('''
SELECT source, COUNT(*) as cnt, COUNT(DISTINCT client_nom) as clients 
FROM paiements GROUP BY source
''')

print("\nBy source:")
for row in cursor.fetchall():
    print(f"  {row[0]:12s}: {row[1]:4d} payments, {row[2]:3d} unique clients")

# TOP 10 by total (excluding unknowns)
print("\n\nTOP CLIENTS (excluding Stripe IDs/Unknown):")
cursor.execute('''
SELECT client_nom, COUNT(*) as cnt, SUM(montant) as total
FROM paiements
WHERE client_nom NOT LIKE 'cus_%' 
  AND client_nom IS NOT NULL 
  AND client_nom != ''
GROUP BY client_nom
ORDER BY total DESC
LIMIT 15
''')

for i, row in enumerate(cursor.fetchall(), 1):
    print(f"{i:2d}. {row[0][:45]:45s} | €{row[2]:10,.2f} | {row[1]:3d} payments")

conn.close()
