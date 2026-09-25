from flask import Flask, render_template, request, jsonify, redirect, url_for, session, send_file
from functools import wraps
from datetime import datetime
import os
import config
import database
import signnow
import formules
import facture_pdf
from reportlab.lib.pagesizes import letter, A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
import io
import secrets
import hashlib

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

# Préparation de la base au démarrage.
# Encapsulé : un incident ici ne doit jamais empêcher le serveur de
# démarrer, sinon la plateforme ne voit qu'un conteneur qui ne répond pas.
try:
    database.init_db()
    database.init_contrats()
    database.init_factures_formules()
    database.regenerate_clients_summary()
except Exception as erreur_demarrage:  # pragma: no cover
    import traceback
    print("ERREUR au démarrage lors de la préparation de la base :", erreur_demarrage, flush=True)
    traceback.print_exc()


@app.route('/health')
def health():
    """Sonde de diagnostic : répond même si la base est en défaut.

    N'expose aucun mot de passe : seulement s'ils sont configurés.
    """
    infos = {
        'acces_admin_configure': bool(config.ADMIN_ACCESS_PASSWORD),
        'acces_commercial_configure': bool(config.SALES_ACCESS_PASSWORD),
        'memes_mots_de_passe': bool(config.SALES_ACCESS_PASSWORD)
                               and config.ADMIN_ACCESS_PASSWORD == config.SALES_ACCESS_PASSWORD,
        'dropbox_sign_configure': bool(os.environ.get('DROPBOX_SIGN_API_KEY', '')),
        'signnow_configure': signnow.configure(),
        'api_facture_configure': bool(os.environ.get('FACTURATION_API_TOKEN', '')
                                      or os.environ.get('LINK_HUB_TOKEN', '')),
        'recherches_par_jour': RECHERCHES_PAR_JOUR,
    }
    try:
        infos['contrats'] = database.compter_contrats()
        infos['factures'] = database.compter_factures()
    except Exception:
        pass
    try:
        infos['paiements'] = len(database.obtenir_tous_paiements())
        infos['statut'] = 'ok'
    except Exception as e:
        infos['statut'] = 'base en erreur'
        infos['detail'] = str(e)
    return infos, 200

# ============ AUTHENTIFICATION ET RÔLES ============
# Deux rôles :
#   'admin'  -> Karim, accès complet (tableau de bord, listes, factures)
#   'sales'  -> vendeurs, accès à la recherche uniquement

def login_required(f):
    """Exige une session ouverte, quel que soit le rôle."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('role'):
            return redirect(url_for('connexion'))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    """Réserve la page à l'administrateur."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        role = session.get('role')
        if not role:
            return redirect(url_for('connexion'))
        if role != 'admin':
            # Un vendeur est renvoyé vers son seul outil : la recherche
            return redirect(url_for('recherche'))
        return f(*args, **kwargs)
    return decorated_function


@app.context_processor
def injecter_role():
    """Rend le rôle disponible dans tous les gabarits (menu adapté)."""
    return {'role': session.get('role'), 'est_admin': session.get('role') == 'admin'}


# ============ ROUTES DE CONNEXION ============
@app.route('/connexion', methods=['GET', 'POST'])
def connexion():
    if request.method == 'POST':
        password = request.form.get('password', '')

        if password and password == config.ADMIN_ACCESS_PASSWORD:
            session.clear()
            session['role'] = 'admin'
            return redirect(url_for('index'))

        if password and config.SALES_ACCESS_PASSWORD and password == config.SALES_ACCESS_PASSWORD:
            session.clear()
            session['role'] = 'sales'
            return redirect(url_for('recherche'))

        return render_template('connexion.html', error='Mot de passe incorrect')

    return render_template('connexion.html')


@app.route('/deconnexion')
def deconnexion():
    session.clear()
    return redirect(url_for('connexion'))


# Nombre minimum de caractères exigé pour lancer une recherche.
# Empêche un vendeur de balayer le fichier client avec une lettre ou deux.
RECHERCHE_MIN = 5

# Nombre de recherches autorisées par jour pour un accès commercial.
# L'administration n'est pas limitée.
RECHERCHES_PAR_JOUR = int(os.environ.get('RECHERCHES_PAR_JOUR', '10'))


def _cle_commande(email, nom, formule):
    """Identifie une commande, pour ne pas la facturer deux fois.

    Un lien de paiement découpé en plusieurs tranches, ou simplement
    rechargé par le vendeur, retombe sur la même clé et donc sur la
    même facture. La clé est bornée à la journée : un renouvellement
    plus tard donnera bien une nouvelle facture.
    """
    identite = (email or nom or '').strip().lower()
    empreinte = f"{identite}|{(formule or '').strip().lower()}|{datetime.now():%Y-%m-%d}"
    return hashlib.sha256(empreinte.encode()).hexdigest()[:32]


def _emettre_licence_optipc(email, nom, telephone, duree_mois, notes=''):
    """Emet une vraie licence OptiPC (ST-XXXX-XXXX-XXXX) via l'API licences.

    La licence apparait dans l'historique du License Desk. Non bloquant :
    en cas d'echec, renvoie une chaine vide et la facturation continue.
    """
    jeton = os.environ.get('OPTIPC_ADMIN_TOKEN', '')
    base_api = os.environ.get('OPTIPC_API_BASE', 'https://api.secureetech.com')
    if not jeton:
        return ''
    try:
        import json as json_mod
        import urllib.request as urlreq
        corps = json_mod.dumps({
            'email': email or '',
            'name': nom or '',
            'phone': telephone or '',
            'months': int(duree_mois or 12),
            'seats': 1,
            'notes': notes or '',
        }).encode('utf-8')
        requete = urlreq.Request(
            base_api + '/v1/admin/issue',
            data=corps,
            headers={'x-admin-token': jeton,
                     'Content-Type': 'application/json',
                     'User-Agent': 'Secureetech-Facturation/1.0'},
            method='POST')
        reponse = urlreq.urlopen(requete, timeout=20)
        donnees_api = json_mod.loads(reponse.read().decode('utf-8'))
        cle_emise = donnees_api.get('key', '') or ''
        if cle_emise:
            print(f"OptiPC : licence emise {cle_emise} pour {email}")
        return cle_emise
    except Exception as exc:
        print(f"OptiPC issue (non bloquant) : {exc}")
        return ''


def _garantir_colonne_licence():
    """Ajoute la colonne licence_optipc a la table factures si absente."""
    try:
        connexion = database.get_connection()
        connexion.execute("ALTER TABLE factures ADD COLUMN licence_optipc TEXT")
        connexion.commit()
        connexion.close()
    except Exception:
        pass
    try:
        connexion = database.get_connection()
        connexion.execute("ALTER TABLE factures ADD COLUMN client_telephone TEXT")
        connexion.commit()
        connexion.close()
    except Exception:
        pass


_garantir_colonne_licence()


def _stocker_licence(facture_id, cle):
    try:
        connexion = database.get_connection()
        connexion.execute("UPDATE factures SET licence_optipc = ? WHERE id = ?",
                          (cle, facture_id))
        connexion.commit()
        connexion.close()
    except Exception as exc:
        print(f"Stockage licence (non bloquant) : {exc}")


def _licence_de_facture(facture, telephone=''):
    """Licence de la facture ; l'emet via l'API OptiPC si pas encore emise."""
    try:
        cle = (facture.get('licence_optipc') or '').strip()
    except Exception:
        cle = ''
    if cle:
        return cle
    cle = _emettre_licence_optipc(
        facture.get('email') or '',
        facture.get('client_nom') or '',
        telephone,
        facture.get('duree') or 12,
        f"Basket - facture {facture.get('numero_facture') or facture.get('id')}")
    if cle:
        _stocker_licence(facture['id'], cle)
    return cle


def _ajouter_contact_ringover(nom, email, telephone=''):
    """Ajoute le client aux contacts Ringover (best effort, jamais bloquant)."""
    cle_api = os.environ.get('RINGOVER_API_KEY', '')
    if not cle_api or not (email or telephone):
        return
    try:
        import json as json_mod
        import urllib.request as urlreq
        morceaux = (nom or '').strip().split(' ', 1)
        contact = {
            'firstname': morceaux[0] if morceaux and morceaux[0] else (nom or 'Client'),
            'lastname': morceaux[1] if len(morceaux) > 1 else '',
        }
        if telephone:
            chiffres = ''.join(c for c in str(telephone) if c.isdigit())
            if chiffres:
                contact['numbers'] = [{'number': int(chiffres)}]
        if email:
            contact['emails'] = [{'email': email}]
        corps = json_mod.dumps({'contacts': [contact]}).encode('utf-8')
        requete = urlreq.Request(
            'https://public-api.ringover.com/v2/contacts',
            data=corps,
            headers={'Authorization': cle_api,
                     'Content-Type': 'application/json',
                     'User-Agent': 'Secureetech-Facturation/1.0'},
            method='POST')
        urlreq.urlopen(requete, timeout=10)
        print(f"Ringover : contact ajoute ({email or telephone})")
    except Exception as exc:
        print(f"Ringover (non bloquant) : {exc}")


def _numero_facture():
    """Numéro séquentiel, au format ELITE-2026-0048."""
    prefixe = os.environ.get('FACTURE_PREFIXE', 'ELITE')
    depart = int(os.environ.get('FACTURE_DERNIER_NUMERO', '47'))
    return database.prochain_numero(prefixe, depart)


def _quota_recherche():
    """Compte les recherches du jour et dit s'il en reste.

    Retourne (utilisees, restantes, autorise).
    L'administration n'est jamais limitée.
    """
    if session.get('role') == 'admin':
        return 0, None, True

    aujourdhui = datetime.now().strftime('%Y-%m-%d')
    compteur = session.get('recherches') or {}
    if compteur.get('jour') != aujourdhui:
        compteur = {'jour': aujourdhui, 'nombre': 0}

    utilisees = compteur.get('nombre', 0)
    return utilisees, max(0, RECHERCHES_PAR_JOUR - utilisees), utilisees < RECHERCHES_PAR_JOUR


def _consommer_recherche():
    """Incrémente le compteur du jour pour un accès commercial."""
    if session.get('role') == 'admin':
        return
    aujourdhui = datetime.now().strftime('%Y-%m-%d')
    compteur = session.get('recherches') or {}
    if compteur.get('jour') != aujourdhui:
        compteur = {'jour': aujourdhui, 'nombre': 0}
    compteur['nombre'] = compteur.get('nombre', 0) + 1
    session['recherches'] = compteur
    session.modified = True


# ============ RECHERCHE (accessible aux vendeurs) ============
@app.route('/recherche')
@login_required
def recherche():
    """Recherche client. Rien ne s'affiche tant qu'aucun terme n'est saisi."""
    terme = request.args.get('q', '').strip()
    utilisees, restantes, autorise = _quota_recherche()

    resultats = []
    quota_atteint = False

    if len(terme) >= RECHERCHE_MIN:
        if autorise:
            resultats = database.obtenir_clients_summary(recherche=terme)[:50]
            _consommer_recherche()
            utilisees, restantes, autorise = _quota_recherche()
        else:
            quota_atteint = True

    details = {}
    contrats = {}
    factures_ext = {}
    for client in resultats:
        nom = client['client_nom']
        details[nom] = database.obtenir_paiements_client(nom)
        contrats[nom] = database.obtenir_contrats_client(nom, client.get('email') or '')
        factures_ext[nom] = database.obtenir_factures_client(nom, client.get('email') or '')

    return render_template('recherche.html',
                           factures_ext=factures_ext,
                           terme=terme,
                           resultats=resultats,
                           details=details,
                           contrats=contrats,
                           minimum=RECHERCHE_MIN,
                           quota_atteint=quota_atteint,
                           quota_restantes=restantes,
                           quota_total=RECHERCHES_PAR_JOUR,
                           trop_court=(0 < len(terme) < RECHERCHE_MIN))

# ============ DASHBOARD PRINCIPAL ============
@app.route('/')
@admin_required
def index():
    paiements = database.obtenir_tous_paiements()
    factures = database.obtenir_toutes_factures()
    clients = database.obtenir_clients_summary()

    total_paiements = sum(p['montant'] for p in paiements)
    total_factures = sum(f['montant'] for f in factures)

    # Répartition par plateforme
    repartition = {}
    for p in paiements:
        bloc = repartition.setdefault(p['source'], {'source': p['source'], 'nb': 0,
                                                    'total': 0.0, 'noms': set()})
        bloc['nb'] += 1
        bloc['total'] += p['montant'] or 0
        bloc['noms'].add((p['client_nom'] or '').lower())

    # Répartition par entité juridique
    par_source = sorted(
        ({'source': b['source'], 'nb': b['nb'], 'total': b['total'],
          'clients': len(b['noms'])} for b in repartition.values()),
        key=lambda b: b['total'], reverse=True)

    return render_template('index.html',
                         total_paiements=total_paiements,
                         total_factures=total_factures,
                         nb_clients=len(clients),
                         nb_paiements=len(paiements),
                         par_source=par_source,
                         contrats=database.compter_contrats(),
                         nb_factures_ext=database.compter_factures(),
                         signnow_pret=signnow.configure(),
                         clients_top=clients[:10])

# ============ PAGE CLIENTS (AGRÉGÉ) ============
@app.route('/clients')
@admin_required
def clients():
    sort_by = request.args.get('sort', 'total_paiements')
    order = request.args.get('order', 'DESC')
    recherche = request.args.get('q', '').strip()
    source = request.args.get('source', '').strip()
    min_total = request.args.get('min_total', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()

    clients_list = database.obtenir_clients_summary(
        sort_by=sort_by,
        order=order,
        recherche=recherche or None,
        source=source or None,
        min_total=min_total or None,
        date_from=date_from or None,
        date_to=date_to or None,
    )

    total_affiche = sum(c['total_paiements'] for c in clients_list)
    nb_paiements = sum(c['nb_transactions'] for c in clients_list)

    toutes_sources = sorted({p['source'] for p in database.obtenir_tous_paiements()})

    return render_template('clients.html',
                         clients=clients_list,
                         sort_by=sort_by,
                         order=order,
                         recherche=recherche,
                         source=source,
                         min_total=min_total,
                         date_from=date_from,
                         date_to=date_to,
                         sources=toutes_sources,
                         total_affiche=total_affiche,
                         nb_paiements=nb_paiements)

# ============ DÉTAILS CLIENT ============
@app.route('/client/<client_nom>')
@admin_required
def detail_client(client_nom):
    paiements = database.obtenir_paiements_client(client_nom)

    total = sum(p['montant'] for p in paiements)
    sources = set(p['source'] for p in paiements)
    email = next((p['email'] for p in paiements if p.get('email')), '')
    contrats = database.obtenir_contrats_client(client_nom, email)
    factures_ext = database.obtenir_factures_client(client_nom, email)

    return render_template('client_detail.html',
                         factures_ext=factures_ext,
                         client_nom=client_nom,
                         paiements=paiements,
                         total=total,
                         sources=sources,
                         email=email,
                         contrats=contrats,
                         nb_paiements=len(paiements))

# ============ PAGE PAIEMENTS (FILTRAGE) ============
@app.route('/paiements')
@admin_required
def paiements():
    # Récupérer les filtres
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    source = request.args.get('source', '')
    entite = request.args.get('entite', '')
    min_amount = request.args.get('min_amount', '')
    max_amount = request.args.get('max_amount', '')
    
    # Appliquer filtres
    paiements_filtres = database.filtrer_paiements(
        date_from=date_from if date_from else None,
        date_to=date_to if date_to else None,
        source=source if source else None,
        entite=entite if entite else None,
        min_amount=float(min_amount) if min_amount else None,
        max_amount=float(max_amount) if max_amount else None
    )
    
    # Sources disponibles
    all_paiements = database.obtenir_tous_paiements()
    sources_list = sorted(list(set(p['source'] for p in all_paiements)))
    entites_list = sorted({(p.get('entite') or 'à confirmer') for p in all_paiements})
    
    return render_template('paiements.html',
                         paiements=paiements_filtres,
                         sources=sources_list,
                         entites=entites_list,
                         entite=entite,
                         date_from=date_from,
                         date_to=date_to,
                         source=source,
                         min_amount=min_amount,
                         max_amount=max_amount)

# ============ PAGE FACTURES ============
@app.route('/factures', methods=['GET', 'POST'])
@admin_required
def factures():
    """Génération de factures à partir du catalogue de formules."""
    message = None
    erreur = None

    if request.method == 'POST':
        client_nom = (request.form.get('client_nom') or '').strip()
        email = (request.form.get('email') or '').strip()
        adresse = (request.form.get('client_adresse') or '').strip()
        choix = request.form.get('formule') or ''
        prix_libre = (request.form.get('montant_ht') or '').strip()
        date_facture = request.form.get('date_facture') or datetime.now().strftime('%Y-%m-%d')

        if not client_nom:
            erreur = "Le nom du client est obligatoire."
        elif not choix:
            erreur = "Sélectionnez une formule."
        else:
            nom_formule, _, duree_txt = choix.partition('|')
            duree = int(duree_txt or 0)
            calcul = formules.montants(
                nom_formule, duree,
                montant_ht=float(prix_libre) if prix_libre else None)

            if not calcul:
                erreur = "Cette combinaison formule / durée n'existe pas."
            else:
                numero = (request.form.get('numero_facture') or '').strip() or _numero_facture()
                database.ajouter_facture(
                    numero_facture=numero,
                    date_facture=date_facture,
                    client_nom=client_nom,
                    montant=calcul['ttc'],
                    email=email,
                    description=formules.description(nom_formule, duree),
                    formule=nom_formule,
                    duree=duree,
                    montant_ht=calcul['ht'],
                    tva=calcul['tva'],
                    client_adresse=adresse)
                message = (f"Facture {numero} créée : {formules.description(nom_formule, duree)} — "
                           f"{calcul['ht']:.2f} € HT + {calcul['tva']:.2f} € de TVA "
                           f"= {calcul['ttc']:.2f} € TTC.")

    return render_template('factures.html',
                           factures=database.obtenir_toutes_factures(),
                           catalogue=formules.catalogue(),
                           taux_tva=int(formules.TVA * 100),
                           aujourdhui=datetime.now().strftime('%Y-%m-%d'),
                           message=message,
                           erreur=erreur)

# ============ API ENDPOINTS ============
@app.route('/api/factures/ajouter', methods=['POST'])
@admin_required
def api_ajouter_facture():
    data = request.json
    try:
        numero = data.get('numero_facture', f'FAC-{datetime.now().strftime("%Y%m%d%H%M%S")}')
        facture_id = database.ajouter_facture(
            numero_facture=numero,
            date_facture=data.get('date_facture', datetime.now().strftime('%Y-%m-%d')),
            client_nom=data.get('client_nom'),
            montant=float(data.get('montant')),
            email=data.get('email', ''),
            description=data.get('description', '')
        )
        return jsonify({'success': True, 'id': facture_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/factures/<int:facture_id>/pdf')
@admin_required
def api_facture_pdf(facture_id):
    """PDF de la facture, aux couleurs SecureeTech."""
    facture = database.obtenir_facture(facture_id)
    if not facture:
        return jsonify({'error': 'Facture non trouvée'}), 404

    contenu = facture_pdf.construire(facture, taux_tva=formules.TVA)
    numero = (facture.get('numero_facture') or str(facture_id)).replace('/', '-')

    return contenu, 200, {
        'Content-Type': 'application/pdf',
        'Content-Disposition': f'inline; filename=facture_{numero}.pdf',
    }


# ============ CONTRATS ET FACTURES ============
@app.route('/contrat/<path:request_id>')
@login_required
def contrat_pdf(request_id):
    """Télécharge le contrat signé.

    Deux origines possibles : SignNow (PDF archivé sur le serveur) ou
    Dropbox Sign (récupéré par API). Ouvert aux vendeurs.
    """
    # SignNow : le PDF signé est archivé avec l'application.
    # On passe par la base pour retrouver le nom exact du fichier.
    if request_id.startswith('signnow:'):
        conn = database.get_connection()
        ligne = conn.execute(
            'SELECT fichier_local FROM contrats WHERE signature_request_id = ?',
            (request_id,)).fetchone()
        conn.close()
        if not ligne or not ligne['fichier_local']:
            return "Contrat introuvable.", 404

        nom_fichier = os.path.basename(ligne['fichier_local'])
        chemin = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'contrats_signes', nom_fichier)
        if not os.path.isfile(chemin):
            return "Fichier du contrat absent du serveur.", 404
        return send_file(chemin, mimetype='application/pdf',
                         as_attachment=True, download_name=nom_fichier)

    # SignNow par API : le PDF est récupéré à la demande
    if request_id.startswith('signnow_api:'):
        document_id = request_id.split(':', 1)[1]
        try:
            contenu = signnow.telecharger(document_id)
        except signnow.SignNowIndisponible as erreur:
            return str(erreur), 503
        return contenu, 200, {
            'Content-Type': 'application/pdf',
            'Content-Disposition': f'attachment; filename=contrat_{document_id[:10]}.pdf',
        }

    cle = os.environ.get('DROPBOX_SIGN_API_KEY', '')
    if not cle:
        return "Clé Dropbox Sign non configurée sur le serveur.", 503

    import requests as _rq
    reponse = _rq.get(
        f'https://api.hellosign.com/v3/signature_request/files/{request_id}',
        auth=(cle, ''), params={'file_type': 'pdf'}, timeout=30)

    if reponse.status_code != 200:
        return f"Contrat indisponible (code {reponse.status_code}).", 502

    return reponse.content, 200, {
        'Content-Type': 'application/pdf',
        'Content-Disposition': f'attachment; filename=contrat_{request_id[:10]}.pdf',
    }


@app.route('/client/<client_nom>/facture.pdf')
@admin_required
def facture_client(client_nom):
    """Facture récapitulative d'un client, générée depuis ses paiements."""
    paiements = database.obtenir_paiements_client(client_nom)
    if not paiements:
        return "Aucun paiement pour ce client.", 404

    email = next((p['email'] for p in paiements if p.get('email')), '')
    total = sum(p['montant'] for p in paiements)

    tampon = io.BytesIO()
    doc = SimpleDocTemplate(tampon, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    bleu = colors.HexColor('#0967a0')

    titre = ParagraphStyle('Titre', parent=styles['Heading1'], fontSize=20,
                           textColor=bleu, spaceAfter=4)
    normal = styles['Normal']

    elements = [
        Paragraph('SecureeTech', titre),
        Paragraph('Récapitulatif des paiements', normal),
        Spacer(1, 0.7*cm),
        Paragraph(f'<b>Client :</b> {client_nom}', normal),
    ]
    if email:
        elements.append(Paragraph(f'<b>Email :</b> {email}', normal))
    elements += [
        Paragraph(f"<b>Édité le :</b> {datetime.now().strftime('%d/%m/%Y')}", normal),
        Spacer(1, 0.7*cm),
    ]

    lignes = [['Date', 'Plateforme', 'Référence', 'Montant']]
    for p in sorted(paiements, key=lambda x: x['date_paiement']):
        lignes.append([
            p['date_paiement'],
            p['source'],
            (p['reference_externe'] or '')[:24],
            f"{p['montant']:.2f} EUR",
        ])
    lignes.append(['', '', 'TOTAL', f'{total:.2f} EUR'])

    tableau = Table(lignes, colWidths=[2.6*cm, 3*cm, 6.4*cm, 3.5*cm])
    tableau.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), bleu),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#eef2f5')),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('ALIGN', (3, 0), (3, -1), 'RIGHT'),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#dde5ea')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    elements.append(tableau)

    doc.build(elements)
    tampon.seek(0)

    nom_fichier = ''.join(ch if ch.isalnum() else '_' for ch in client_nom)[:40]
    return tampon.getvalue(), 200, {
        'Content-Type': 'application/pdf',
        'Content-Disposition': f'attachment; filename=recapitulatif_{nom_fichier}.pdf',
    }



@app.route('/sync/signnow', methods=['GET', 'POST'])
@admin_required
def sync_signnow():
    """Récupère les nouveaux contrats depuis SignNow."""
    if not signnow.configure():
        return render_template('sync.html', erreur=(
            "Identifiants SignNow absents du serveur. Vérifiez les variables "
            "SIGNNOW_CLIENT_ID, SIGNNOW_CLIENT_SECRET, "
            "SIGNNOW_INITIAL_ACCESS_TOKEN et SIGNNOW_INITIAL_REFRESH_TOKEN."))

    try:
        resultat = signnow.synchroniser()
    except signnow.SignNowIndisponible as erreur:
        return render_template('sync.html', erreur=str(erreur))
    except Exception as erreur:  # pragma: no cover
        return render_template('sync.html', erreur=f"Erreur inattendue : {erreur}")

    return render_template('sync.html', resultat=resultat)



# ============ API POUR LE GÉNÉRATEUR DE LIENS (secureetech.com/basket) ============
@app.route('/api/liens', methods=['POST'])
@app.route('/api/facture', methods=['POST'])
def api_creer_facture():
    """Crée une facture depuis le générateur de liens de paiement.

    Appelé par le plugin WordPress au moment où le vendeur génère le lien.
    Authentification par jeton partagé, dans l'en-tête X-Api-Token.

    Champs attendus (ceux du formulaire panier) :
      first_name, last_name, client_email, description (la formule),
      months (durée), amount (montant HT ; facultatif si la formule
      et la durée suffisent à le déduire de la grille).
    """
    jeton_attendu = (os.environ.get('FACTURATION_API_TOKEN', '')
                     or os.environ.get('LINK_HUB_TOKEN', ''))
    if not jeton_attendu:
        return jsonify({'ok': False, 'erreur': "Aucun jeton d'API configuré sur le serveur."}), 503

    donnees = request.get_json(silent=True) or request.form.to_dict()

    # L'extrait WordPress transmet le jeton dans le corps ; l'en-tête
    # reste accepté pour un appel direct.
    jeton_recu = (request.headers.get('X-Api-Token', '')
                  or (donnees.get('token') or ''))
    if jeton_recu != jeton_attendu:
        return jsonify({'ok': False, 'erreur': 'Jeton invalide.'}), 401

    # L'extrait « Notif dashboard facturation » envoie des noms français.
    prenom = (donnees.get('prenom') or donnees.get('first_name') or '').strip()
    patronyme = (donnees.get('nom') or donnees.get('last_name') or '').strip()
    nom = ' '.join(x for x in [prenom, patronyme] if x).strip()
    nom = nom or (donnees.get('client_nom') or '').strip()

    email = (donnees.get('email') or donnees.get('client_email') or '').strip()
    adresse = (donnees.get('adresse') or donnees.get('client_address')
               or donnees.get('client_adresse') or '').strip()
    formule = (donnees.get('formule') or donnees.get('description') or '').strip()
    plateforme = (donnees.get('plateforme') or donnees.get('platform') or '').strip()
    lien = (donnees.get('lien') or donnees.get('link') or '').strip()
    numero_tva = (donnees.get('tva') or donnees.get('client_vat') or '').strip()

    try:
        duree = int(float(donnees.get('months') or donnees.get('duree') or 0))
    except (TypeError, ValueError):
        duree = 0

    # Le panier transmet « amount » en TTC (valeur du menu stph_price_picker).
    # « montant_ht » / « amount_ht » restent acceptés pour un appel explicite.
    def _nombre(valeur):
        try:
            return float(valeur) if valeur not in (None, '') else None
        except (TypeError, ValueError):
            return None

    montant_ht = _nombre(donnees.get('montant_ht') or donnees.get('amount_ht'))
    montant_ttc = _nombre(donnees.get('montant') or donnees.get('amount_ttc')
                          or donnees.get('amount'))

    # À partir du TTC on retrouve la durée et le prix hors taxes.
    if montant_ht is None and montant_ttc is not None:
        deduit = formules.depuis_ttc(formule, montant_ttc)
        if deduit:
            montant_ht = deduit['ht']
            if not duree:
                duree = deduit['duree']

    if not nom:
        return jsonify({'ok': False, 'erreur': 'Nom du client manquant.'}), 400
    if not formule:
        return jsonify({'ok': False, 'erreur': 'Formule manquante.'}), 400

    calcul = formules.montants(formule, duree, montant_ht=montant_ht)
    if not calcul:
        return jsonify({'ok': False,
                        'erreur': f"Impossible de déterminer le prix pour « {formule} »"
                                  f"{f' sur {duree} mois' if duree else ''}. "
                                  f"Transmettez « amount » (montant HT)."}), 400

    # Une commande = une facture, même si le paiement est découpé en
    # plusieurs liens ou si le vendeur régénère le lien.
    cle = (donnees.get('cle') or donnees.get('order_id') or '').strip() \
        or _cle_commande(email, nom, formule)

    existante = database.facture_par_cle(cle)
    if existante:
        # Une tranche plus élevée que celle enregistrée correspond au total.
        if calcul['ttc'] > (existante.get('montant') or 0) + 0.01:
            database.majorer_facture(existante['id'], calcul['ttc'], calcul['ht'],
                                     calcul['tva'], duree,
                                     formules.description(formule, duree))
            existante = database.obtenir_facture(existante['id'])

        return jsonify({
            'ok': True,
            'deja_existante': True,
            'licence_optipc': _licence_de_facture(existante),
            'licence_duree_mois': existante.get('duree') or 12,
            'facture_id': existante['id'],
            'numero': existante['numero_facture'],
            'client': existante['client_nom'],
            'formule': existante['formule'],
            'duree': existante['duree'],
            'montant_ht': existante['montant_ht'],
            'tva': existante['tva'],
            'montant_ttc': existante['montant'],
            'pdf': url_for('api_facture_pdf', facture_id=existante['id'], _external=True),
        }), 200

    numero = (donnees.get('numero') or '').strip() or _numero_facture()

    facture_id = database.ajouter_facture(
        numero_facture=numero,
        date_facture=datetime.now().strftime('%Y-%m-%d'),
        client_nom=nom,
        montant=calcul['ttc'],
        email=email,
        description=formules.description(formule, duree),
        formule=formule,
        duree=duree,
        montant_ht=calcul['ht'],
        tva=calcul['tva'],
        cle_commande=cle,
        client_adresse=adresse,
        plateforme=plateforme,
        lien_paiement=lien,
        numero_tva=numero_tva)

    if not facture_id:
        return jsonify({'ok': False,
                        'erreur': f"Le numéro de facture « {numero} » existe déjà."}), 409

    _ajouter_contact_ringover(nom, email,
                               (donnees.get('telephone') or donnees.get('phone') or '').strip())

    return jsonify({
        'ok': True,
        'deja_existante': False,
        'licence_optipc': _licence_de_facture(database.obtenir_facture(facture_id) or {}),
        'licence_duree_mois': duree or 12,
        'facture_id': facture_id,
        'numero': numero,
        'client': nom,
        'formule': formule,
        'duree': duree,
        'montant_ht': calcul['ht'],
        'tva': calcul['tva'],
        'montant_ttc': calcul['ttc'],
        'pdf': url_for('api_facture_pdf', facture_id=facture_id, _external=True),
    }), 201


# ============ ERROR HANDLERS ============
@app.template_filter('licence_optipc')
def _filtre_licence_optipc(facture):
    """Affiche la cle de licence OptiPC d'une facture dans les gabarits."""
    try:
        return (facture.get('licence_optipc') or '').strip()
    except Exception:
        return ''


def _paiement_recu(email_client, nom_client):
    """Vrai si au moins un paiement enregistre correspond au client."""
    try:
        paiements_tous = database.obtenir_tous_paiements()
    except Exception as exc:
        print(f"Verification paiement : {exc}")
        return False
    email_bas = (email_client or '').strip().lower()
    nom_bas = (nom_client or '').strip().lower()
    for p in paiements_tous:
        if email_bas and (p.get('email') or '').strip().lower() == email_bas:
            return True
        if nom_bas and (p.get('client_nom') or '').strip().lower() == nom_bas:
            return True
    return False


def _contrat_signe_du_client(email_client, nom_client):
    """Ligne du contrat signe le plus recent du client, sinon None."""
    try:
        connexion = database.get_connection()
        curseur = connexion.execute("SELECT * FROM contrats")
        colonnes = [d[0] for d in curseur.description]
        lignes = [dict(zip(colonnes, l)) for l in curseur.fetchall()]
        connexion.close()
    except Exception as exc:
        print(f"Lecture contrats : {exc}")
        return None
    email_bas = (email_client or '').strip().lower()
    nom_bas = (nom_client or '').strip().lower()
    candidats = []
    for ligne in lignes:
        valeurs = {str(k).lower(): ('' if val is None else str(val))
                   for k, val in ligne.items()}
        statut = ''
        for cle_statut in ('statut', 'status', 'etat', 'state'):
            if cle_statut in valeurs:
                statut = valeurs[cle_statut].lower()
                break
        if statut and 'sign' not in statut:
            continue
        texte = ' '.join(valeurs.values()).lower()
        if (email_bas and email_bas in texte) or (nom_bas and nom_bas in texte):
            candidats.append(ligne)
    return candidats[-1] if candidats else None


def _request_id_contrat(ligne):
    """Retrouve l'identifiant (40 hex) du contrat dans la ligne."""
    for valeur in (ligne or {}).values():
        s = str(valeur or '').strip()
        if len(s) == 40 and all(c in '0123456789abcdef' for c in s.lower()):
            return s
    return ''


def _pdf_contrat_signe(request_id):
    """Bytes du PDF du contrat signe (fichier local, sinon SignNow)."""
    if not request_id:
        return None
    dossiers = ('contrats_signes',
                os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'contrats_signes'))
    for dossier in dossiers:
        try:
            if os.path.isdir(dossier):
                for nom_fichier in os.listdir(dossier):
                    if request_id in nom_fichier and nom_fichier.lower().endswith('.pdf'):
                        with open(os.path.join(dossier, nom_fichier), 'rb') as flux:
                            return flux.read()
        except Exception:
            continue
    try:
        contenu = signnow.telecharger(request_id)
        if contenu:
            return contenu
    except Exception as exc:
        print(f"Telechargement contrat : {exc}")
    return None


# ---------------------------------------------------------------------------
# Envoi d'une facture par email (SMTP Zoho) : PDF joint + lien du contrat
# + cle de licence OptiPC. Bouton "Envoyer" de la page Factures.
# ---------------------------------------------------------------------------
@app.route('/factures/<int:facture_id>/envoyer', methods=['POST'])
@admin_required
def envoyer_facture(facture_id):
    style_page = "font-family:Arial;max-width:520px;margin:60px auto;padding:24px"
    facture = database.obtenir_facture(facture_id)
    if not facture:
        return f"<div style='{style_page}'><p>Facture introuvable.</p><p><a href='/factures'>Retour aux factures</a></p></div>", 404
    email_client = (facture.get('email') or '').strip()
    if not email_client:
        return f"<div style='{style_page}'><p>Cette facture n'a pas d'adresse email client.</p><p><a href='/factures'>Retour aux factures</a></p></div>", 400

    hote = os.environ.get('ZOHO_SMTP_HOST', '')
    try:
        port = int(os.environ.get('ZOHO_SMTP_PORT', '465') or 465)
    except (TypeError, ValueError):
        port = 465
    utilisateur = os.environ.get('ZOHO_SMTP_USER', '')
    mot_de_passe = os.environ.get('ZOHO_SMTP_PASSWORD', '')
    if not (os.environ.get('MAIL_API_KEY', '') or (hote and utilisateur and mot_de_passe)):
        return f"<div style='{style_page}'><p>Aucun envoi configure (MAIL_API_KEY ou SMTP Zoho).</p><p><a href='/factures'>Retour aux factures</a></p></div>", 503

    numero = facture.get('numero_facture') or str(facture_id)
    client = facture.get('client_nom') or ''
    formule_f = facture.get('formule') or facture.get('description') or ''
    try:
        montant_f = float(facture.get('montant') or 0)
    except (TypeError, ValueError):
        montant_f = 0.0
    licence = _licence_de_facture(facture)

    # Controle avant envoi : paiement recu ET contrat signe obligatoires.
    if not _paiement_recu(email_client, client):
        return (f"<div style='{style_page}'><h2>Envoi bloque</h2>"
                f"<p>Aucun paiement enregistre pour <b>{client}</b> ({email_client}).</p>"
                "<p>L'email part uniquement quand le paiement est recu"
                " et le contrat signe.</p>"
                "<p><a href='/factures'>Retour aux factures</a></p></div>"), 409
    contrat_ligne = _contrat_signe_du_client(email_client, client)
    if contrat_ligne is None:
        return (f"<div style='{style_page}'><h2>Envoi bloque</h2>"
                f"<p>Aucun contrat signe trouve pour <b>{client}</b> ({email_client}).</p>"
                "<p>L'email part uniquement quand le paiement est recu"
                " et le contrat signe.</p>"
                "<p><a href='/factures'>Retour aux factures</a></p></div>"), 409
    pdf_contrat = _pdf_contrat_signe(_request_id_contrat(contrat_ligne))
    if pdf_contrat:
        ligne_contrat_txt = "Votre contrat signe est joint a cet email.\n\n"
        ligne_contrat_html = "Votre contrat signe est egalement joint a cet email."
    else:
        ligne_contrat_txt = f"Votre contrat a signer :\n{lien_contrat}\n\n"
        ligne_contrat_html = (f"Votre contrat a signer : <a href='{lien_contrat}'"
                              f" style='color:#7b2ff7;'>{lien_contrat}</a>")
    lien_contrat = os.environ.get('SIGNNOW_SIGNING_LINK',
                                  'https://signnow.com/s/EJxThaKZ')

    try:
        pdf = facture_pdf.construire(facture, taux_tva=formules.TVA)
    except Exception as exc:
        return f"<div style='{style_page}'><p>Impossible de generer le PDF : {exc}</p><p><a href='/factures'>Retour aux factures</a></p></div>", 500

    duree_txt = (str(facture.get('duree')) + ' mois') if facture.get('duree') else 'la duree convenue'
    bloc_licence_txt = (f"Votre cle de licence OptiPC :\n{licence}\n\n") if licence else ''
    texte_email = (
        f"Bonjour {client},\n\n"
        f"Nous vous confirmons votre souscription a un contrat de {duree_txt}"
        f" pour le service {formule_f} avec SecureeTech.\n"
        f"Vous trouverez ci-joint votre facture {numero} ({montant_f:.2f} EUR TTC).\n\n"
        + ligne_contrat_txt
        + bloc_licence_txt +
        "Assistance & entretien : 09 80 80 17 59"
        " (du lundi au vendredi, de 10h00 a 17h00)\n\n"
        "En vous remerciant pour votre confiance,\n"
        "L'equipe SecureeTech\n"
        "Daniel Moreau - Responsable service client & technique\n"
        "contact@secureetech.com - secureetech.com")
    bloc_licence_html = ''
    if licence:
        bloc_licence_html = (
            "<div style='background:#efe9ff;border:1px solid #c9b8ff;border-radius:10px;"
            "padding:16px 18px;margin:14px 0;'>"
            "<p style='margin:0;font-weight:bold;'>Votre cle de licence OptiPC</p>"
            "<p style='margin:8px 0 0;font-family:Consolas,monospace;font-size:18px;"
            "letter-spacing:1px;'><b>" + licence + "</b></p></div>")
    html_email = (
        "<!DOCTYPE html><html><body style='margin:0;padding:0;background:#2a1b4d;"
        "font-family:Arial,Helvetica,sans-serif;'>"
        "<div style='max-width:680px;margin:0 auto;padding:24px;'>"
        "<div style='background:#1e1240;border-radius:14px;padding:24px 30px;'>"
        "<h1 style='color:#ffffff;font-size:21px;margin:0 0 10px;'>Secureetech | assistance informatique Premium</h1>"
        "<div style='height:3px;background:linear-gradient(90deg,#7b2ff7,#f7793b);border-radius:2px;'></div>"
        "</div>"
        "<div style='background:#ffffff;border-radius:14px;padding:26px 30px;margin-top:14px;color:#1e1240;font-size:14px;line-height:1.55;'>"
        f"<p>Bonjour <b>{client}</b>,</p>"
        "<div style='background:#f3edff;border-radius:10px;padding:16px 18px;margin:14px 0;'>"
        "<p style='margin:0;font-weight:bold;'>Confirmation</p>"
        f"<p style='margin:8px 0 0;'>Nous vous confirmons votre souscription a un contrat de <b>{duree_txt}</b>"
        f" pour le service <b>{formule_f}</b> avec SecureeTech.<br>"
        "Ce service est conforme au <b>RGPD</b> (Reglement General sur la Protection des Donnees).</p></div>"
        "<div style='background:#ffffff;border:1px solid #e6e0f5;border-radius:10px;padding:16px 18px;margin:14px 0;'>"
        "<p style='margin:0;font-weight:bold;'>Mise en place de la securite</p>"
        "<p style='margin:8px 0 0;'>Notre informaticien va proceder a la mise en place de la securite de votre systeme"
        " en installant un <b>antivirus de derniere generation</b> ainsi qu'un <b>programme d'optimisation performant</b>.</p>"
        "<p style='margin:8px 0 0;'>L'antivirus reduit les risques lies aux menaces courantes (malwares, sites frauduleux,"
        " tentatives d'intrusion) et l'outil d'optimisation ameliore la stabilite et les performances de votre appareil.</p></div>"
        + bloc_licence_html +
        f"<div style='background:#ffffff;border:1px solid #e6e0f5;border-radius:10px;padding:16px 18px;margin:14px 0;'>"
        f"<p style='margin:0;font-weight:bold;'>Votre facture et votre contrat</p>"
        f"<p style='margin:8px 0 0;'>Votre facture <b>{numero}</b> ({montant_f:.2f} EUR TTC) est jointe a cet email.<br>"
        + ligne_contrat_html + "</p></div>"
        "<div style='background:#ede9fb;border-radius:10px;padding:16px 18px;margin:14px 0;'>"
        "<p style='margin:0;font-weight:bold;'>Assistance &amp; entretien</p>"
        "<p style='margin:8px 0 0;'>Pour toute question, demande d'entretien ou besoin d'assistance,"
        " vous pouvez nous joindre au : <b>09 80 80 17 59</b><br>(du lundi au vendredi, de 10h00 a 17h00)</p></div>"
        "<div style='background:#fdf3e7;border-radius:10px;padding:16px 18px;margin:14px 0;'>"
        "<p style='margin:0;font-weight:bold;'>Engagement juridique &amp; demandes de remboursement</p>"
        "<p style='margin:8px 0 0;'>Conformement a l'article <b>1103</b> du Code civil, le contrat signe engage juridiquement les parties.</p>"
        "<p style='margin:8px 0 0;'>En cas de demande de remboursement, vous devez imperativement <b>contacter SecureeTech</b>"
        " afin de rechercher une solution <b>a l'amiable</b>.</p>"
        "<p style='margin:8px 0 0;'>Toute contestation initiee directement aupres de votre banque <b>sans nous avoir contactes au prealable</b>"
        " pourra etre contestee, et nous nous reservons le droit d'engager toute mesure necessaire afin de faire respecter le contrat.</p></div>"
        "<p style='margin:18px 0 4px;'>En vous remerciant pour votre confiance,<br><b>L'equipe SecureeTech</b></p>"
        "<div style='background:#1e1240;border-radius:10px;padding:18px 22px;margin-top:14px;color:#ffffff;'>"
        "<p style='margin:0;font-size:18px;font-weight:bold;'>Daniel Moreau</p>"
        "<p style='margin:2px 0 12px;color:#f7793b;font-size:11px;letter-spacing:1.5px;'>RESPONSABLE SERVICE CLIENT &amp; TECHNIQUE</p>"
        "<p style='margin:0;font-size:13px;line-height:1.9;'>"
        "<span style='color:#f7793b;font-size:11px;letter-spacing:1px;'>TEL</span>&nbsp;&nbsp;&nbsp;<b>09 80 80 17 59</b><br>"
        "<span style='color:#f7793b;font-size:11px;letter-spacing:1px;'>MAIL</span>&nbsp;&nbsp;contact@secureetech.com<br>"
        "<span style='color:#f7793b;font-size:11px;letter-spacing:1px;'>WEB</span>&nbsp;&nbsp;&nbsp;secureetech.com</p></div>"
        "<p style='margin:14px 0 0;font-size:11px;color:#8a80a8;'>SECUREETECH | Assistance informatique Premium</p>"
        "</div></div></body></html>")
    nom_pdf = 'facture_' + str(numero).replace('/', '-') + '.pdf'
    sujet = f"Confirmation de votre souscription - Secureetech (facture {numero})"

    cle_resend = os.environ.get('MAIL_API_KEY', '')
    if cle_resend:
        # Envoi via l'API HTTPS Resend (le SMTP sortant est bloque par l'hebergeur).
        import json as json_mod
        import base64
        import urllib.request as urlreq
        expediteur = os.environ.get('FACTURE_FROM_EMAIL', 'contact@secureetech.com')
        pieces_jointes = [{'filename': nom_pdf,
                           'content': base64.b64encode(pdf).decode('ascii')}]
        if pdf_contrat:
            pieces_jointes.append({
                'filename': ('contrat_signe_' + str(numero) + '.pdf').replace('/', '-'),
                'content': base64.b64encode(pdf_contrat).decode('ascii')})
        corps_api = json_mod.dumps({
            'from': f"Secureetech <{expediteur}>",
            'to': [email_client],
            'subject': sujet,
            'text': texte_email,
            'html': html_email,
            'attachments': pieces_jointes,
        }).encode('utf-8')
        requete = urlreq.Request(
            'https://api.resend.com/emails',
            data=corps_api,
            headers={'Authorization': 'Bearer ' + cle_resend,
                     'Content-Type': 'application/json',
                     'User-Agent': 'Secureetech-Facturation/1.0'},
            method='POST')
        try:
            urlreq.urlopen(requete, timeout=25)
        except Exception as exc:
            return f"<div style='{style_page}'><p>Echec de l'envoi (Resend) : {exc}</p><p><a href='/factures'>Retour aux factures</a></p></div>", 500
    else:
        import smtplib
        from email.message import EmailMessage
        message = EmailMessage()
        message['Subject'] = sujet
        message['From'] = utilisateur
        message['To'] = email_client
        message.set_content(texte_email)
        message.add_alternative(html_email, subtype='html')
        message.add_attachment(pdf, maintype='application', subtype='pdf', filename=nom_pdf)
        if pdf_contrat:
            message.add_attachment(pdf_contrat, maintype='application',
                                   subtype='pdf', filename='contrat_signe.pdf')
        try:
            if port == 465:
                with smtplib.SMTP_SSL(hote, port, timeout=25) as smtp:
                    smtp.login(utilisateur, mot_de_passe)
                    smtp.send_message(message)
            else:
                with smtplib.SMTP(hote, port, timeout=25) as smtp:
                    smtp.starttls()
                    smtp.login(utilisateur, mot_de_passe)
                    smtp.send_message(message)
        except Exception as exc:
            return f"<div style='{style_page}'><p>Echec de l'envoi : {exc}</p><p><a href='/factures'>Retour aux factures</a></p></div>", 500

    return (f"<div style='{style_page}'><h2>Facture envoyee</h2>"
            f"<p>La facture {numero} a ete envoyee a <b>{email_client}</b> :"
            f" facture PDF{' + contrat signe joint' if pdf_contrat else ''}"
            f"{(' + cle OptiPC ' + licence) if licence else ''}.</p>"
            "<p><a href='/factures'>Retour aux factures</a></p></div>"), 200


# ---------------------------------------------------------------------------
# API /api/contrat : appelee par la page basket WordPress (snippet
# « Champs adresse et TVA basket »). Cree la facture et renvoie le lien
# du contrat a signer + le numero de facture, au format attendu :
# { ok, link, invoice_number, facture_url }
# ---------------------------------------------------------------------------
@app.route('/api/contrat', methods=['POST'])
def api_contrat():
    donnees = request.get_json(silent=True) or request.form.to_dict()

    jeton_attendu = (os.environ.get('API_FACTURE_TOKEN', '')
                     or os.environ.get('LINK_HUB_TOKEN', ''))
    if not jeton_attendu:
        return jsonify({'ok': False, 'erreur': "Aucun jeton d'API configure sur le serveur."}), 503
    jeton_recu = (request.headers.get('X-Api-Token', '') or donnees.get('token', '')).strip()
    if jeton_recu != jeton_attendu:
        return jsonify({'ok': False, 'erreur': 'Jeton invalide.'}), 401

    prenom = (donnees.get('prenom') or '').strip()
    nom_famille = (donnees.get('nom') or '').strip()
    nom = (prenom + ' ' + nom_famille).strip()
    email = (donnees.get('email') or '').strip()
    adresse = (donnees.get('adresse') or '').strip()
    formule = (donnees.get('formule') or donnees.get('description') or '').strip()

    brut = str(donnees.get('montant') or '').strip()
    brut = brut.replace('\u202f', '').replace('\u00a0', '').replace(' ', '')
    brut = brut.replace('EUR', '').replace('\u20ac', '')
    if ',' in brut and '.' in brut:
        brut = brut.replace('.', '').replace(',', '.')
    elif ',' in brut:
        brut = brut.replace(',', '.')
    try:
        montant_ttc = float(brut) or None
    except (TypeError, ValueError):
        montant_ttc = None

    if not nom:
        return jsonify({'ok': False, 'erreur': 'Nom du client manquant.'}), 400
    if not formule:
        return jsonify({'ok': False, 'erreur': 'Formule manquante.'}), 400

    duree = 0
    montant_ht = None
    if montant_ttc is not None:
        deduit = formules.depuis_ttc(formule, montant_ttc)
        if deduit:
            montant_ht = deduit['ht']
            duree = deduit['duree']

    calcul = formules.montants(formule, duree, montant_ht=montant_ht)
    if not calcul and montant_ttc is not None:
        ht_estime = round(montant_ttc / (1 + formules.TVA), 2)
        calcul = {'ttc': round(montant_ttc, 2),
                  'ht': ht_estime,
                  'tva': round(montant_ttc - ht_estime, 2)}
        if not duree:
            duree = 12
    if not calcul:
        return jsonify({'ok': False,
                        'erreur': f"Impossible de determiner le prix pour « {formule} »."}), 400
    try:
        description_facture = formules.description(formule, duree) or formule
    except Exception:
        description_facture = formule

    cle = _cle_commande(email, nom, formule)
    existante = database.facture_par_cle(cle)
    if existante:
        facture_id = existante['id']
        numero = existante['numero_facture']
    else:
        numero = _numero_facture()
        facture_id = database.ajouter_facture(
            numero_facture=numero,
            date_facture=datetime.now().strftime('%Y-%m-%d'),
            client_nom=nom,
            montant=calcul['ttc'],
            email=email,
            description=description_facture,
            formule=formule,
            duree=duree,
            montant_ht=calcul['ht'],
            tva=calcul['tva'],
            cle_commande=cle,
            client_adresse=adresse,
            plateforme=(donnees.get('plateforme') or '').strip(),
            lien_paiement='',
            numero_tva=(donnees.get('numero_tva') or donnees.get('tva') or '').strip())
        if not facture_id:
            return jsonify({'ok': False,
                            'erreur': f"Le numero de facture « {numero} » existe deja."}), 409

    telephone_client = (donnees.get('telephone') or donnees.get('phone') or '').strip()
    _ajouter_contact_ringover(nom, email, telephone_client)

    if telephone_client:
        try:
            connexion = database.get_connection()
            connexion.execute("UPDATE factures SET client_telephone = ? WHERE id = ?",
                              (telephone_client, facture_id))
            connexion.commit()
            connexion.close()
        except Exception as exc:
            print(f"Stockage telephone (non bloquant) : {exc}")

    facture_courante = database.obtenir_facture(facture_id) or {}
    cle_licence = _licence_de_facture(facture_courante, telephone_client)
    duree_licence = facture_courante.get('duree') or duree or 12

    lien_contrat = os.environ.get('SIGNNOW_SIGNING_LINK',
                                  'https://signnow.com/s/EJxThaKZ')

    return jsonify({
        'ok': True,
        'link': lien_contrat,
        'invoice_number': numero,
        'facture_url': url_for('api_facture_pdf', facture_id=facture_id, _external=True),
        'licence_optipc': cle_licence,
        'licence_duree_mois': duree_licence,
    }), 200


# ---------------------------------------------------------------------------
# Webhook Stripe : enregistre les paiements en temps reel (aucune dependance
# externe : la signature est verifiee avec hmac de la bibliotheque standard).
# ---------------------------------------------------------------------------
@app.route('/webhook/stripe', methods=['POST'])
def webhook_stripe():
    secret_webhook = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
    charge_utile = request.get_data()

    if secret_webhook:
        import hmac as hmac_mod
        signature = request.headers.get('Stripe-Signature', '')
        elements = dict(m.split('=', 1) for m in signature.split(',') if '=' in m)
        horodatage = elements.get('t', '')
        attendu = hmac_mod.new(secret_webhook.encode(),
                               horodatage.encode() + b'.' + charge_utile,
                               hashlib.sha256).hexdigest()
        if not hmac_mod.compare_digest(attendu, elements.get('v1', '')):
            return jsonify({'ok': False, 'erreur': 'Signature invalide.'}), 400

    evenement = request.get_json(silent=True) or {}

    if evenement.get('type') == 'charge.succeeded':
        objet = (evenement.get('data') or {}).get('object') or {}
        details = objet.get('billing_details') or {}
        montant = (objet.get('amount') or 0) / 100.0
        email = details.get('email') or objet.get('receipt_email') or ''
        nom = details.get('name') or ''
        reference = objet.get('id', '')
        try:
            deja = [p for p in database.obtenir_tous_paiements()
                    if (p.get('reference_externe') or '') == reference]
            if reference and not deja:
                database.ajouter_paiement(
                    date_paiement=datetime.now().strftime('%Y-%m-%d'),
                    source='Stripe',
                    montant=montant,
                    client_nom=nom,
                    email=email,
                    reference_externe=reference,
                    entite='')
                print(f"Webhook Stripe : paiement {reference} enregistre ({montant} EUR)")
        except Exception as exc:
            print(f"Webhook Stripe : enregistrement impossible ({exc})")

    return jsonify({'ok': True}), 200


@app.errorhandler(404)
def not_found(error):
    return render_template('404.html'), 404

@app.errorhandler(500)
def server_error(error):
    return render_template('500.html'), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=config.DEBUG)
