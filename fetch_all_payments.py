#!/usr/bin/env python3
"""
Fetch payments from all sources:
- Stripe (2 accounts)
- Mollie
- Bridge
- GoCardless (already imported)

NOTE: API keys should be set via environment variables, not hardcoded!
"""
import os
import requests
from datetime import datetime
import database
import config

# Get API keys from environment (set in Railway)
STRIPE_KEY_1 = os.getenv('STRIPE_API_KEY', '')
STRIPE_KEY_2 = os.getenv('STRIPE_API_KEY_SECONDARY', '')
MOLLIE_KEY = os.getenv('MOLLIE_API_KEY', '')
BRIDGE_CLIENT_ID = os.getenv('BRIDGE_CLIENT_ID', '')
BRIDGE_CLIENT_SECRET = os.getenv('BRIDGE_CLIENT_SECRET', '')

if not STRIPE_KEY_1:
    print("ERROR: STRIPE_API_KEY not set in environment!")
    exit(1)

# Initialize database
database.init_db()

# ============ STRIPE ============
def fetch_stripe_payments(api_key, account_name="Stripe"):
    print(f"\n📊 Fetching {account_name} payments...")
    count = 0
    try:
        # Get charges (successful only)
        url = "https://api.stripe.com/v1/charges"
        params = {
            'limit': 100,
            'status': 'succeeded'  # Only successful charges
        }
        
        headers = {'Authorization': f'Bearer {api_key}'}
        
        while url:
            response = requests.get(url, params=params, headers=headers, timeout=10)
            if response.status_code != 200:
                print(f"❌ Stripe error: {response.status_code} - {response.text[:100]}")
                break
            
            data = response.json()
            charges = data.get('data', [])
            
            for charge in charges:
                try:
                    if charge['status'] != 'succeeded' or charge.get('refunded'):
                        continue  # Skip failed or refunded
                    
                    date_str = datetime.fromtimestamp(charge['created']).strftime('%Y-%m-%d')
                    amount = charge['amount'] / 100  # Convert from cents
                    
                    customer_name = charge.get('description', 'Unknown')
                    if charge.get('customer'):
                        customer_name = charge['customer'][:30]
                    
                    email = charge.get('receipt_email', '')
                    
                    database.ajouter_paiement(
                        date_paiement=date_str,
                        source='stripe',
                        montant=amount,
                        client_nom=customer_name,
                        email=email,
                        reference_externe=charge['id']
                    )
                    count += 1
                except Exception as e:
                    print(f"Error processing charge {charge.get('id')}: {e}")
                    continue
            
            # Check for next page
            url = None
            if data.get('has_more'):
                url = f"https://api.stripe.com/v1/charges"
                params['starting_after'] = charges[-1]['id'] if charges else None
    
    except Exception as e:
        print(f"❌ Error fetching from {account_name}: {e}")
    
    return count

# ============ MOLLIE ============
def fetch_mollie_payments():
    print(f"\n📊 Fetching Mollie payments...")
    count = 0
    try:
        url = "https://api.mollie.com/v2/payments"
        headers = {'Authorization': f'Bearer {MOLLIE_KEY}'}
        params = {'limit': 250}
        
        while url:
            response = requests.get(url, headers=headers, params=params, timeout=10)
            if response.status_code != 200:
                print(f"❌ Mollie error: {response.status_code}")
                break
            
            data = response.json()
            payments = data.get('_embedded', {}).get('payments', [])
            
            for payment in payments:
                try:
                    # Only paid payments
                    if payment['status'] != 'paid':
                        continue
                    
                    date_str = payment['createdAt'].split('T')[0]
                    amount = float(payment['amount']['value'])
                    customer_name = payment.get('description', 'Mollie Payment')[:50]
                    email = ''
                    
                    database.ajouter_paiement(
                        date_paiement=date_str,
                        source='mollie',
                        montant=amount,
                        client_nom=customer_name,
                        email=email,
                        reference_externe=payment['id']
                    )
                    count += 1
                except Exception as e:
                    continue
            
            # Check for next page
            url = None
            links = data.get('_links', {})
            if 'next' in links:
                url = links['next'].get('href')
                params = {}
    
    except Exception as e:
        print(f"❌ Error fetching from Mollie: {e}")
    
    return count

# ============ MAIN ============
if __name__ == '__main__':
    print("="*60)
    print("🔄 FETCHING PAYMENTS FROM ALL SOURCES")
    print("="*60)
    
    total = 0
    
    # Stripe Account 1
    if STRIPE_KEY_1:
        stripe1 = fetch_stripe_payments(STRIPE_KEY_1, "Stripe (Account 1)")
        print(f"✅ Stripe Account 1: {stripe1} payments")
        total += stripe1
    
    # Stripe Account 2
    if STRIPE_KEY_2:
        stripe2 = fetch_stripe_payments(STRIPE_KEY_2, "Stripe (Account 2)")
        print(f"✅ Stripe Account 2: {stripe2} payments")
        total += stripe2
    
    # Mollie
    if MOLLIE_KEY:
        mollie = fetch_mollie_payments()
        print(f"✅ Mollie: {mollie} payments")
        total += mollie
    
    print("\n" + "="*60)
    print(f"🎉 TOTAL NEW PAYMENTS: {total}")
    print("="*60)
    print(f"Note: GoCardless (85 payments) already imported separately")
    print(f"Total with GoCardless: {total + 85}")
