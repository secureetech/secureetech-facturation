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


def _cle_licence_optipc(cle_commande, email):
    """Cle de licence OptiPC (12 mois), deterministe par commande.

    Derivee de la cle de commande + email avec la SECRET_KEY du serveur :
    la meme commande redonne toujours la meme licence (idempotent),
    sans table supplementaire en base.
    """
    graine = f"{cle_commande}|{(email or '').lower()}|optipc-12m".encode()
    empreinte = hashlib.sha256(config.SECRET_KEY.encode() + graine).hexdigest().upper()
    return f"OPTIPC-{empreinte[0:4]}-{empreinte[4:8]}-{empreinte[8:12]}-{empreinte[12:16]}"


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
                     'Content-Type': 'application/json'},
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
            'licence_optipc': _cle_licence_optipc(cle, email),
            'licence_duree_mois': 12,
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
        'licence_optipc': _cle_licence_optipc(cle, email),
        'licence_duree_mois': 12,
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

    try:
        montant_ttc = float(donnees.get('montant') or 0) or None
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
    if not calcul:
        return jsonify({'ok': False,
                        'erreur': f"Impossible de determiner le prix pour « {formule} »."}), 400

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
            description=formules.description(formule, duree),
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

    _ajouter_contact_ringover(nom, email,
                              (donnees.get('telephone') or donnees.get('phone') or '').strip())

    lien_contrat = os.environ.get('SIGNNOW_SIGNING_LINK',
                                  'https://signnow.com/s/EJxThaKZ')

    return jsonify({
        'ok': True,
        'link': lien_contrat,
        'invoice_number': numero,
        'facture_url': url_for('api_facture_pdf', facture_id=facture_id, _external=True),
        'licence_optipc': _cle_licence_optipc(cle, email),
        'licence_duree_mois': 12,
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
