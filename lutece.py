#!/usr/bin/env python3
"""
lutece — un compilateur pour un petit langage aux mots-clés français,
qui produit de l'assembleur x86-64, assemblé et lié par gcc.

    python3 lutece.py exemples/premiers.lut          compile et lance
    python3 lutece.py exemples/premiers.lut -o prog  produit l'exécutable prog
    python3 lutece.py exemples/premiers.lut -S       montre l'assembleur

Étapes : analyse lexicale -> analyse syntaxique (descente récursive) ->
arbre -> vérifications (variables, nombre d'arguments) -> optimisations
(pliage de constantes, code mort) -> génération d'assembleur.
"""
import os
import re
import subprocess
import sys
import tempfile

# ================================================================== erreurs


class ErreurLutece(Exception):
    def __init__(self, message, ligne=None, colonne=None):
        super().__init__(message)
        self.ligne, self.colonne = ligne, colonne


# ============================================================== lexique

MOTS_CLES = {"fonction", "fin", "soit", "si", "alors", "sinon", "tant", "que", "faire", "pour", "de", "à",
             "retourner", "afficher", "et", "ou", "non", "vrai", "faux", "tableau", "pas", "sortir", "continuer"}

MOTIF = re.compile(r"""
    (?P<espace>[ \t\r]+) | (?P<commentaire>\#[^\n]*) | (?P<ligne>\n) |
    (?P<nombre>\d+) | (?P<texte>"(?:[^"\\\n]|\\.)*") |
    (?P<nom>[A-Za-zÀ-ÿ_][A-Za-zÀ-ÿ0-9_]*) |
    (?P<op>==|!=|<=|>=|<-|[-+*/%<>=(),\[\]:])
""", re.VERBOSE)


class Jeton:
    __slots__ = ("genre", "val", "ligne", "col")

    def __init__(self, genre, val, ligne, col):
        self.genre, self.val, self.ligne, self.col = genre, val, ligne, col

    def __repr__(self):
        return f"{self.genre}:{self.val!r}"


def lexique(source):
    jetons, ligne, debut_ligne, pos = [], 1, 0, 0
    while pos < len(source):
        m = MOTIF.match(source, pos)
        if not m:
            raise ErreurLutece(f"caractère inattendu {source[pos]!r}", ligne, pos - debut_ligne + 1)
        genre, val = m.lastgroup, m.group()
        col = pos - debut_ligne + 1
        if genre == "ligne":
            if jetons and jetons[-1].genre != "fin_ligne":
                jetons.append(Jeton("fin_ligne", "\n", ligne, col))
            ligne, debut_ligne = ligne + 1, m.end()
        elif genre == "nom" and val in MOTS_CLES:
            jetons.append(Jeton("mot", val, ligne, col))
        elif genre == "texte":
            jetons.append(Jeton("texte", bytes(val[1:-1], "utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8"), ligne, col))
        elif genre == "nombre":
            if int(val) >= 2 ** 63:
                raise ErreurLutece(f"nombre trop grand : {val}", ligne, col)
            jetons.append(Jeton("nombre", int(val), ligne, col))
        elif genre not in ("espace", "commentaire"):
            jetons.append(Jeton(genre, val, ligne, col))
        pos = m.end()
    jetons.append(Jeton("fin_ligne", "\n", ligne, 1))
    jetons.append(Jeton("eof", None, ligne + 1, 1))
    return jetons


# ===================================================================== arbre
# Noeuds sous forme de tuples : (genre, position, ...). Simple et suffisant.

# ============================================================ syntaxe


class Analyseur:
    """Grammaire (une instruction par ligne) :
      programme   := (fonction | instruction)*
      fonction    := 'fonction' NOM '(' params ')' bloc 'fin'
      instruction := 'soit' NOM '=' expr | NOM '=' expr | NOM '[' expr ']' '=' expr
                   | 'si' expr 'alors' bloc ('sinon' 'si' ... | 'sinon' bloc)? 'fin'
                   | 'tant' 'que' expr 'faire' bloc 'fin'
                   | 'pour' NOM 'de' expr 'à' expr ('pas' expr)? 'faire' bloc 'fin'
                   | 'retourner' expr? | 'afficher' (expr | TEXTE) (',' ...)*
                   | 'sortir' | 'continuer' | expr
      expr        := ou ;  ou := et ('ou' et)* ; et := non ('et' non)* ; non := 'non' non | comparaison
      comparaison := somme (('=='|'!='|'<'|'<='|'>'|'>=') somme)?
      somme       := produit (('+'|'-') produit)* ; produit := unaire (('*'|'/'|'%') unaire)*
      unaire      := '-' unaire | primaire
      primaire    := NOMBRE | 'vrai' | 'faux' | NOM | NOM '(' args ')' | NOM '[' expr ']'
                   | 'tableau' '(' expr ')' | '(' expr ')'
    """

    def __init__(self, jetons):
        self.j, self.k = jetons, 0

    @property
    def courant(self):
        return self.j[self.k]

    def voit(self, genre, val=None):
        c = self.courant
        return c.genre == genre and (val is None or c.val == val)

    def voit_mot(self, *mots):
        return self.courant.genre == "mot" and self.courant.val in mots

    def prend(self, genre, val=None, quoi=None):
        c = self.courant
        if not self.voit(genre, val):
            vu = "la fin de la ligne" if c.genre == "fin_ligne" else "la fin du fichier" if c.genre == "eof" else f"« {c.val} »"
            raise ErreurLutece(f"j'attendais {quoi or (val and '« ' + val + ' »') or genre}, j'ai trouvé {vu}", c.ligne, c.col)
        self.k += 1
        return c

    def sauts(self):
        while self.voit("fin_ligne"):
            self.k += 1

    def programme(self):
        fonctions, principal = [], []
        self.sauts()
        while not self.voit("eof"):
            if self.voit_mot("fonction"):
                fonctions.append(self.fonction())
            else:
                principal.append(self.instruction())
            self.sauts()
        return fonctions, principal

    def fonction(self):
        debut = self.prend("mot", "fonction")
        nom = self.prend("nom", quoi="le nom de la fonction")
        self.prend("op", "(")
        params = []
        if not self.voit("op", ")"):
            params.append(self.prend("nom", quoi="un paramètre").val)
            while self.voit("op", ","):
                self.k += 1
                params.append(self.prend("nom", quoi="un paramètre").val)
        self.prend("op", ")")
        self.voit("op", ":") and self.prend("op", ":")
        corps = self.bloc("fin")
        self.prend("mot", "fin", quoi="« fin » (pour terminer la fonction)")
        return ("fonction", (debut.ligne, debut.col), nom.val, params, corps)

    def bloc(self, *terminaisons):
        instrs = []
        self.sauts()
        while not self.voit_mot(*terminaisons):
            if self.voit("eof"):
                c = self.courant
                raise ErreurLutece(f"il manque « {terminaisons[-1]} »", c.ligne, c.col)
            instrs.append(self.instruction())
            self.sauts()
        return instrs

    def fin_instruction(self):
        # une instruction se termine en fin de ligne, ou juste avant un « fin » / « sinon »
        # (pour écrire  si n < 2 alors retourner n fin  sur une seule ligne)
        if not self.voit("fin_ligne") and not self.voit("eof") and not self.voit_mot("fin", "sinon"):
            c = self.courant
            raise ErreurLutece(f"« {c.val} » en trop à la fin de l'instruction", c.ligne, c.col)

    def instruction(self):
        c = self.courant
        pos = (c.ligne, c.col)
        if self.voit_mot("soit"):
            self.k += 1
            nom = self.prend("nom", quoi="un nom de variable").val
            self.prend("op", "=", quoi="« = »")
            r = ("soit", pos, nom, self.expr())
        elif self.voit_mot("si"):
            r = self.si()
        elif self.voit_mot("tant"):
            self.k += 1
            self.prend("mot", "que")
            cond = self.expr()
            self.prend("mot", "faire")
            corps = self.bloc("fin")
            self.prend("mot", "fin")
            r = ("tantque", pos, cond, corps)
        elif self.voit_mot("pour"):
            self.k += 1
            var = self.prend("nom", quoi="la variable de la boucle").val
            self.prend("mot", "de")
            a = self.expr()
            if self.voit("nom", "a"):  # « a » sans accent est accepté ici
                self.k += 1
            else:
                self.prend("mot", "à", quoi="« à »")
            b = self.expr()
            pas = ("nombre", pos, 1)
            if self.voit_mot("pas"):
                self.k += 1
                pas = self.expr()
            self.prend("mot", "faire")
            corps = self.bloc("fin")
            self.prend("mot", "fin")
            r = ("pour", pos, var, a, b, pas, corps)
        elif self.voit_mot("retourner"):
            self.k += 1
            r = ("retourner", pos, None if self.voit("fin_ligne") else self.expr())
        elif self.voit_mot("afficher"):
            self.k += 1
            morceaux = [self.morceau_affichage()]
            while self.voit("op", ","):
                self.k += 1
                morceaux.append(self.morceau_affichage())
            r = ("afficher", pos, morceaux)
        elif self.voit_mot("sortir", "continuer"):
            self.k += 1
            r = (c.val, pos)
        elif self.voit("nom") and self.j[self.k + 1].genre == "op" and self.j[self.k + 1].val == "=":
            nom = self.prend("nom").val
            self.k += 1
            r = ("affecte", pos, nom, self.expr())
        else:
            e = self.expr()
            if e[0] == "indice" and self.voit("op", "="):
                self.k += 1
                r = ("affecte_case", pos, e[2], e[3], self.expr())
            else:
                r = ("expr", pos, e)
        if r[0] not in ("si", "tantque", "pour"):
            self.fin_instruction()
        return r

    def si(self):
        c = self.prend("mot", "si")
        cond = self.expr()
        self.prend("mot", "alors", quoi="« alors »")
        alors = self.bloc("sinon", "fin")
        sinon = []
        if self.voit_mot("sinon"):
            self.k += 1
            if self.voit_mot("si"):  # sinon si ... : un seul « fin » pour toute la chaîne
                return ("si", (c.ligne, c.col), cond, alors, [self.si()])
            sinon = self.bloc("fin")
        self.prend("mot", "fin", quoi="« fin » (pour fermer le si)")
        return ("si", (c.ligne, c.col), cond, alors, sinon)

    def morceau_affichage(self):
        if self.voit("texte"):
            t = self.prend("texte")
            return ("texte", (t.ligne, t.col), t.val)
        return self.expr()

    # ---- expressions, par priorité croissante
    def expr(self):
        g = self.et()
        while self.voit_mot("ou"):
            c = self.prend("mot")
            g = ("ou", (c.ligne, c.col), g, self.et())
        return g

    def et(self):
        g = self.non()
        while self.voit_mot("et"):
            c = self.prend("mot")
            g = ("et", (c.ligne, c.col), g, self.non())
        return g

    def non(self):
        if self.voit_mot("non"):
            c = self.prend("mot")
            return ("non", (c.ligne, c.col), self.non())
        return self.comparaison()

    def comparaison(self):
        g = self.somme()
        if self.voit("op") and self.courant.val in ("==", "!=", "<", "<=", ">", ">="):
            c = self.prend("op")
            g = ("bin", (c.ligne, c.col), c.val, g, self.somme())
            if self.voit("op") and self.courant.val in ("==", "!=", "<", "<=", ">", ">="):
                raise ErreurLutece("comparaisons enchaînées : écris  a < b et b < c", self.courant.ligne, self.courant.col)
        return g

    def somme(self):
        g = self.produit()
        while self.voit("op") and self.courant.val in "+-" and self.courant.val:
            c = self.prend("op")
            g = ("bin", (c.ligne, c.col), c.val, g, self.produit())
        return g

    def produit(self):
        g = self.unaire()
        while self.voit("op") and self.courant.val in ("*", "/", "%"):
            c = self.prend("op")
            g = ("bin", (c.ligne, c.col), c.val, g, self.unaire())
        return g

    def unaire(self):
        if self.voit("op", "-"):
            c = self.prend("op")
            return ("bin", (c.ligne, c.col), "-", ("nombre", (c.ligne, c.col), 0), self.unaire())
        return self.primaire()

    def primaire(self):
        c = self.courant
        pos = (c.ligne, c.col)
        if self.voit("nombre"):
            self.k += 1
            return ("nombre", pos, c.val)
        if self.voit_mot("vrai", "faux"):
            self.k += 1
            return ("nombre", pos, 1 if c.val == "vrai" else 0)
        if self.voit_mot("tableau"):
            self.k += 1
            self.prend("op", "(")
            n = self.expr()
            self.prend("op", ")")
            return ("tableau", pos, n)
        if self.voit("op", "("):
            self.k += 1
            e = self.expr()
            self.prend("op", ")", quoi="« ) »")
            return e
        if self.voit("nom"):
            self.k += 1
            if self.voit("op", "("):
                self.k += 1
                args = []
                if not self.voit("op", ")"):
                    args.append(self.expr())
                    while self.voit("op", ","):
                        self.k += 1
                        args.append(self.expr())
                self.prend("op", ")", quoi="« ) »")
                return ("appel", pos, c.val, args)
            if self.voit("op", "["):
                self.k += 1
                i = self.expr()
                self.prend("op", "]", quoi="« ] »")
                return ("indice", pos, c.val, i)
            return ("var", pos, c.val)
        vu = "la fin de la ligne" if c.genre == "fin_ligne" else f"« {c.val} »"
        raise ErreurLutece(f"une valeur était attendue, j'ai trouvé {vu}", c.ligne, c.col)


# ========================================================== optimisations

def div_c(a, b):
    """Division entière tronquée vers 0, comme en C et comme idiv."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


def plie(n):
    """Pliage des constantes et suppression du code mort, en remontant l'arbre."""
    if not isinstance(n, tuple):
        return n
    if isinstance(n, tuple) and n and isinstance(n[0], str):
        n = tuple(plie(x) if isinstance(x, tuple) else [plie(y) for y in x] if isinstance(x, list) else x for x in n)
    g = n[0]
    if g == "bin" and n[3][0] == "nombre" and n[4][0] == "nombre":
        a, b, op = n[3][2], n[4][2], n[2]
        if op in "/%" and b == 0:
            return n  # laissé au programme : il s'arrêtera avec un message
        v = {"+": lambda: a + b, "-": lambda: a - b, "*": lambda: a * b,
             "/": lambda: div_c(a, b), "%": lambda: a - b * div_c(a, b),
             "==": lambda: int(a == b), "!=": lambda: int(a != b), "<": lambda: int(a < b),
             "<=": lambda: int(a <= b), ">": lambda: int(a > b), ">=": lambda: int(a >= b)}[op]()
        v = (v + 2 ** 63) % 2 ** 64 - 2 ** 63  # comme le processeur : 64 bits qui bouclent
        return ("nombre", n[1], v)
    if g == "bin" and n[2] in "+-" and n[4][0] == "nombre" and n[4][2] == 0:
        return n[3]  # x + 0, x - 0
    if g == "bin" and n[2] == "*" and n[4][0] == "nombre" and n[4][2] == 1:
        return n[3]
    if g == "non" and n[2][0] == "nombre":
        return ("nombre", n[1], int(not n[2][2]))
    if g in ("et", "ou") and n[2][0] == "nombre":
        gauche_vraie = n[2][2] != 0
        if g == "et" and not gauche_vraie:
            return ("nombre", n[1], 0)
        if g == "ou" and gauche_vraie:
            return ("nombre", n[1], 1)
        return ("non", n[1], ("non", n[1], n[3]))  # le résultat vaut la vérité de la droite
    if g == "si" and n[2][0] == "nombre":
        return ("bloc", n[1], n[3] if n[2][2] else n[4])
    if g == "tantque" and n[2][0] == "nombre" and n[2][2] == 0:
        return ("bloc", n[1], [])
    return n


# ======================================================= génération de code
# Machine à pile simulée sur x86-64 : toute expression laisse sa valeur dans
# rax ; pour a OP b on calcule b, on l'empile, on calcule a, on dépile dans rcx.
# Variables locales et paramètres : sur la pile, sous rbp ([rbp-8], [rbp-16]...).
# Appels : convention System V (6 premiers arguments dans les registres).

REGISTRES_ARGS = ["rdi", "rsi", "rdx", "rcx", "r8", "r9"]


class Generateur:
    def __init__(self, fonctions):
        self.fonctions = {f[2]: f for f in fonctions}
        self.lignes, self.textes, self.etiquette = [], {}, 0

    def e(self, ligne):
        self.lignes.append("        " + ligne if not ligne.endswith(":") else ligne)

    def empile(self, reg="rax"):
        self.e(f"push {reg}")
        self.prof += 1

    def depile(self, reg):
        self.e(f"pop {reg}")
        self.prof -= 1

    def appelle(self, symbole):
        """call, en gardant rsp multiple de 16 comme l'exige la convention System V."""
        if self.prof % 2:
            self.e("sub rsp, 8")
        self.e(f"call {symbole}")
        if self.prof % 2:
            self.e("add rsp, 8")

    def nouvelle(self, prefixe):
        self.etiquette += 1
        return f".L{prefixe}{self.etiquette}"

    def texte(self, s):
        if s not in self.textes:
            self.textes[s] = f".Ltexte{len(self.textes)}"
        return self.textes[s]

    # ---- portée des variables
    def variables_de(self, instrs, vues):
        """Toutes les variables d'un corps de fonction (déclarées par soit/pour)."""
        for i in instrs:
            if i[0] == "soit" and i[2] not in vues:
                vues.append(i[2])
            if i[0] == "pour":
                for nom in (i[2], f"{i[2]}#fin", f"{i[2]}#pas"):
                    if nom not in vues:
                        vues.append(nom)
            for sous in i:
                if isinstance(sous, list) and sous and isinstance(sous[0], tuple) and sous[0] and isinstance(sous[0][0], str):
                    self.variables_de(sous, vues)
        return vues

    def fonction(self, nom, params, corps, principale=False):
        noms = list(params) + [v for v in self.variables_de(corps, []) if v not in params]
        self.place = {v: -8 * (k + 1) for k, v in enumerate(noms)}
        self.declarees = set(params)
        self.boucles = []
        self.prof = 0  # nombre de « push » en cours au-dessus des variables locales
        taille = (8 * len(noms) + 15) // 16 * 16
        self.nom_fonction = nom
        self.fin = self.nouvelle("retour")
        symbole = "main" if principale else f"lut_{nom}"
        self.e(f".globl {symbole}") if principale else None
        self.e(f".type {symbole}, @function")
        self.lignes.append(f"{symbole}:")
        self.e("push rbp")
        self.e("mov rbp, rsp")
        if taille:
            self.e(f"sub rsp, {taille}")
        for k, p in enumerate(params):
            if k < 6:
                self.e(f"mov [rbp{self.place[p]}], {REGISTRES_ARGS[k]}")
            else:  # 7e argument et suivants : déjà sur la pile, au-dessus de l'adresse de retour
                self.e(f"mov rax, [rbp+{16 + 8 * (k - 6)}]")
                self.e(f"mov [rbp{self.place[p]}], rax")
        for i in corps:
            self.instr(i)
        self.e("xor eax, eax")
        self.lignes.append(f"{self.fin}:")
        self.e("leave")
        self.e("ret")

    def adresse(self, nom, pos):
        if nom not in self.place or nom not in self.declarees:
            raise ErreurLutece(f"variable « {nom} » inconnue (il faut d'abord  soit {nom} = ...)", *pos)
        return f"[rbp{self.place[nom]}]"

    # ---- instructions
    def instr(self, i):
        g, pos = i[0], i[1]
        if g == "bloc":
            for x in i[2]:
                self.instr(x)
        elif g == "soit":
            self.expr(i[3])
            self.declarees.add(i[2])
            self.e(f"mov {self.adresse(i[2], pos)}, rax")
        elif g == "affecte":
            self.expr(i[3])
            self.e(f"mov {self.adresse(i[2], pos)}, rax")
        elif g == "affecte_case":
            self.expr(i[4])
            self.empile()
            self.adresse_case(i[2], i[3], pos)
            self.depile("rcx")
            self.e("mov [rax], rcx")
        elif g == "expr":
            self.expr(i[2])
        elif g == "afficher":
            for k, m in enumerate(i[2]):
                if m[0] == "texte":
                    self.e(f"lea rdi, {self.texte(m[2])}[rip]")
                    self.appelle("lut_ecrire_texte")
                else:
                    self.expr(m)
                    self.e("mov rdi, rax")
                    self.appelle("lut_ecrire_entier")
                if k + 1 < len(i[2]):
                    self.appelle("lut_ecrire_espace")
            self.appelle("lut_ecrire_ligne")
        elif g == "retourner":
            if i[2] is not None:
                self.expr(i[2])
            else:
                self.e("xor eax, eax")
            self.e(f"jmp {self.fin}")
        elif g == "si":
            sinon, fin = self.nouvelle("sinon"), self.nouvelle("finsi")
            self.condition(i[2], sinon)
            for x in i[3]:
                self.instr(x)
            if i[4]:
                self.e(f"jmp {fin}")
            self.lignes.append(f"{sinon}:")
            for x in i[4]:
                self.instr(x)
            self.lignes.append(f"{fin}:")
        elif g == "tantque":
            debut, fin = self.nouvelle("tq"), self.nouvelle("fintq")
            self.lignes.append(f"{debut}:")
            self.condition(i[2], fin)
            self.boucles.append((debut, fin))
            for x in i[3]:
                self.instr(x)
            self.boucles.pop()
            self.e(f"jmp {debut}")
            self.lignes.append(f"{fin}:")
        elif g == "pour":
            var, a, b, pas, corps = i[2], i[3], i[4], i[5], i[6]
            if pas[0] == "nombre" and pas[2] == 0:
                raise ErreurLutece("une boucle « pour » avec un pas de 0 ne s'arrêterait jamais", *pas[1])
            for nom in (var, f"{var}#fin", f"{var}#pas"):
                self.declarees.add(nom)
            self.expr(b)
            self.e(f"mov {self.adresse(var + '#fin', pos)}, rax")
            self.expr(pas)
            self.e(f"mov {self.adresse(var + '#pas', pos)}, rax")
            self.expr(a)
            self.e(f"mov {self.adresse(var, pos)}, rax")
            test, suite, fin = self.nouvelle("pour"), self.nouvelle("suite"), self.nouvelle("finpour")
            self.lignes.append(f"{test}:")
            # on continue tant que i <= fin (pas positif) ou i >= fin (pas négatif)
            self.e(f"mov rax, {self.adresse(var, pos)}")
            self.e(f"mov rcx, {self.adresse(var + '#fin', pos)}")
            self.e(f"mov rdx, {self.adresse(var + '#pas', pos)}")
            self.e("cmp rax, rcx")
            self.e("setle r8b")
            self.e("setge r9b")
            self.e("test rdx, rdx")
            self.e("cmovs r8d, r9d")
            self.e("test r8b, r8b")
            self.e(f"jz {fin}")
            self.boucles.append((suite, fin))
            for x in corps:
                self.instr(x)
            self.boucles.pop()
            self.lignes.append(f"{suite}:")
            self.e(f"mov rax, {self.adresse(var + '#pas', pos)}")
            self.e(f"add {self.adresse(var, pos)}, rax")
            self.e(f"jmp {test}")
            self.lignes.append(f"{fin}:")
        elif g in ("sortir", "continuer"):
            if not self.boucles:
                raise ErreurLutece(f"« {g} » en dehors d'une boucle", *pos)
            self.e(f"jmp {self.boucles[-1][1 if g == 'sortir' else 0]}")
        else:
            raise ErreurLutece(f"instruction inconnue {g}", *pos)

    def condition(self, c, si_faux):
        """Saute à si_faux quand c est fausse. Les comparaisons sautent
        directement, sans fabriquer un 0 ou un 1 dans un registre."""
        sauts = {"==": "jne", "!=": "je", "<": "jge", "<=": "jg", ">": "jle", ">=": "jl"}
        if c[0] == "bin" and c[2] in sauts:
            self.operandes(c[3], c[4])
            self.e("cmp rax, rcx")
            self.e(f"{sauts[c[2]]} {si_faux}")
        elif c[0] == "et":
            self.condition(c[2], si_faux)
            self.condition(c[3], si_faux)
        elif c[0] == "ou":
            vrai = self.nouvelle("vrai")
            faux_gauche = self.nouvelle("essaiedroite")
            self.condition(c[2], faux_gauche)
            self.e(f"jmp {vrai}")
            self.lignes.append(f"{faux_gauche}:")
            self.condition(c[3], si_faux)
            self.lignes.append(f"{vrai}:")
        elif c[0] == "non":
            vrai = self.nouvelle("non")
            self.condition(c[2], vrai)  # si c[2] est fausse, « non c[2] » est vraie
            self.e(f"jmp {si_faux}")
            self.lignes.append(f"{vrai}:")
        else:
            self.expr(c)
            self.e("test rax, rax")
            self.e(f"jz {si_faux}")

    def operandes(self, a, b):
        """a dans rax, b dans rcx."""
        if b[0] == "nombre" and -2 ** 31 <= b[2] < 2 ** 31:
            self.expr(a)
            self.e(f"mov rcx, {b[2]}")
        elif b[0] == "var":
            self.expr(a)
            self.e(f"mov rcx, {self.adresse(b[2], b[1])}")
        elif a[0] in ("nombre", "var"):  # a simple : pas besoin de la pile
            self.expr(b)
            self.e("mov rcx, rax")
            self.expr(a)
        else:
            self.expr(b)
            self.empile()
            self.expr(a)
            self.depile("rcx")

    def adresse_case(self, nom, indice, pos):
        """rax = adresse de nom[indice], avec vérification des bornes."""
        self.expr(indice)
        self.e(f"mov rcx, {self.adresse(nom, pos)}")
        self.e("cmp rax, [rcx-8]")  # la taille est rangée juste avant la première case
        self.e(f"mov edi, {pos[0]}")
        self.e("jae lut_hors_bornes")  # non signé : un indice négatif est aussi refusé
        self.e("lea rax, [rcx+rax*8]")

    def expr(self, n):
        g, pos = n[0], n[1]
        if g == "nombre":
            v = n[2]
            self.e("xor eax, eax" if v == 0 else f"mov rax, {v}")
        elif g == "var":
            self.e(f"mov rax, {self.adresse(n[2], pos)}")
        elif g == "bin":
            op = n[2]
            self.operandes(n[3], n[4])
            if op == "+":
                self.e("add rax, rcx")
            elif op == "-":
                self.e("sub rax, rcx")
            elif op == "*":
                self.e("imul rax, rcx")
            elif op in "/%" and n[4][0] == "nombre" and n[4][2] > 1 and n[4][2] & (n[4][2] - 1) == 0:
                # division par une puissance de 2 : des décalages au lieu de idiv (~10× plus lent).
                # Un simple sar arrondirait vers −∞ ; on ajoute 2^k − 1 aux négatifs pour arrondir vers 0.
                k = n[4][2].bit_length() - 1
                if op == "%":
                    self.e("mov rdx, rax")
                self.e("mov rcx, rax")
                self.e("sar rcx, 63")
                self.e(f"shr rcx, {64 - k}")
                self.e("add rax, rcx")
                self.e(f"sar rax, {k}")
                if op == "%":  # x % 2^k = x − (x / 2^k)·2^k
                    self.e(f"shl rax, {k}")
                    self.e("sub rdx, rax")
                    self.e("mov rax, rdx")
            elif op in "/%":
                self.e(f"mov edi, {pos[0]}")
                self.e("test rcx, rcx")
                self.e("jz lut_division_par_zero")
                moins1, fin = self.nouvelle("moins1"), self.nouvelle("findiv")
                self.e("cmp rcx, -1")  # idiv planterait sur −2⁶³ / −1
                self.e(f"je {moins1}")
                self.e("cqo")  # étend le signe de rax dans rdx
                self.e("idiv rcx")
                if op == "%":
                    self.e("mov rax, rdx")
                self.e(f"jmp {fin}")
                self.lignes.append(f"{moins1}:")
                self.e("neg rax" if op == "/" else "xor eax, eax")
                self.lignes.append(f"{fin}:")
            else:
                cc = {"==": "e", "!=": "ne", "<": "l", "<=": "le", ">": "g", ">=": "ge"}[op]
                self.e("cmp rax, rcx")
                self.e(f"set{cc} al")
                self.e("movzx eax, al")
        elif g in ("et", "ou", "non"):
            faux, fin = self.nouvelle("faux"), self.nouvelle("fin")
            self.condition(n, faux)
            self.e("mov eax, 1")
            self.e(f"jmp {fin}")
            self.lignes.append(f"{faux}:")
            self.e("xor eax, eax")
            self.lignes.append(f"{fin}:")
        elif g == "appel":
            nom, args = n[2], n[3]
            if nom not in self.fonctions:
                raise ErreurLutece(f"fonction « {nom} » inconnue", *pos)
            attendus = len(self.fonctions[nom][3])
            if len(args) != attendus:
                raise ErreurLutece(f"« {nom} » attend {attendus} argument(s), pas {len(args)}", *pos)
            pile = max(0, len(args) - 6)  # arguments au-delà du 6e : passés sur la pile
            cale = (self.prof + pile) % 2   # pour que rsp soit multiple de 16 au moment du call
            if cale:
                self.e("sub rsp, 8")
                self.prof += 1
            for a in reversed(args):
                self.expr(a)
                self.empile()
            for k in range(min(6, len(args))):
                self.depile(REGISTRES_ARGS[k])
            self.e(f"call lut_{nom}")
            if pile + cale:
                self.e(f"add rsp, {8 * (pile + cale)}")
                self.prof -= pile + cale
        elif g == "indice":
            self.adresse_case(n[2], n[3], pos)
            self.e("mov rax, [rax]")
        elif g == "tableau":
            self.expr(n[2])
            self.e(f"mov esi, {pos[0]}")
            self.e("mov rdi, rax")
            self.appelle("lut_tableau")
        else:
            raise ErreurLutece(f"expression inconnue {g}", *pos)


# Petite bibliothèque d'exécution, en assembleur aussi, au-dessus de la libc.
EXECUTION = r"""
        .text
lut_ecrire_entier:              # afficher rdi en décimal
        push rbp
        mov rbp, rsp
        mov rsi, rdi
        lea rdi, .Lformat_entier[rip]
        xor eax, eax
        call printf@PLT
        leave
        ret
lut_ecrire_texte:
        push rbp
        mov rbp, rsp
        mov rsi, rdi
        lea rdi, .Lformat_texte[rip]
        xor eax, eax
        call printf@PLT
        leave
        ret
lut_ecrire_espace:
        push rbp
        mov rbp, rsp
        mov edi, 32
        call putchar@PLT
        leave
        ret
lut_ecrire_ligne:
        push rbp
        mov rbp, rsp
        mov edi, 10
        call putchar@PLT
        leave
        ret
lut_tableau:                    # rdi = taille, esi = ligne ; renvoie l'adresse de la case 0
        push rbp
        mov rbp, rsp
        push rbx
        sub rsp, 8
        test rdi, rdi
        js .Ltaille_negative
        mov rbx, rdi
        lea rdi, [rdi+1]
        mov esi, 8
        call calloc@PLT         # une case de plus, devant : la taille du tableau
        test rax, rax
        jz .Lplus_de_memoire
        mov [rax], rbx
        add rax, 8
        mov rbx, [rbp-8]
        leave
        ret
.Ltaille_negative:
        mov edi, esi
        lea rsi, .Lmsg_taille[rip]
        jmp lut_erreur
.Lplus_de_memoire:
        mov edi, 0
        lea rsi, .Lmsg_memoire[rip]
        jmp lut_erreur
lut_division_par_zero:          # edi = numéro de ligne
        lea rsi, .Lmsg_division[rip]
        jmp lut_erreur
lut_hors_bornes:
        lea rsi, .Lmsg_bornes[rip]
        jmp lut_erreur
lut_erreur:                     # edi = ligne, rsi = message
        and rsp, -16
        mov ebx, edi            # on ne reviendra pas : pas besoin de sauver rbx et r12
        mov r12, rsi
        xor edi, edi
        call fflush@PLT         # d'abord ce que le programme a déjà affiché
        mov edx, ebx
        mov rcx, r12
        mov rdi, [rip+stderr]
        lea rsi, .Lformat_erreur[rip]
        xor eax, eax
        call fprintf@PLT
        mov edi, 1
        call exit@PLT

        .section .rodata
.Lformat_entier: .string "%ld"
.Lformat_texte:  .string "%s"
.Lformat_erreur: .string "erreur à la ligne %d : %s\n"
.Lmsg_division:  .string "division par zéro"
.Lmsg_bornes:    .string "indice en dehors du tableau"
.Lmsg_taille:    .string "taille de tableau négative"
.Lmsg_memoire:   .string "plus de mémoire"
"""


def compile_source(source):
    fonctions, principal = Analyseur(lexique(source)).programme()
    fonctions = [plie(f) for f in fonctions]
    principal = [plie(i) for i in principal]
    noms = [f[2] for f in fonctions]
    for f in fonctions:
        if noms.count(f[2]) > 1:
            raise ErreurLutece(f"la fonction « {f[2]} » est définie deux fois", *f[1])
        if len(set(f[3])) != len(f[3]):
            raise ErreurLutece(f"paramètre en double dans « {f[2]} »", *f[1])
    g = Generateur(fonctions)
    g.lignes += ["        .intel_syntax noprefix", "        .text"]
    g.fonction("principal", [], principal, principale=True)
    for f in fonctions:
        g.fonction(f[2], f[3], f[4])
    g.lignes.append(EXECUTION)
    if g.textes:
        g.lignes.append("        .section .rodata")
        for s, etiq in g.textes.items():
            echappe = "".join(chr(b) if 32 <= b < 127 and b not in (34, 92) else f"\\{b:03o}" for b in s.encode("utf-8"))
            g.lignes.append(f'{etiq}: .string "{echappe}"')
    g.lignes.append('        .section .note.GNU-stack,"",@progbits')
    return "\n".join(g.lignes) + "\n"


def affiche_erreur(chemin, source, e):
    print(f"{chemin}:{e.ligne}:{e.colonne}: erreur : {e}", file=sys.stderr)
    if e.ligne and 1 <= e.ligne <= len(source.splitlines()):
        ligne = source.splitlines()[e.ligne - 1]
        print(f"  {ligne}", file=sys.stderr)
        print(f"  {' ' * ((e.colonne or 1) - 1)}^", file=sys.stderr)


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--aide"):
        print(__doc__)
        return 0
    chemin = args[0]
    sortie = args[args.index("-o") + 1] if "-o" in args else None
    source = open(chemin, encoding="utf-8").read()
    try:
        asm = compile_source(source)
    except ErreurLutece as e:
        affiche_erreur(chemin, source, e)
        return 1
    if "-S" in args:
        print(asm)
        return 0
    with tempfile.TemporaryDirectory() as d:
        fichier_s = os.path.join(d, "prog.s")
        with open(fichier_s, "w") as f:
            f.write(asm)
        exe = sortie or os.path.join(d, "prog")
        r = subprocess.run(["gcc", "-o", exe, fichier_s], capture_output=True, text=True)
        if r.returncode:
            print("gcc a refusé l'assembleur produit (bug du compilateur) :\n" + r.stderr, file=sys.stderr)
            return 2
        if sortie:
            print(f"-> {sortie}", file=sys.stderr)
            return 0
        return subprocess.run([exe]).returncode


if __name__ == "__main__":
    sys.exit(main())
