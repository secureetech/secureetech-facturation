from flask import Flask, render_template, request, jsonify, redirect, url_for, session, send_file
from functools import wraps
from datetime import datetime
import os
import config
import database
import signnow
from reportlab.lib.pagesizes import letter, A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
import io

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

# Préparation de la base au démarrage.
# Encapsulé : un incident ici ne doit jamais empêcher le serveur de
# démarrer, sinon la plateforme ne voit qu'un conteneur qui ne répond pas.
try:
    database.init_db()
    database.init_contrats()
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
    }
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


# ============ RECHERCHE (accessible aux vendeurs) ============
@app.route('/recherche')
@login_required
def recherche():
    """Recherche client. Rien ne s'affiche tant qu'aucun terme n'est saisi."""
    terme = request.args.get('q', '').strip()

    resultats = []
    if len(terme) >= RECHERCHE_MIN:
        resultats = database.obtenir_clients_summary(recherche=terme)[:50]

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
@app.route('/factures')
@admin_required
def factures():
    factures = database.obtenir_toutes_factures()
    return render_template('factures.html', factures=factures)

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
    facture = database.obtenir_facture(facture_id)
    if not facture:
        return jsonify({'error': 'Facture non trouvée'}), 404
    
    # Créer un PDF simple avec ReportLab
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    elements = []
    
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=24,
        textColor=colors.HexColor('#1a1a1a'),
        spaceAfter=30
    )
    
    elements.append(Paragraph(f"FACTURE {facture['numero_facture']}", title_style))
    elements.append(Spacer(1, 0.2*inch))
    
    data = [
        ['Client:', facture['client_nom']],
        ['Date:', facture['date_facture']],
        ['Email:', facture['email'] or 'N/A'],
        ['Montant:', f"€{facture['montant']:.2f}"],
        ['Description:', facture['description'] or 'N/A']
    ]
    
    table = Table(data, colWidths=[1.5*inch, 4.5*inch])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), colors.lightgrey),
        ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
        ('GRID', (0, 0), (-1, -1), 1, colors.grey)
    ]))
    
    elements.append(table)
    doc.build(elements)
    buffer.seek(0)
    
    return buffer.getvalue(), 200, {'Content-Disposition': f'attachment; filename=facture_{facture_id}.pdf', 'Content-Type': 'application/pdf'}


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


# ============ ERROR HANDLERS ============
@app.errorhandler(404)
def not_found(error):
    return render_template('404.html'), 404

@app.errorhandler(500)
def server_error(error):
    return render_template('500.html'), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=config.DEBUG)
