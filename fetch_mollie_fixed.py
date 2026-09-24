#!/usr/bin/env python3
import os
import requests
import database
from datetime import datetime

MOLLIE_KEY = os.getenv("MOLLIE_API_KEY", "")

print("📊 Fetching Mollie payments (fixed)...")
count = 0

try:
    url = "https://api.mollie.com/v2/payments"
    headers = {'Authorization': f'Bearer {MOLLIE_KEY}'}
    
    # Mollie doesn't use status param in query for list, it filters automatically
    params = {'limit': 250, 'include': 'details'}
    
    while url:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        print(f"Status: {response.status_code}")
        
        if response.status_code != 200:
            print(f"Error: {response.text}")
            break
        
        data = response.json()
        payments = data.get('_embedded', {}).get('payments', [])
        
        print(f"Found {len(payments)} payments in this page")
        
        for payment in payments:
            try:
                # Only paid payments
                if payment['status'] != 'paid':
                    continue
                
                date_str = payment['createdAt'].split('T')[0]
                amount = float(payment['amount']['value'])
                description = payment.get('description', 'Mollie Payment')[:50]
                
                database.ajouter_paiement(
                    date_paiement=date_str,
                    source='mollie',
                    montant=amount,
                    client_nom=description,
                    email='',
                    reference_externe=payment['id']
                )
                count += 1
            except Exception as e:
                print(f"Error: {e}")
        
        # Next page
        url = None
        links = data.get('_links', {})
        if 'next' in links:
            url = links['next'].get('href')
            params = {}

except Exception as e:
    print(f"Error: {e}")

print(f"✅ Mollie: {count} payments imported")
