#!/usr/bin/env python3
"""
Import GoCardless payments from CSV export
"""
import csv
from datetime import datetime
import database

# Initialize database
database.init_db()

csv_file = '/mnt/user-data/uploads/payments_index-export-EX01M347NNQRB421MP346MFB43QB.csv'

with open(csv_file, 'r', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    count = 0
    skipped = 0
    
    for row in reader:
        try:
            # Extract data from CSV
            date_str = row['created_at'].split(' ')[0]  # Get date part only
            amount = float(row['amount'])
            client_name = f"{row['customers.given_name']} {row['customers.family_name']}".strip()
            email = row['customers.email']
            description = row['description']
            status = row['status']
            reference = row.get('links.payout', '') or 'GoCardless-' + row['created_at'].replace('-', '').replace(' ', '').replace(':', '')
            
            # Skip failed/chargebacked payments or ones without payout
            if status not in ['paid_out']:
                skipped += 1
                continue
            
            # Add to database
            paiement_id = database.ajouter_paiement(
                date_paiement=date_str,
                source='gocardless',
                montant=amount,
                client_nom=client_name,
                email=email,
                reference_externe=reference
            )
            
            count += 1
            if count % 50 == 0:
                print(f"✓ Imported {count} payments...")
        
        except Exception as e:
            print(f"Error importing row: {row.get('created_at', 'unknown')} - {e}")
            skipped += 1
            continue

print(f"\n✅ Successfully imported {count} GoCardless payments!")
print(f"⏭️  Skipped {skipped} non-paid-out transactions")
print(f"Database location: ./facturation.db")
