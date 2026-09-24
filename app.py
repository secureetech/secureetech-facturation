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

# ============ ROUTES PRINCIPALES ============
@app.route('/')
def index():
    if 'authenticated' not in session:
        return redirect(url_for('connexion'))
    
    paiements = database.obtenir_tous_paiements()
    factures = database.obtenir_toutes_factures()
    
    total_paiements = sum(p['montant'] for p in paiements if p['statut'] != 'deleted')
    total_factures = sum(f['montant'] for f in factures)
    
    return render_template('index.html', 
                         paiements=paiements, 
                         factures=factures,
                         total_paiements=total_paiements,
                         total_factures=total_factures)

@app.route('/paiements')
@login_required
def paiements():
    paiements = database.obtenir_tous_paiements()
    sources = list(set(p['source'] for p in paiements))
    return render_template('paiements.html', paiements=paiements, sources=sources)

@app.route('/factures')
@login_required
def factures():
    factures = database.obtenir_toutes_factures()
    return render_template('factures.html', factures=factures)

# ============ API ENDPOINTS ============
@app.route('/api/paiements/ajouter', methods=['POST'])
@login_required
def api_ajouter_paiement():
    data = request.json
    try:
        paiement_id = database.ajouter_paiement(
            date_paiement=data.get('date_paiement'),
            source=data.get('source'),
            montant=float(data.get('montant')),
            client_nom=data.get('client_nom', ''),
            email=data.get('email', ''),
            reference_externe=data.get('reference_externe', '')
        )
        return jsonify({'success': True, 'id': paiement_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/paiements/<int:paiement_id>/supprimer', methods=['DELETE'])
@login_required
def api_supprimer_paiement(paiement_id):
    try:
        database.supprimer_paiement(paiement_id)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

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
        ['Montant:', f"${facture['montant']:.2f}"],
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
