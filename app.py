from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from functools import wraps
from datetime import datetime
import os
import config
import database
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
import io

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

# Initialiser la BD
database.init_db()
database.regenerate_clients_summary()

# Décorateur pour vérifier l'authentification
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'authenticated' not in session:
            return redirect(url_for('connexion'))
        return f(*args, **kwargs)
    return decorated_function

# ============ ROUTES DE CONNEXION ============
@app.route('/connexion', methods=['GET', 'POST'])
def connexion():
    if request.method == 'POST':
        password = request.form.get('password', '')
        if password == config.ADMIN_ACCESS_PASSWORD:
            session['authenticated'] = True
            return redirect(url_for('index'))
        else:
            return render_template('connexion.html', error='Mot de passe incorrect')
    return render_template('connexion.html')

@app.route('/deconnexion')
def deconnexion():
    session.clear()
    return redirect(url_for('connexion'))

# ============ DASHBOARD PRINCIPAL ============
@app.route('/')
@login_required
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
                         clients_top=clients[:10])

# ============ PAGE CLIENTS (AGRÉGÉ) ============
@app.route('/clients')
@login_required
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
@login_required
def detail_client(client_nom):
    paiements = database.obtenir_paiements_client(client_nom)
    
    total = sum(p['montant'] for p in paiements)
    sources = set(p['source'] for p in paiements)
    
    return render_template('client_detail.html',
                         client_nom=client_nom,
                         paiements=paiements,
                         total=total,
                         sources=sources,
                         nb_paiements=len(paiements))

# ============ PAGE PAIEMENTS (FILTRAGE) ============
@app.route('/paiements')
@login_required
def paiements():
    # Récupérer les filtres
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    source = request.args.get('source', '')
    min_amount = request.args.get('min_amount', '')
    max_amount = request.args.get('max_amount', '')
    
    # Appliquer filtres
    paiements_filtres = database.filtrer_paiements(
        date_from=date_from if date_from else None,
        date_to=date_to if date_to else None,
        source=source if source else None,
        min_amount=float(min_amount) if min_amount else None,
        max_amount=float(max_amount) if max_amount else None
    )
    
    # Sources disponibles
    all_paiements = database.obtenir_tous_paiements()
    sources_list = sorted(list(set(p['source'] for p in all_paiements)))
    
    return render_template('paiements.html',
                         paiements=paiements_filtres,
                         sources=sources_list,
                         date_from=date_from,
                         date_to=date_to,
                         source=source,
                         min_amount=min_amount,
                         max_amount=max_amount)

# ============ PAGE FACTURES ============
@app.route('/factures')
@login_required
def factures():
    factures = database.obtenir_toutes_factures()
    return render_template('factures.html', factures=factures)

# ============ API ENDPOINTS ============
@app.route('/api/factures/ajouter', methods=['POST'])
@login_required
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
@login_required
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
