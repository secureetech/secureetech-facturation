#!/usr/bin/env python3
"""
Catalogue des formules SecureeTech et calcul des montants.

Les prix affichés aux clients sont hors taxes ; la TVA espagnole de 21 %
s'ajoute pour obtenir le montant réellement encaissé.
"""

TVA = 0.21

# Abonnements : prix HT par durée, en mois
ABONNEMENTS = {
    'Basic': {6: 249, 12: 399, 24: 549, 36: 699},
    'Sérénité': {6: 299, 12: 499, 24: 699, 36: 999},
    'Privilège': {6: 399, 12: 699, 24: 999, 36: 1299},
}

# Offres à paiement unique
OFFRES_UNIQUES = {
    'Excellence': {'prix_ht': 3897, 'libelle': 'Formule famille, jusqu\'à 5 appareils'},
    'Infinity': {'prix_ht': 9990, 'libelle': 'Protection numérique à vie, 5 appareils'},
}

DUREES = [6, 12, 24, 36]


def catalogue():
    """Liste exploitable par un menu déroulant."""
    entrees = []
    for formule, grille in ABONNEMENTS.items():
        for duree, prix in sorted(grille.items()):
            entrees.append({
                'cle': f'{formule}|{duree}',
                'formule': formule,
                'duree': duree,
                'prix_ht': prix,
                'libelle': f'{formule} — {duree} mois — {prix} € HT',
            })
    for formule, infos in OFFRES_UNIQUES.items():
        entrees.append({
            'cle': f'{formule}|0',
            'formule': formule,
            'duree': 0,
            'prix_ht': infos['prix_ht'],
            'libelle': f"{formule} — paiement unique — {infos['prix_ht']} € HT",
        })
    return entrees


def prix_ht(formule, duree=0):
    """Prix hors taxes d'une formule, ou None si la combinaison n'existe pas."""
    if formule in ABONNEMENTS:
        return ABONNEMENTS[formule].get(int(duree or 0))
    if formule in OFFRES_UNIQUES:
        return OFFRES_UNIQUES[formule]['prix_ht']
    return None


def montants(formule, duree=0, montant_ht=None):
    """Retourne le détail HT / TVA / TTC.

    montant_ht permet de forcer un prix négocié hors grille.
    """
    base = montant_ht if montant_ht is not None else prix_ht(formule, duree)
    if base is None:
        return None

    base = round(float(base), 2)
    tva = round(base * TVA, 2)
    return {
        'ht': base,
        'tva': tva,
        'taux_tva': TVA,
        'ttc': round(base + tva, 2),
    }


def description(formule, duree=0):
    """Libellé lisible pour la ligne de facture."""
    if formule in OFFRES_UNIQUES:
        return f"{formule} — {OFFRES_UNIQUES[formule]['libelle']}"
    if duree:
        return f'Abonnement {formule} — {duree} mois'
    return f'Abonnement {formule}'
