import sqlite3
import os
from datetime import datetime
import json

DB_FILE = 'facturation.db'

def get_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initialiser la base de données avec les tables"""
    conn = get_connection()
    cursor = conn.cursor()
    
    # Table des paiements
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS paiements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date_paiement TEXT NOT NULL,
        source TEXT NOT NULL,
        montant REAL NOT NULL,
        client_nom TEXT,
        email TEXT,
        statut TEXT DEFAULT 'pending',
        reference_externe TEXT,
        metadonnees TEXT,
        synchronise INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Table des factures
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS factures (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        numero_facture TEXT UNIQUE NOT NULL,
        date_facture TEXT NOT NULL,
        client_nom TEXT NOT NULL,
        email TEXT,
        montant REAL NOT NULL,
        description TEXT,
        statut TEXT DEFAULT 'draft',
        paiement_id INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (paiement_id) REFERENCES paiements(id)
    )
    ''')
    
    # Table des vendeurs (sales team)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS vendeurs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nom TEXT NOT NULL,
        email TEXT,
        commission_pct REAL DEFAULT 10.0,
        actif INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    conn.commit()
    conn.close()

def ajouter_paiement(date_paiement, source, montant, client_nom='', email='', reference_externe=''):
    """Ajouter un paiement à la BD"""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
    INSERT INTO paiements (date_paiement, source, montant, client_nom, email, reference_externe)
    VALUES (?, ?, ?, ?, ?, ?)
    ''', (date_paiement, source, montant, client_nom, email, reference_externe))
    
    conn.commit()
    paiement_id = cursor.lastrowid
    conn.close()
    return paiement_id

def obtenir_tous_paiements():
    """Récupérer tous les paiements"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM paiements ORDER BY date_paiement DESC')
    paiements = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return paiements

def obtenir_paiements_par_source(source):
    """Récupérer les paiements d'une source spécifique"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM paiements WHERE source = ? ORDER BY date_paiement DESC', (source,))
    paiements = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return paiements

def supprimer_paiement(paiement_id):
    """Supprimer un paiement"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM paiements WHERE id = ?', (paiement_id,))
    conn.commit()
    conn.close()

def ajouter_facture(numero_facture, date_facture, client_nom, montant, email='', description=''):
    """Ajouter une facture"""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
    INSERT INTO factures (numero_facture, date_facture, client_nom, montant, email, description)
    VALUES (?, ?, ?, ?, ?, ?)
    ''', (numero_facture, date_facture, client_nom, montant, email, description))
    
    conn.commit()
    facture_id = cursor.lastrowid
    conn.close()
    return facture_id

def obtenir_toutes_factures():
    """Récupérer toutes les factures"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM factures ORDER BY date_facture DESC')
    factures = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return factures

def obtenir_facture(facture_id):
    """Récupérer une facture spécifique"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM factures WHERE id = ?', (facture_id,))
    facture = cursor.fetchone()
    conn.close()
    return dict(facture) if facture else None

# Initialiser la BD au chargement du module
if not os.path.exists(DB_FILE):
    init_db()
