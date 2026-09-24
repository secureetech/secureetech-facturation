#!/usr/bin/env python3
"""
Import GoCardless payments from CSV export
Only import PAID_OUT payments (successful payments only)
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
    skipped_failed = 0
    skipped_chargebacked = 0
    skipped_other = 0
    
    for row in reader:
        try:
            status = row['status'].strip().lower()
            
            # ONLY import paid_out payments (successful)
            if status != 'paid_out':
                if status == 'failed':
                    skipped_failed += 1
                elif status == 'charged_back':
                    skipped_chargebacked += 1
                else:
                    skipped_other += 1
                continue
            
            # Extract data from CSV
            date_str = row['created_at'].split(' ')[0]  # Get date part only
            amount = float(row['amount'])
            client_name = f"{row['customers.given_name']} {row['customers.family_name']}".strip()
            email = row['customers.email']
            description = row['description']
            reference = row.get('links.payout', '') or f"GC-{date_str.replace('-', '')}"
            
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
            if count % 20 == 0:
                print(f"✓ Imported {count} SUCCESSFUL payments...")
        
        except Exception as e:
            print(f"Error importing row: {row.get('created_at', 'unknown')} - {e}")
            skipped_other += 1
            continue

print(f"\n{'='*60}")
print(f"✅ Successfully imported {count} PAID_OUT payments!")
print(f"{'='*60}")
print(f"❌ Skipped {skipped_failed} FAILED payments")
print(f"❌ Skipped {skipped_chargebacked} CHARGEBACKED payments")
print(f"⏭️  Skipped {skipped_other} OTHER status payments")
print(f"{'='*60}")
print(f"Database location: ./facturation.db")
