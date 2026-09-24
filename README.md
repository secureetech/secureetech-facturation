# 🎯 Secureetech Facturation - Nouvelle App

Application Flask complète de gestion de facturation et paiements pour Secureetech.

## 📋 Fonctionnalités

- ✅ **Connexion sécurisée** avec mot de passe admin
- ✅ **Gestion des paiements** (Stripe, Mollie, GoCardless, Bridge)
- ✅ **Création de factures** avec génération PDF
- ✅ **Dashboard** avec statistiques en temps réel
- ✅ **API RESTful** pour intégration

## 🚀 Déploiement sur Railway

### Option 1: Via GitHub (Recommandé)

1. Créer un repo GitHub: `secureetech/secureetech-facturation`
2. Pusher le code vers GitHub
3. Sur Railway:
   - Aller à Settings → Source → Connect Repo
   - Sélectionner `secureetech/secureetech-facturation`
   - Railway déploiera automatiquement chaque push

### Option 2: Direct depuis le code local

1. Utiliser Railway CLI:
```bash
npm install -g @railway/cli
railway login
cd /root/secureetech-facturation-new
railway up
```

## 🔧 Variables d'environnement (Railway)

Créer dans Railway → Variables:
- `ADMIN_ACCESS_PASSWORD`: Mot de passe admin
- `STRIPE_API_KEY`: Clé Stripe (optionnel)
- `PORT`: 8000 (défaut)
- `DEBUG`: false

## 📝 Structure

```
secureetech-facturation-new/
├── app.py              # Application Flask principale
├── config.py           # Configuration
├── database.py         # Base de données SQLite
├── requirements.txt    # Dépendances Python
├── Procfile           # Commande de lancement
├── Dockerfile         # Configuration Docker
└── templates/         # Templates HTML
    ├── base.html      # Template de base
    ├── index.html     # Dashboard
    ├── paiements.html # Gestion paiements
    └── factures.html  # Gestion factures
```

## 🔐 Accès

- URL: `https://secureetech-facturation-production.up.railway.app`
- Route login: `/connexion`
- Password: **Défini dans ADMIN_ACCESS_PASSWORD**

## 📞 Support

Contact: support@secureetech.com
