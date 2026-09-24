import sqlite3
from datetime import datetime

DATABASE_URL = 'facturation.db'

def get_connection():
    """Obtenir une connexion à la base de données"""
    conn = sqlite3.connect(DATABASE_URL)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initialiser les tables de la base de données"""
    conn = get_connection()
    cursor = conn.cursor()
    
    # Table des paiements
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS paiements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date_paiement TEXT,
        source TEXT,
        montant REAL,
        client_nom TEXT,
        email TEXT,
        reference_externe TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Table des factures
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS factures (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        numero_facture TEXT UNIQUE,
        date_facture TEXT,
        client_nom TEXT,
        montant REAL,
        email TEXT,
        description TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Table des clients agrégés (vue)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS clients_summary (
        id INTEGER PRIMARY KEY,
        client_nom TEXT UNIQUE,
        total_paiements REAL,
        nb_transactions INTEGER,
        sources TEXT,
        dernier_paiement TEXT,
        email TEXT
    )
    ''')
    
    conn.commit()
    conn.close()

def ajouter_paiement(date_paiement, source, montant, client_nom='', email='', reference_externe=''):
    """Ajouter un paiement à la BD"""
    conn = get_connection()
    cursor = conn.cursor()
    
    # Normaliser le nom du client
    client_nom = client_nom.strip() if client_nom else 'Unknown'
    
    try:
        cursor.execute('''
        INSERT INTO paiements (date_paiement, source, montant, client_nom, email, reference_externe)
        VALUES (?, ?, ?, ?, ?, ?)
        ''', (date_paiement, source, montant, client_nom, email, reference_externe))
        
        conn.commit()
        return cursor.lastrowid
    except Exception as e:
        print(f"Error adding payment: {e}")
        return None
    finally:
        conn.close()

def obtenir_tous_paiements():
    """Obtenir tous les paiements"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM paiements ORDER BY date_paiement DESC')
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

# ============ CLIENTS SUMMARY (NEW) ============

def _nom_canonique(variantes):
    """Choisit le libellé le mieux écrit parmi les variantes d'un même nom.

    Priorité : casse mixte (Jean Dupont) > Minuscules > MAJUSCULES.
    À qualité égale, la variante la plus fréquente l'emporte.
    """
    def score(item):
        nom, freq = item
        if nom.isupper():
            qualite = 0
        elif nom.islower():
            qualite = 1
        else:
            qualite = 2
        return (qualite, freq)

    return max(variantes.items(), key=score)[0]


def regenerate_clients_summary():
    """Regénérer la vue clients avec agrégation (insensible à la casse).

    Un même client payant via plusieurs plateformes, ou dont le nom est
    saisi avec une casse différente selon la source, apparaît sur une
    seule ligne avec le total cumulé.
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute('DELETE FROM clients_summary')

    cursor.execute('''
    SELECT client_nom, montant, source, date_paiement, email
    FROM paiements
    ''')

    agrege = {}
    for nom, montant, source, date_paiement, email in cursor.fetchall():
        nom = (nom or 'Inconnu').strip()
        cle = nom.lower()

        entree = agrege.setdefault(cle, {
            'variantes': {},
            'total': 0.0,
            'nb': 0,
            'sources': set(),
            'dernier': '',
            'email': '',
        })

        entree['variantes'][nom] = entree['variantes'].get(nom, 0) + 1
        entree['total'] += montant or 0
        entree['nb'] += 1
        if source:
            entree['sources'].add(source)
        if date_paiement and date_paiement > entree['dernier']:
            entree['dernier'] = date_paiement
        if email and not entree['email']:
            entree['email'] = email

    for entree in agrege.values():
        cursor.execute('''
        INSERT INTO clients_summary
        (client_nom, total_paiements, nb_transactions, sources, dernier_paiement, email)
        VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            _nom_canonique(entree['variantes']),
            round(entree['total'], 2),
            entree['nb'],
            ','.join(sorted(entree['sources'])),
            entree['dernier'],
            entree['email'],
        ))

    conn.commit()
    conn.close()

    return len(agrege)

def obtenir_clients_summary(sort_by='total_paiements', order='DESC', limit=None,
                            recherche=None, source=None, min_total=None,
                            date_from=None, date_to=None):
    """Obtenir le résumé agrégé par client, avec recherche et filtres.

    recherche  : texte cherché dans le nom ou l'email
    source     : ne garder que les clients ayant payé via cette plateforme
    min_total  : total cumulé minimum
    date_from  : dernier paiement à partir de cette date
    date_to    : dernier paiement jusqu'à cette date
    """
    conn = get_connection()
    cursor = conn.cursor()

    allowed_sorts = ['total_paiements', 'nb_transactions', 'dernier_paiement', 'client_nom']
    sort_by = sort_by if sort_by in allowed_sorts else 'total_paiements'
    order = 'DESC' if str(order).upper() == 'DESC' else 'ASC'

    query = 'SELECT * FROM clients_summary WHERE 1=1'
    params = []

    if recherche:
        query += ' AND (LOWER(client_nom) LIKE ? OR LOWER(IFNULL(email, "")) LIKE ?)'
        motif = f'%{recherche.lower().strip()}%'
        params += [motif, motif]

    if source:
        query += ' AND sources LIKE ?'
        params.append(f'%{source}%')

    if min_total:
        query += ' AND total_paiements >= ?'
        params.append(float(min_total))

    if date_from:
        query += ' AND dernier_paiement >= ?'
        params.append(date_from)

    if date_to:
        query += ' AND dernier_paiement <= ?'
        params.append(date_to)

    query += f' ORDER BY {sort_by} {order}'
    if limit:
        query += ' LIMIT ?'
        params.append(int(limit))

    cursor.execute(query, params)
    clients = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return clients

def obtenir_paiements_client(client_nom):
    """Obtenir tous les paiements d'un client"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        '''SELECT * FROM paiements
           WHERE LOWER(TRIM(client_nom)) = LOWER(TRIM(?))
           ORDER BY date_paiement DESC''',
        (client_nom,)
    )
    paiements = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return paiements

def filtrer_paiements(date_from=None, date_to=None, source=None, min_amount=None, max_amount=None):
    """Filtrer les paiements par critères"""
    conn = get_connection()
    cursor = conn.cursor()
    
    query = 'SELECT * FROM paiements WHERE 1=1'
    params = []
    
    if date_from:
        query += ' AND date_paiement >= ?'
        params.append(date_from)
    
    if date_to:
        query += ' AND date_paiement <= ?'
        params.append(date_to)
    
    if source:
        query += ' AND source = ?'
        params.append(source)
    
    if min_amount:
        query += ' AND montant >= ?'
        params.append(min_amount)
    
    if max_amount:
        query += ' AND montant <= ?'
        params.append(max_amount)
    
    query += ' ORDER BY date_paiement DESC'
    
    cursor.execute(query, params)
    paiements = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return paiements

# ============ FACTURES ============

def ajouter_facture(numero_facture, date_facture, client_nom, montant, email='', description=''):
    """Ajouter une facture"""
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
        INSERT INTO factures (numero_facture, date_facture, client_nom, montant, email, description)
        VALUES (?, ?, ?, ?, ?, ?)
        ''', (numero_facture, date_facture, client_nom, montant, email, description))
        
        conn.commit()
        return cursor.lastrowid
    except Exception as e:
        print(f"Error adding invoice: {e}")
        return None
    finally:
        conn.close()

def obtenir_toutes_factures():
    """Obtenir toutes les factures"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM factures ORDER BY date_facture DESC')
    factures = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return factures

def obtenir_facture(facture_id):
    """Obtenir une facture par ID"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM factures WHERE id = ?', (facture_id,))
    facture = cursor.fetchone()
    conn.close()
    return dict(facture) if facture else None

def supprimer_facture(facture_id):
    """Supprimer une facture"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM factures WHERE id = ?', (facture_id,))
    conn.commit()
    conn.close()
