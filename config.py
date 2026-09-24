import os
from dotenv import load_dotenv

load_dotenv()

# Mot de passe d'accès admin (accès complet)
ADMIN_ACCESS_PASSWORD = os.getenv('ADMIN_ACCESS_PASSWORD', 'changeme123')

# Mot de passe d'accès commercial (recherche seule).
# Réutilise PAIEMENTS_ACCESS_PASSWORD, déjà défini sur Railway.
# SALES_ACCESS_PASSWORD reste accepté comme repli.
# Si les deux sont vides, aucun accès commercial n'est possible.
SALES_ACCESS_PASSWORD = (os.getenv('PAIEMENTS_ACCESS_PASSWORD', '')
                         or os.getenv('SALES_ACCESS_PASSWORD', ''))

# Clés API
STRIPE_API_KEY = os.getenv('STRIPE_API_KEY', '')
MOLLIE_API_KEY = os.getenv('MOLLIE_API_KEY', '')
GOCARDLESS_ACCESS_TOKEN = os.getenv('GOCARDLESS_ACCESS_TOKEN', '')
BRIDGE_API_KEY = os.getenv('BRIDGE_API_KEY', '')

# Base de données
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///facturation.db')

# Configuration Flask
SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
DEBUG = os.getenv('DEBUG', 'False').lower() == 'true'

# Configuration du serveur
HOST = os.getenv('HOST', '0.0.0.0')
PORT = int(os.getenv('PORT', 5000))
