"""
Portal de propostas/projetos para clientes da Leanttro
"""

from flask import (
    Flask, render_template, request, jsonify,
    redirect, session, g, abort, url_for, Response
)
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
import io
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from functools import wraps
from datetime import datetime, timedelta
import psycopg2
import psycopg2.extras
import os
import glob
import secrets
import string
import requests
import json
import unicodedata
import re
from dotenv import load_dotenv

# ── Imports do módulo de métricas premium ────────────────────
try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build as google_build
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import (
        RunReportRequest, DateRange, Metric, Dimension, OrderBy
    )
    import google.auth.exceptions
    GOOGLE_LIBS_OK = True
except ImportError:
    GOOGLE_LIBS_OK = False
    print("⚠️  Módulo de métricas: libs Google não instaladas. "
          "Rode: pip install google-auth google-auth-oauthlib "
          "google-api-python-client google-analytics-data")

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "leanttro_portal_secret_troque_em_producao")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.url_map.strict_slashes = False

# ── Configs ───────────────────────────────────────────────────
GROQ_API_KEY      = os.getenv("GROQ_API_KEY", "")
GROQ_API_URL      = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL        = "llama-3.3-70b-versatile"
GOOGLE_CLIENT_ID  = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SEC = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT   = os.getenv("GOOGLE_REDIRECT_URI", "https://portal.leanttro.com/api/metricas/oauth/callback")
BASE_URL          = os.getenv("BASE_URL", "http://localhost:5002")

# Escopos OAuth do módulo de métricas
SCOPES_GSC = ["https://www.googleapis.com/auth/webmasters.readonly"]
SCOPES_GA4 = ["https://www.googleapis.com/auth/analytics.readonly"]
SCOPES_ALL = SCOPES_GSC + SCOPES_GA4

# ═══════════════════════════════════════════════════════════
#  BANCO
# ═══════════════════════════════════════════════════════════

def get_db():
    if "db" not in g:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError(
                "DATABASE_URL nao configurada. "
                "Defina a variavel de ambiente DATABASE_URL antes de iniciar o servidor."
            )
        g.db = psycopg2.connect(
            dsn=database_url,
            cursor_factory=psycopg2.extras.RealDictCursor
        )
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        try: db.close()
        except: pass

def query(sql, params=(), one=False, commit=False):
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(sql, params)
        if commit:
            db.commit()
            try:    return cur.fetchone()
            except: return None
        return cur.fetchone() if one else cur.fetchall()
    except Exception as e:
        if commit:
            db.rollback()
        raise e

def gerar_token(n=14):
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(n))

def gerar_slug(texto):
    texto = unicodedata.normalize('NFKD', texto)
    texto = texto.encode('ascii', 'ignore').decode('ascii')
    texto = texto.lower().strip()
    texto = re.sub(r'[^a-z0-9\s-]', '', texto)
    texto = re.sub(r'[\s_-]+', '-', texto)
    return texto.strip('-')[:80]

def gerar_slug_unico(base):
    """Gera slug único verificando colisões no banco  clientes."""
    slug = gerar_slug(base)
    if not slug:
        slug = 'cliente'
    candidato = slug
    i = 2
    while query('SELECT id FROM clientes WHERE slug = %s', (candidato,), one=True):
        candidato = f'{slug}-{i}'
        i += 1
    return candidato

def gerar_slug_proposta(titulo, cliente_nome=None, excluir_id=None):
    """Gera slug bonito para proposta baseado no título + primeiro nome do cliente.
    Ex: 'Site João' -> 'site-joao', colisão -> 'site-joao-2'
    """
    base = titulo
    if cliente_nome:
        primeiro = cliente_nome.strip().split()[0]
        base = f"{titulo} {primeiro}"
    slug = gerar_slug(base)
    if not slug:
        slug = 'proposta'
    candidato = slug
    i = 2
    while True:
        q = "SELECT id FROM propostas WHERE slug = %s"
        params = [candidato]
        if excluir_id:
            q += " AND id != %s"
            params.append(excluir_id)
        if not query(q, params, one=True):
            break
        candidato = f"{slug}-{i}"
        i += 1
    return candidato

# ═══════════════════════════════════════════════════════════
#  AUTH
# ═══════════════════════════════════════════════════════════

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_id"):
            return redirect("/admin/login")
        return f(*args, **kwargs)
    return decorated

# ═══════════════════════════════════════════════════════════
#  TEMPLATES DINÂMICOS DO PORTAL
#  Ficam em templates/portal/
#  Nomeados como: portal_moderno.html, portal_dark.html, etc.
#  O admin escolhe qual usar por proposta
# ═══════════════════════════════════════════════════════════

def listar_templates_portal():
    pasta = os.path.join(app.root_path, "templates", "portal")
    arquivos = glob.glob(os.path.join(pasta, "portal_*.html"))
    slugs = [os.path.basename(f).replace(".html", "") for f in sorted(arquivos)]
    return slugs if slugs else ["portal_padrao"]

# ═══════════════════════════════════════════════════════════
#  GROQ  IA
# ═══════════════════════════════════════════════════════════

def groq_chat(system_prompt, user_prompt, max_tokens=600):
    if not GROQ_API_KEY:
        return None, "GROQ_API_KEY não configurada"
    try:
        r = requests.post(
            GROQ_API_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt}
                ],
                "temperature": 0.6,
                "max_tokens": max_tokens
            },
            timeout=20
        )
        data = r.json()
        return data["choices"][0]["message"]["content"].strip(), None
    except Exception as e:
        return None, str(e)

# ═══════════════════════════════════════════════════════════
#  ROTAS  AUTH ADMIN
# ═══════════════════════════════════════════════════════════

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin_id"):
        return redirect("/admin")
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        senha = request.form.get("senha", "")
        user  = query("SELECT * FROM usuarios WHERE email = %s", (email,), one=True)
        if not user or not check_password_hash(user["senha_hash"], senha):
            return render_template("admin/login.html", erro="E-mail ou senha incorretos")
        session["admin_id"]   = user["id"]
        session["admin_nome"] = user["nome"]
        session.permanent     = True
        app.permanent_session_lifetime = timedelta(days=30)
        return redirect("/admin")
    return render_template("admin/login.html")

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect("/admin/login")

# ═══════════════════════════════════════════════════════════
#  ROTAS  PAINEL ADMIN
# ═══════════════════════════════════════════════════════════

@app.route("/admin")
@login_required
def admin_dashboard():
    stats = {
        "total_propostas": (query("SELECT COUNT(*) as n FROM propostas", one=True) or {}).get("n", 0),
        "total_clientes":  (query("SELECT COUNT(*) as n FROM clientes",  one=True) or {}).get("n", 0),
        "em_andamento":    (query("SELECT COUNT(*) as n FROM propostas WHERE status='em_andamento'", one=True) or {}).get("n", 0),
        "vistas":          (query("SELECT COUNT(*) as n FROM propostas WHERE status IN ('vista','em_andamento','concluido')", one=True) or {}).get("n", 0),
    }
    recentes = query("""
        SELECT p.*, c.nome as cliente_nome, c.empresa as cliente_empresa
        FROM propostas p JOIN clientes c ON c.id = p.cliente_id
        ORDER BY p.criado_em DESC LIMIT 6
    """) or []
    mensagens_novas = query("""
        SELECT COUNT(*) as n FROM mensagens WHERE remetente = 'cliente' AND lida = false
    """, one=True)
    templates = listar_templates_portal()
    return render_template("admin/index.html",
        stats=dict(stats),
        recentes=[dict(r) for r in recentes],
        mensagens_novas=(mensagens_novas or {}).get("n", 0),
        templates=templates,
        admin_nome=session.get("admin_nome")
    )

# ═══════════════════════════════════════════════════════════
#  API  CLIENTES
# ═══════════════════════════════════════════════════════════

@app.route("/api/clientes")
@login_required
def api_clientes_listar():
    clientes = query("""
        SELECT c.*, COUNT(p.id) as total_propostas
        FROM clientes c
        LEFT JOIN propostas p ON p.cliente_id = c.id
        GROUP BY c.id ORDER BY c.nome
    """) or []
    return jsonify([dict(c) for c in clientes])

@app.route("/api/clientes", methods=["POST"])
@login_required
def api_cliente_criar():
    d = request.json or {}
    if not d.get("nome"):
        return jsonify({"erro": "Nome obrigatório"}), 400
    # Gera slug baseado no nome/empresa e token único de acesso
    base = d.get("empresa") or d["nome"]
    slug = d.get("slug") or gerar_slug_unico(base)
    token_cliente = gerar_token(20)
    row = query(
        "INSERT INTO clientes (nome, empresa, email, whatsapp, slug, token_cliente, logo_url) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (d["nome"], d.get("empresa"), d.get("email"), d.get("whatsapp"), slug, token_cliente, d.get("logo_url")), commit=True
    )
    return jsonify({"ok": True, "id": row["id"] if row else None, "slug": slug, "token_cliente": token_cliente})

@app.route("/api/clientes/<int:cid>", methods=["GET"])
@login_required
def api_cliente_get(cid):
    c = query("SELECT * FROM clientes WHERE id = %s", (cid,), one=True)
    if not c: return jsonify({"erro": "não encontrado"}), 404
    return jsonify(dict(c))

@app.route("/api/clientes/<int:cid>", methods=["PUT"])
@login_required
def api_cliente_editar(cid):
    d = request.json or {}
    # Atualiza slug se nome/empresa mudar, verificando unicidade (excluindo o próprio cliente)
    atual = query("SELECT slug, nome, empresa FROM clientes WHERE id=%s", (cid,), one=True)
    base = d.get("empresa") or d["nome"]
    if d.get("slug"):
        novo_slug = d["slug"]
        # Verifica unicidade do slug informado manualmente
        conflito = query("SELECT id FROM clientes WHERE slug=%s AND id != %s", (novo_slug, cid), one=True)
        if conflito:
            return jsonify({"erro": "Slug já em uso por outro cliente"}), 409
    elif atual and (d["nome"] != atual["nome"] or d.get("empresa") != atual["empresa"]):
        base_slug = gerar_slug(base)
        candidato = base_slug
        i = 2
        while query("SELECT id FROM clientes WHERE slug=%s AND id != %s", (candidato, cid), one=True):
            candidato = f"{base_slug}-{i}"
            i += 1
        novo_slug = candidato
    else:
        novo_slug = atual["slug"] if atual else gerar_slug_unico(base)
    query("UPDATE clientes SET nome=%s, empresa=%s, email=%s, whatsapp=%s, slug=%s, logo_url=%s WHERE id=%s",
          (d["nome"], d.get("empresa"), d.get("email"), d.get("whatsapp"), novo_slug, d.get("logo_url"), cid), commit=True)
    return jsonify({"ok": True, "slug": novo_slug})

@app.route("/api/clientes/<int:cid>", methods=["DELETE"])
@login_required
def api_cliente_deletar(cid):
    query("DELETE FROM clientes WHERE id = %s", (cid,), commit=True)
    return jsonify({"ok": True})

@app.route("/api/clientes/<int:cid>/pin", methods=["GET"])
@login_required
def api_cliente_pin_get(cid):
    """Retorna o PIN atual do cliente (só para o admin)."""
    c = query("SELECT pin_acesso FROM clientes WHERE id=%s", (cid,), one=True)
    if not c:
        return jsonify({"erro": "não encontrado"}), 404
    return jsonify({"pin": c["pin_acesso"] or ""})

@app.route("/api/clientes/<int:cid>/pin", methods=["PUT"])
@login_required
def api_cliente_pin_set(cid):
    """Define ou remove o PIN de acesso do cliente."""
    d = request.json or {}
    pin = str(d.get("pin", "")).strip()
    if pin and (not pin.isdigit() or len(pin) != 4):
        return jsonify({"erro": "PIN deve ter exatamente 4 dígitos numéricos"}), 400
    query("UPDATE clientes SET pin_acesso=%s WHERE id=%s",
          (pin or None, cid), commit=True)
    return jsonify({"ok": True, "pin": pin or None})

# ═══════════════════════════════════════════════════════════
#  API  PROPOSTAS
# ═══════════════════════════════════════════════════════════

@app.route("/api/propostas")
@login_required
def api_propostas_listar():
    ps = query("""
        SELECT p.*, c.nome as cliente_nome, c.empresa as cliente_empresa,
               c.whatsapp as cliente_whatsapp
        FROM propostas p JOIN clientes c ON c.id = p.cliente_id
        ORDER BY p.criado_em DESC
    """) or []
    return jsonify([dict(p) for p in ps])

@app.route("/api/propostas", methods=["POST"])
@login_required
def api_proposta_criar():
    d = request.json or {}
    if not d.get("cliente_id") or not d.get("titulo"):
        return jsonify({"erro": "cliente_id e titulo obrigatórios"}), 400
    token = gerar_token()
    cliente = query("SELECT nome FROM clientes WHERE id=%s", (d["cliente_id"],), one=True)
    slug = gerar_slug_proposta(d["titulo"], cliente["nome"] if cliente else None)
    row = query("""
        INSERT INTO propostas
            (cliente_id, titulo, descricao, validade, prazo_entrega,
             forma_pagamento, status, cor_tema, mensagem_final, token, template, slug)
        VALUES (%s,%s,%s,%s,%s,%s,'rascunho',%s,%s,%s,%s,%s) RETURNING id, token, slug
    """, (
        d["cliente_id"], d["titulo"], d.get("descricao"),
        d.get("validade") or None, d.get("prazo_entrega") or None,
        d.get("forma_pagamento"), d.get("cor_tema", "#c17f3a"),
        d.get("mensagem_final"), token, d.get("template", "portal_padrao"), slug
    ), commit=True)
    return jsonify({"ok": True, "id": row["id"], "token": row["token"], "slug": row["slug"]})

@app.route("/api/propostas/<int:pid>", methods=["GET"])
@login_required
def api_proposta_get(pid):
    p = query("""
        SELECT p.*, c.nome as cliente_nome, c.empresa as cliente_empresa,
               c.email as cliente_email, c.whatsapp as cliente_whatsapp
        FROM propostas p JOIN clientes c ON c.id = p.cliente_id
        WHERE p.id = %s
    """, (pid,), one=True)
    if not p: return jsonify({"erro": "não encontrada"}), 404
    data = dict(p)
    data["servicos"]  = [dict(s) for s in (query("SELECT * FROM servicos  WHERE proposta_id=%s ORDER BY ordem", (pid,)) or [])]
    data["tarefas"]   = [dict(t) for t in (query("SELECT * FROM tarefas   WHERE proposta_id=%s ORDER BY ordem", (pid,)) or [])]
    data["midias"]    = [dict(m) for m in (query("SELECT * FROM midias    WHERE proposta_id=%s ORDER BY ordem", (pid,)) or [])]
    data["links"]     = [dict(l) for l in (query("SELECT * FROM links     WHERE proposta_id=%s ORDER BY ordem", (pid,)) or [])]
    data["mensagens"] = [dict(m) for m in (query("SELECT * FROM mensagens WHERE proposta_id=%s ORDER BY criado_em", (pid,)) or [])]
    data["metricas"]  = [dict(m) for m in (query("SELECT * FROM metricas  WHERE proposta_id=%s ORDER BY mes_ano DESC", (pid,)) or [])]
    # Contrato
    contrato = query("SELECT contrato_texto, contrato_tipo, contrato_duracao, contrato_assinado, contrato_assinado_em, contrato_ip, contrato_nome_assinou FROM propostas WHERE id=%s", (pid,), one=True)
    if contrato:
        data["contrato_texto"]        = contrato["contrato_texto"]
        data["contrato_tipo"]         = contrato["contrato_tipo"]
        data["contrato_duracao"]      = contrato["contrato_duracao"]
        data["contrato_assinado"]     = contrato["contrato_assinado"]
        data["contrato_assinado_em"]  = contrato["contrato_assinado_em"].isoformat() if contrato["contrato_assinado_em"] else None
        data["contrato_ip"]           = contrato["contrato_ip"]
        data["contrato_nome_assinou"] = contrato["contrato_nome_assinou"]
    # Serializa datas pra string
    for k, v in data.items():
        if hasattr(v, 'isoformat'):
            data[k] = v.isoformat()
    return jsonify(data)

@app.route("/api/propostas/<int:pid>", methods=["PUT"])
@login_required
def api_proposta_editar(pid):
    d = request.json or {}
    # Regenera slug se o título mudou
    atual = query("SELECT titulo, slug FROM propostas WHERE id=%s", (pid,), one=True)
    if atual and d["titulo"] != atual["titulo"]:
        cliente = query("SELECT nome FROM clientes WHERE id=%s", (d["cliente_id"],), one=True)
        novo_slug = gerar_slug_proposta(d["titulo"], cliente["nome"] if cliente else None, excluir_id=pid)
    else:
        novo_slug = atual["slug"] if atual and atual["slug"] else gerar_slug_proposta(d["titulo"], excluir_id=pid)
    query("""
        UPDATE propostas SET
            cliente_id=%s, titulo=%s, descricao=%s,
            validade=%s, prazo_entrega=%s, forma_pagamento=%s,
            status=%s, cor_tema=%s, mensagem_final=%s,
            template=%s, slug=%s, atualizado_em=NOW()
        WHERE id=%s
    """, (
        d["cliente_id"], d["titulo"], d.get("descricao"),
        d.get("validade") or None, d.get("prazo_entrega") or None,
        d.get("forma_pagamento"), d.get("status", "rascunho"),
        d.get("cor_tema", "#c17f3a"), d.get("mensagem_final"),
        d.get("template", "portal_padrao"), novo_slug, pid
    ), commit=True)

    # Reescreve serviços
    query("DELETE FROM servicos WHERE proposta_id=%s", (pid,), commit=True)
    for i, s in enumerate(d.get("servicos", [])):
        if s.get("nome"):
            query("""INSERT INTO servicos
                (proposta_id,nome,descricao,categoria,quantidade,valor_unit,recorrente,periodo,ordem)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (pid, s["nome"], s.get("descricao"), s.get("categoria","outro"),
                 s.get("quantidade",1), s.get("valor_unit",0),
                 s.get("recorrente",False), s.get("periodo","unico"), i), commit=True)

    # Reescreve tarefas
    query("DELETE FROM tarefas WHERE proposta_id=%s", (pid,), commit=True)
    for i, t in enumerate(d.get("tarefas", [])):
        if t.get("titulo"):
            query("""INSERT INTO tarefas
                (proposta_id,titulo,instrucao,responsavel,tipo_entrega,status,ordem)
                VALUES (%s,%s,%s,%s,%s,'pendente',%s)""",
                (pid, t["titulo"], t.get("instrucao"), t.get("responsavel","eu"),
                 t.get("tipo_entrega","confirmacao"), i), commit=True)

    # Reescreve mídias
    query("DELETE FROM midias WHERE proposta_id=%s", (pid,), commit=True)
    for i, m in enumerate(d.get("midias", [])):
        if m.get("url"):
            query("INSERT INTO midias (proposta_id,url,legenda,tipo,ordem) VALUES (%s,%s,%s,%s,%s)",
                  (pid, m["url"], m.get("legenda"), m.get("tipo","portfolio"), i), commit=True)

    # Reescreve links
    query("DELETE FROM links WHERE proposta_id=%s", (pid,), commit=True)
    for i, l in enumerate(d.get("links", [])):
        if l.get("url") and l.get("titulo"):
            query("INSERT INTO links (proposta_id,titulo,url,tipo,ordem) VALUES (%s,%s,%s,%s,%s)",
                  (pid, l["titulo"], l["url"], l.get("tipo","referencia"), i), commit=True)

    return jsonify({"ok": True})

@app.route("/api/propostas/<int:pid>", methods=["DELETE"])
@login_required
def api_proposta_deletar(pid):
    query("DELETE FROM propostas WHERE id=%s", (pid,), commit=True)
    return jsonify({"ok": True})

@app.route("/api/propostas/<int:pid>/duplicar", methods=["POST"])
@login_required
def api_proposta_duplicar(pid):
    p = query("SELECT * FROM propostas WHERE id=%s", (pid,), one=True)
    if not p: return jsonify({"erro": "não encontrada"}), 404
    token = gerar_token()
    titulo_copia = f"[Cópia] {p['titulo']}"
    cliente = query("SELECT nome FROM clientes WHERE id=%s", (p["cliente_id"],), one=True)
    slug_copia = gerar_slug_proposta(titulo_copia, cliente["nome"] if cliente else None)
    row = query("""
        INSERT INTO propostas
            (cliente_id,titulo,descricao,validade,prazo_entrega,
             forma_pagamento,cor_tema,mensagem_final,token,status,slug)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'rascunho',%s) RETURNING id
    """, (p["cliente_id"], titulo_copia, p["descricao"],
          p["validade"], p["prazo_entrega"], p["forma_pagamento"],
          p["cor_tema"], p["mensagem_final"], token, slug_copia), commit=True)
    novo_id = row["id"]
    for s in (query("SELECT * FROM servicos WHERE proposta_id=%s ORDER BY ordem", (pid,)) or []):
        query("""INSERT INTO servicos
            (proposta_id,nome,descricao,categoria,quantidade,valor_unit,recorrente,periodo,ordem)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (novo_id,s["nome"],s["descricao"],s["categoria"],
             s["quantidade"],s["valor_unit"],s["recorrente"],s["periodo"],s["ordem"]), commit=True)
    for t in (query("SELECT * FROM tarefas WHERE proposta_id=%s ORDER BY ordem", (pid,)) or []):
        query("""INSERT INTO tarefas
            (proposta_id,titulo,instrucao,responsavel,tipo_entrega,status,ordem)
            VALUES (%s,%s,%s,%s,%s,'pendente',%s)""",
            (novo_id,t["titulo"],t["instrucao"],t["responsavel"],t["tipo_entrega"],t["ordem"]), commit=True)
    return jsonify({"ok": True, "id": novo_id, "token": token})

# ═══════════════════════════════════════════════════════════
#  API  TAREFAS (atualização de status pelo cliente/admin)
# ═══════════════════════════════════════════════════════════

@app.route("/api/tarefas/<int:tid>/resposta", methods=["POST"])
def api_tarefa_resposta(tid):
    """Cliente entrega uma tarefa  não precisa de login admin"""
    d = request.json or {}
    token_proposta = d.get("token")
    # Verifica que a tarefa pertence a essa proposta
    tarefa = query("""
        SELECT t.* FROM tarefas t
        JOIN propostas p ON p.id = t.proposta_id
        WHERE t.id = %s AND p.token = %s
    """, (tid, token_proposta), one=True)
    if not tarefa: return jsonify({"erro": "não encontrada"}), 404
    query("""UPDATE tarefas SET resposta=%s, status='entregue', atualizado_em=NOW()
             WHERE id=%s""",
          (d.get("resposta"), tid), commit=True)
    return jsonify({"ok": True})

@app.route("/api/tarefas/<int:tid>/status", methods=["POST"])
@login_required
def api_tarefa_status(tid):
    """Admin aprova/recusa entrega do cliente ou marca sua própria tarefa"""
    d = request.json or {}
    query("UPDATE tarefas SET status=%s, atualizado_em=NOW() WHERE id=%s",
          (d.get("status", "pendente"), tid), commit=True)
    return jsonify({"ok": True})

# ═══════════════════════════════════════════════════════════
#  API  MENSAGENS
# ═══════════════════════════════════════════════════════════

@app.route("/api/mensagens/<int:pid>", methods=["POST"])
def api_mensagem_criar(pid):
    d = request.json or {}
    token = d.get("token")
    remetente = "admin"
    # Se tem token, é o cliente enviando
    if token:
        p = query("SELECT id FROM propostas WHERE token=%s AND id=%s", (token, pid), one=True)
        if not p: return jsonify({"erro": "não autorizado"}), 403
        remetente = "cliente"
    elif not session.get("admin_id"):
        return jsonify({"erro": "não autorizado"}), 403

    if not d.get("texto"):
        return jsonify({"erro": "texto obrigatório"}), 400

    row = query("""
        INSERT INTO mensagens (proposta_id, remetente, texto)
        VALUES (%s,%s,%s) RETURNING id, criado_em
    """, (pid, remetente, d["texto"]), commit=True)
    return jsonify({"ok": True, "id": row["id"] if row else None})

@app.route("/api/mensagens/<int:pid>/lidas", methods=["POST"])
@login_required
def api_mensagens_marcar_lidas(pid):
    query("UPDATE mensagens SET lida=true WHERE proposta_id=%s AND remetente='cliente'",
          (pid,), commit=True)
    return jsonify({"ok": True})

# ═══════════════════════════════════════════════════════════
#  API  MÉTRICAS (entrada manual por proposta  existente)
# ═══════════════════════════════════════════════════════════

@app.route("/api/metricas/<int:pid>", methods=["POST"])
@login_required
def api_metrica_salvar(pid):
    d = request.json or {}
    mes_ano = d.get("mes_ano")
    if not mes_ano: return jsonify({"erro": "mes_ano obrigatório"}), 400
    # UPSERT
    query("""
        INSERT INTO metricas
            (proposta_id, mes_ano, impressoes, cliques, posicao_media,
             usuarios, sessoes, pageviews,
             negocios_cadastrados, acessos_hub, negocios_mais_vistos, observacao)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (proposta_id, mes_ano) DO UPDATE SET
            impressoes=%s, cliques=%s, posicao_media=%s,
            usuarios=%s, sessoes=%s, pageviews=%s,
            negocios_cadastrados=%s, acessos_hub=%s,
            negocios_mais_vistos=%s, observacao=%s
    """, (
        pid, mes_ano,
        d.get("impressoes",0), d.get("cliques",0), d.get("posicao_media",0),
        d.get("usuarios",0), d.get("sessoes",0), d.get("pageviews",0),
        d.get("negocios_cadastrados",0), d.get("acessos_hub",0),
        json.dumps(d.get("negocios_mais_vistos",[])), d.get("observacao"),
        # UPDATE
        d.get("impressoes",0), d.get("cliques",0), d.get("posicao_media",0),
        d.get("usuarios",0), d.get("sessoes",0), d.get("pageviews",0),
        d.get("negocios_cadastrados",0), d.get("acessos_hub",0),
        json.dumps(d.get("negocios_mais_vistos",[])), d.get("observacao")
    ), commit=True)
    return jsonify({"ok": True})

@app.route("/api/metricas/<int:pid>/sync", methods=["POST"])
@login_required
def api_metrica_sync_google(pid):
    """Puxa dados do GA4 e GSC pra essa proposta usando as credenciais salvas"""
    cred = query("SELECT * FROM credenciais_google WHERE proposta_id=%s", (pid,), one=True)
    if not cred:
        return jsonify({"erro": "Credenciais Google não configuradas para este cliente"}), 400
    # TODO: implementar chamada real ao GA4/GSC com os tokens salvos
    # Por enquanto retorna instrução
    return jsonify({
        "ok": False,
        "info": "Configure as credenciais OAuth para sincronização automática",
        "configured": bool(cred.get("access_token"))
    })

# ═══════════════════════════════════════════════════════════
#  API  IA (GROQ)
# ═══════════════════════════════════════════════════════════

@app.route("/api/ia/gerar-proposta", methods=["POST"])
@login_required
def api_ia_gerar_proposta():
    d = request.json or {}
    servicos = d.get("servicos", "")
    cliente  = d.get("cliente", "")
    tipo     = d.get("tipo", "site")

    texto, erro = groq_chat(
        "Você é um assistente da Leanttro, agência de tecnologia em SP. "
        "Escreva textos de proposta comercial diretos, profissionais e persuasivos. "
        "Sempre em português do Brasil. Sem enrolação.",
        f"Escreva uma descrição de proposta comercial para o cliente '{cliente}' "
        f"para os seguintes serviços: {servicos}. Tipo do projeto: {tipo}. "
        f"Seja direto e profissional. Máximo 3 parágrafos curtos.",
        max_tokens=400
    )
    if erro: return jsonify({"erro": erro}), 500
    return jsonify({"texto": texto})

@app.route("/api/ia/sugerir-tarefas", methods=["POST"])
@login_required
def api_ia_sugerir_tarefas():
    d = request.json or {}
    servicos = d.get("servicos", "")
    tipo     = d.get("tipo", "site")

    texto, erro = groq_chat(
        "Você é um gerente de projetos da Leanttro. "
        "Retorne APENAS um JSON válido, sem texto extra, sem markdown.",
        f"Para um projeto de '{servicos}' (tipo: {tipo}), liste as tarefas necessárias. "
        f"Retorne JSON com duas listas: "
        f"'minhas_tarefas' (o que eu faço) e 'tarefas_cliente' (o que o cliente precisa entregar). "
        f"Cada item: {{titulo, instrucao}}. Máximo 5 por lista.",
        max_tokens=600
    )
    if erro: return jsonify({"erro": erro}), 500
    try:
        # Remove possíveis backticks de markdown
        texto_limpo = texto.strip().strip("```json").strip("```").strip()
        data = json.loads(texto_limpo)
        return jsonify(data)
    except:
        return jsonify({"erro": "IA retornou formato inválido", "raw": texto}), 500

@app.route("/api/ia/gerar-mensagem", methods=["POST"])
@login_required
def api_ia_gerar_mensagem():
    d = request.json or {}
    contexto  = d.get("contexto", "")
    tom       = d.get("tom", "profissional e amigável")
    cliente   = d.get("cliente", "")

    texto, erro = groq_chat(
        "Você é o Leandro da Leanttro. Escreva mensagens diretas, sem enrolação, "
        "em português do Brasil. Sem saudações formais excessivas.",
        f"Escreva uma mensagem para o cliente '{cliente}'. Contexto: {contexto}. "
        f"Tom: {tom}. Máximo 5 linhas.",
        max_tokens=250
    )
    if erro: return jsonify({"erro": erro}), 500
    return jsonify({"texto": texto})

@app.route("/api/ia/gerar-contrato", methods=["POST"])
@login_required
def api_ia_gerar_contrato():
    """Gera texto de contrato via Groq com base nos dados da proposta"""
    d = request.json or {}
    cliente_nome    = d.get("cliente_nome", "")
    cliente_empresa = d.get("cliente_empresa", "")
    servicos        = d.get("servicos", "")
    valor           = d.get("valor", "0")
    forma_pgto      = d.get("forma_pagamento", "")
    duracao         = d.get("duracao", "")
    tipo            = d.get("tipo", "pontual")  # 'pontual' ou 'recorrente'
    prazo           = d.get("prazo_entrega", "")

    tipo_label = "prestação de serviço pontual" if tipo == "pontual" else "prestação de serviço recorrente/mensal"

    prompt = f"""Gere um contrato de {tipo_label} com as seguintes informações:

CONTRATANTE: {cliente_nome}{f'  {cliente_empresa}' if cliente_empresa else ''}
CONTRATADA: Leanttro Tecnologia — CNPJ 63.556.406/0001-75 — São Paulo/SP
SERVIÇOS: {servicos}
VALOR: R$ {valor}{f' — {forma_pgto}' if forma_pgto else ''}
{'DURAÇÃO: ' + duracao if duracao else ''}
{'PRAZO DE ENTREGA: ' + prazo if prazo else ''}

Escreva um contrato profissional e direto, com as seguintes cláusulas:
1. Das Partes
2. Do Objeto (serviços contratados detalhados)
3. Do Valor e Forma de Pagamento
4. {'Do Prazo de Entrega' if tipo == 'pontual' else 'Da Vigência e Renovação'}
5. Das Obrigações da Contratada
6. Das Obrigações do Contratante
7. Da Propriedade Intelectual
8. Da Rescisão
9. Das Disposições Gerais (foro: São Paulo/SP)

Linguagem: clara, direta, válida no Brasil. Sem formatação markdown, apenas texto corrido com títulos em MAIÚSCULO."""

    texto, erro = groq_chat(
        "Você é um especialista jurídico brasileiro. Escreva contratos de prestação de serviço claros, diretos e com validade legal. Sempre em português do Brasil.",
        prompt,
        max_tokens=2000
    )
    if erro:
        return jsonify({"erro": erro}), 500
    return jsonify({"texto": texto})

@app.route("/api/ia/analisar-metricas", methods=["POST"])
@login_required
def api_ia_analisar_metricas():
    d = request.json or {}
    metricas = d.get("metricas", [])
    cliente  = d.get("cliente", "")
    if not metricas:
        return jsonify({"erro": "Sem métricas para analisar"}), 400

    resumo = "\n".join([
        f"Mês {m.get('mes_ano')}: "
        f"{m.get('impressoes',0)} impressões, {m.get('cliques',0)} cliques, "
        f"posição média {m.get('posicao_media',0)}, "
        f"{m.get('usuarios',0)} usuários, {m.get('sessoes',0)} sessões"
        for m in metricas[-6:]  # últimos 6 meses
    ])

    texto, erro = groq_chat(
        "Você é um especialista em SEO e marketing digital da Leanttro. "
        "Analise métricas e dê um diagnóstico direto e acionável em português do Brasil.",
        f"Cliente: {cliente}\nMétricas dos últimos meses:\n{resumo}\n\n"
        f"Dê um diagnóstico em 3-4 frases: o que cresceu, o que precisa melhorar "
        f"e 1 ação concreta para o próximo mês.",
        max_tokens=350
    )
    if erro: return jsonify({"erro": erro}), 500
    return jsonify({"analise": texto})

@app.route("/api/ia/resumir-cliente", methods=["POST"])
@login_required
def api_ia_resumir_cliente():
    d = request.json or {}
    pid = d.get("proposta_id")
    if not pid: return jsonify({"erro": "proposta_id obrigatório"}), 400

    p   = query("SELECT p.*, c.nome as cliente_nome FROM propostas p JOIN clientes c ON c.id=p.cliente_id WHERE p.id=%s", (pid,), one=True)
    msgs = query("SELECT remetente, texto, criado_em FROM mensagens WHERE proposta_id=%s ORDER BY criado_em DESC LIMIT 10", (pid,)) or []
    tarefas = query("SELECT titulo, responsavel, status FROM tarefas WHERE proposta_id=%s", (pid,)) or []

    contexto = f"Projeto: {p['titulo'] if p else ''}\n"
    contexto += f"Status: {p['status'] if p else ''}\n"
    tarefas_resumo = ", ".join([f"{t['titulo']}({t['status']})" for t in tarefas])
    contexto += f"Tarefas: {tarefas_resumo}\n"
    contexto += f"Últimas mensagens: {' | '.join([m['texto'][:80] for m in msgs])}"

    texto, erro = groq_chat(
        "Você é assistente da Leanttro. Resuma o estado atual do projeto de forma concisa.",
        f"Resuma em 3-4 frases o estado atual deste projeto:\n{contexto}",
        max_tokens=300
    )
    if erro: return jsonify({"erro": erro}), 500
    return jsonify({"resumo": texto})

@app.route("/api/propostas/<int:pid>/assinar-contrato", methods=["POST"])
def api_assinar_contrato(pid):
    """Cliente assina o contrato — não requer login admin"""
    d = request.json or {}
    token = d.get("token", "")
    nome  = d.get("nome", "").strip()

    # Verifica que o token bate com a proposta
    p = query("SELECT id, token, contrato_texto, contrato_assinado FROM propostas WHERE id=%s", (pid,), one=True)
    if not p or p["token"] != token:
        return jsonify({"erro": "Não autorizado"}), 403
    if not p["contrato_texto"]:
        return jsonify({"erro": "Contrato ainda não gerado"}), 400
    if p["contrato_assinado"]:
        return jsonify({"erro": "Contrato já assinado"}), 400
    if not nome:
        return jsonify({"erro": "Nome obrigatório para assinar"}), 400

    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()

    query("""
        UPDATE propostas SET
            contrato_assinado=TRUE,
            contrato_assinado_em=NOW(),
            contrato_ip=%s,
            contrato_nome_assinou=%s
        WHERE id=%s
    """, (ip, nome, pid), commit=True)

    return jsonify({"ok": True})

@app.route("/api/propostas/<int:pid>/contrato", methods=["PUT"])
@login_required
def api_contrato_salvar(pid):
    """Admin salva o texto e dados do contrato gerado"""
    d = request.json or {}
    query("""
        UPDATE propostas SET
            contrato_texto=%s,
            contrato_duracao=%s,
            contrato_tipo=%s,
            contrato_assinado=FALSE,
            contrato_assinado_em=NULL,
            contrato_ip=NULL,
            contrato_nome_assinou=NULL
        WHERE id=%s
    """, (
        d.get("texto"),
        d.get("duracao"),
        d.get("tipo", "pontual"),
        pid
    ), commit=True)
    return jsonify({"ok": True})

@app.route("/api/propostas/<int:pid>/contrato/pdf")
def api_contrato_pdf(pid):
    """Gera e retorna o PDF do contrato — acessível pelo cliente via token ou pelo admin logado"""
    token    = request.args.get("token", "")
    is_admin = bool(session.get("admin_id"))

    p = query("""
        SELECT p.*, c.nome as cliente_nome, c.empresa as cliente_empresa
        FROM propostas p JOIN clientes c ON c.id = p.cliente_id
        WHERE p.id = %s
    """, (pid,), one=True)

    if not p:
        abort(404)
    if not is_admin and p["token"] != token:
        abort(403)
    if not p["contrato_texto"]:
        abort(404)

    # Dados de assinatura
    assinado     = p.get("contrato_assinado") or False
    nome_assinou = p.get("contrato_nome_assinou") or ""
    assinado_em  = p.get("contrato_assinado_em")
    data_fmt     = ""
    if assinado_em:
        try:
            data_fmt = assinado_em.strftime("%d/%m/%Y")
        except Exception:
            partes   = str(assinado_em)[:10].split("-")
            data_fmt = f"{partes[2]}/{partes[1]}/{partes[0]}" if len(partes) == 3 else str(assinado_em)[:10]

    cliente_label = p.get("cliente_empresa") or p.get("cliente_nome") or ""
    titulo        = p.get("titulo") or "Contrato"
    texto         = p.get("contrato_texto") or ""

    # ── Monta PDF com ReportLab (pure Python, zero deps nativas) ──
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18*mm, rightMargin=18*mm,
        topMargin=20*mm, bottomMargin=20*mm
    )

    styles = getSampleStyleSheet()

    s_titulo = ParagraphStyle("rl_titulo",
        parent=styles["Normal"],
        fontSize=15, fontName="Helvetica-Bold",
        alignment=1, spaceAfter=4
    )
    s_sub = ParagraphStyle("rl_sub",
        parent=styles["Normal"],
        fontSize=10, fontName="Helvetica",
        alignment=1, textColor=colors.HexColor("#555555"), spaceAfter=0
    )
    s_corpo = ParagraphStyle("rl_corpo",
        parent=styles["Normal"],
        fontSize=10.5, fontName="Helvetica",
        leading=18, spaceAfter=4,
        textColor=colors.HexColor("#111111")
    )
    s_rodape = ParagraphStyle("rl_rodape",
        parent=styles["Normal"],
        fontSize=9, fontName="Helvetica",
        textColor=colors.HexColor("#444444"),
        spaceBefore=10
    )
    s_footer = ParagraphStyle("rl_footer",
        parent=styles["Normal"],
        fontSize=8, fontName="Helvetica",
        alignment=1, textColor=colors.HexColor("#999999"),
        spaceBefore=16
    )

    story = []

    # Cabeçalho
    story.append(Paragraph("CONTRATO DE PRESTAÇÃO DE SERVIÇOS", s_titulo))
    story.append(Paragraph(f"{titulo} — {cliente_label}", s_sub))
    story.append(Spacer(1, 4*mm))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#111111")))
    story.append(Spacer(1, 6*mm))

    # Corpo — cada linha vira um parágrafo; linhas vazias viram espaço
    for linha in texto.split("\n"):
        linha = linha.strip()
        # Escapa caracteres especiais do XML/ReportLab
        linha_safe = (linha
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        if linha_safe:
            story.append(Paragraph(linha_safe, s_corpo))
        else:
            story.append(Spacer(1, 3*mm))

    # Linha separadora antes do rodapé
    story.append(Spacer(1, 8*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cccccc")))

    # Rodapé de assinatura
    if assinado:
        rodape_txt = f"✓ Assinado eletronicamente por <b>{nome_assinou}</b> em {data_fmt} — válido conforme Lei 14.063/2020"
    else:
        rodape_txt = "Assinatura do Contratante: _____________________________      Data: ___/___/______"
    story.append(Paragraph(rodape_txt, s_rodape))

    # Footer institucional
    story.append(Paragraph(
        "Leanttro Tecnologia · CNPJ 63.556.406/0001-75 · São Paulo/SP · leanttro.com",
        s_footer
    ))

    doc.build(story)
    pdf_bytes = buf.getvalue()

    filename = re.sub(r'[^a-z0-9-]', '', titulo.lower().replace(' ', '-')) + ".pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

# ═══════════════════════════════════════════════════════════
#  API  TEMPLATES DO PORTAL
# ═══════════════════════════════════════════════════════════

@app.route("/api/templates")
@login_required
def api_templates_listar():
    return jsonify(listar_templates_portal())

# ═══════════════════════════════════════════════════════════
#  PORTAL PÚBLICO DO CLIENTE
# ═══════════════════════════════════════════════════════════

@app.route("/p/<token>")
def portal_cliente(token):
    # Aceita tanto o token aleatório quanto o slug bonito
    p = query("""
        SELECT p.*, c.nome as cliente_nome, c.empresa as cliente_empresa,
               c.email as cliente_email, c.whatsapp as cliente_whatsapp,
               c.slug as cliente_slug, c.logo_url as cliente_logo_url,
               c.pin_acesso as cliente_pin, c.id as cliente_id
        FROM propostas p JOIN clientes c ON c.id = p.cliente_id
        WHERE p.token = %s OR p.slug = %s
    """, (token, token), one=True)
    if not p: abort(404)

    # ── Verificação de PIN ──────────────────────────────────
    pin_do_cliente = p.get("cliente_pin") or ""
    if pin_do_cliente:
        chave_sessao = f"pin_ok_{p['cliente_id']}"
        if not session.get(chave_sessao):
            # Ainda não autenticou nesta sessão — mostra tela de PIN
            erro_pin = None
            if request.method == "POST":
                pin_digitado = request.form.get("pin", "").strip()
                if pin_digitado == str(pin_do_cliente):
                    session[chave_sessao] = True
                else:
                    erro_pin = "PIN incorreto. Tente novamente."
            if not session.get(chave_sessao):
                return render_template("portal/pin.html",
                    token=token,
                    cliente_nome=p.get("cliente_nome") or "",
                    logo_url=p.get("cliente_logo_url") or "",
                    erro=erro_pin
                )
    # ────────────────────────────────────────────────────────

    # Registra visualização na primeira vez
    if p["status"] == "rascunho":
        pass  # rascunho não registra
    elif p["status"] == "proposta":
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
        query("UPDATE propostas SET status='vista', visto_em=NOW(), visto_ip=%s WHERE token=%s",
              (ip, token), commit=True)
    elif p["status"] not in ("vista", "em_andamento", "concluido"):
        pass

    servicos  = query("SELECT * FROM servicos  WHERE proposta_id=%s ORDER BY ordem", (p["id"],)) or []
    tarefas   = query("SELECT * FROM tarefas   WHERE proposta_id=%s ORDER BY ordem", (p["id"],)) or []
    midias    = query("SELECT * FROM midias    WHERE proposta_id=%s ORDER BY ordem", (p["id"],)) or []
    links_    = query("SELECT * FROM links     WHERE proposta_id=%s ORDER BY ordem", (p["id"],)) or []
    mensagens = query("SELECT * FROM mensagens WHERE proposta_id=%s ORDER BY criado_em", (p["id"],)) or []
    metricas  = query("SELECT * FROM metricas  WHERE proposta_id=%s ORDER BY mes_ano DESC LIMIT 12", (p["id"],)) or []

    total = sum(float(s["quantidade"] or 1) * float(s["valor_unit"] or 0) for s in servicos)

    tarefas_eu      = [t for t in tarefas if t["responsavel"] == "eu"]
    tarefas_cliente = [t for t in tarefas if t["responsavel"] == "cliente"]

    # Template dinâmico  se não existe usa o padrão
    template_slug = p.get("template") or "portal_padrao"
    template_path = f"portal/{template_slug}.html"
    pasta = os.path.join(app.root_path, "templates", "portal", f"{template_slug}.html")
    if not os.path.exists(pasta):
        template_path = "portal/portal_padrao.html"

    contrato_row = query("SELECT contrato_texto, contrato_tipo, contrato_duracao, contrato_assinado, contrato_assinado_em, contrato_nome_assinou FROM propostas WHERE id=%s", (p["id"],), one=True)
    contrato = dict(contrato_row) if contrato_row else {}
    if contrato.get("contrato_assinado_em"):
        contrato["contrato_assinado_em"] = contrato["contrato_assinado_em"].isoformat()

    return render_template(template_path,
        proposta=dict(p),
        cliente={"nome": p["cliente_nome"], "empresa": p["cliente_empresa"],
                 "email": p["cliente_email"], "whatsapp": p["cliente_whatsapp"],
                 "slug": p.get("cliente_slug"), "logo_url": p.get("cliente_logo_url")},
        servicos=[dict(s) for s in servicos],
        tarefas_eu=[dict(t) for t in tarefas_eu],
        tarefas_cliente=[dict(t) for t in tarefas_cliente],
        midias=[dict(m) for m in midias],
        links=[dict(l) for l in links_],
        mensagens=[dict(m) for m in mensagens],
        metricas=[dict(m) for m in metricas],
        total=total,
        token=p["token"],
        contrato=contrato
    )


@app.route("/p/<token>", methods=["POST"])
def portal_cliente_pin(token):
    """Recebe o POST do formulário de PIN e redireciona de volta ao GET."""
    return portal_cliente(token)

# ═══════════════════════════════════════════════════════════
#  PORTAL DO CLIENTE  por slug
# ═══════════════════════════════════════════════════════════

@app.route("/c/<slug>", methods=["GET", "POST"])
def portal_cliente_lista(slug):
    cliente = query("SELECT * FROM clientes WHERE slug = %s", (slug,), one=True)
    if not cliente:
        abort(404)

    # ── Verificação de PIN ──────────────────────────────────
    pin_do_cliente = cliente.get("pin_acesso") or ""
    if pin_do_cliente:
        chave_sessao = f"pin_ok_{cliente['id']}"
        if not session.get(chave_sessao):
            erro_pin = None
            if request.method == "POST":
                pin_digitado = request.form.get("pin", "").strip()
                if pin_digitado == str(pin_do_cliente):
                    session[chave_sessao] = True
                else:
                    erro_pin = "PIN incorreto. Tente novamente."
            if not session.get(chave_sessao):
                return render_template("portal/pin.html",
                    token=slug,
                    form_action=f"/c/{slug}",
                    cliente_nome=cliente.get("nome") or "",
                    logo_url=cliente.get("logo_url") or "",
                    erro=erro_pin
                )
    # ────────────────────────────────────────────────────────

    propostas_raw = query("""
        SELECT p.*, COALESCE(SUM(s.quantidade * s.valor_unit), 0) as valor_total
        FROM propostas p
        LEFT JOIN servicos s ON s.proposta_id = p.id
        WHERE p.cliente_id = %s AND p.status != 'rascunho'
        GROUP BY p.id
        ORDER BY p.criado_em DESC
    """, (cliente["id"],)) or []
    propostas = [dict(p) for p in propostas_raw]
    # Serializa datas
    for p in propostas:
        for k, v in p.items():
            if hasattr(v, 'isoformat'):
                p[k] = v.isoformat()
    return render_template("portal/portal_cliente.html",
        cliente=dict(cliente),
        propostas=propostas
    )

@app.route("/c/<slug>/<token>")
def portal_cliente_redirecionar(slug, token):
    row = query("""
        SELECT p.token FROM propostas p
        JOIN clientes c ON c.id = p.cliente_id
        WHERE c.slug = %s AND p.token = %s
    """, (slug, token), one=True)
    if not row:
        abort(404)
    return redirect(f"/p/{token}")

# ═══════════════════════════════════════════════════════════
#  ROTA RAIZ
# ═══════════════════════════════════════════════════════════

@app.route("/")
def index():
    if session.get("admin_id"):
        return redirect("/admin")
    return redirect("/admin/login")

@app.route("/health")
def health():
    return jsonify({"status": "ok", "app": "leanttro-portal"})

# ═══════════════════════════════════════════════════════════════════════════
#  MÓDULO DE MÉTRICAS PREMIUM
#  Acesso via /metricas/<cliente_slug>
#  Credenciais Google por cliente (JSONB em clientes.google_credentials)
#  URI de callback lida do env: GOOGLE_REDIRECT_URI
# ═══════════════════════════════════════════════════════════════════════════

# ── Helpers de credenciais ──────────────────────────────────────────────

def _get_creds(cliente_id):
    """Lê google_credentials do banco e retorna objeto Credentials ou None."""
    if not GOOGLE_LIBS_OK:
        return None
    row = query(
        "SELECT google_credentials FROM clientes WHERE id=%s",
        (cliente_id,), one=True
    )
    if not row or not row["google_credentials"]:
        return None
    cred_data = row["google_credentials"]
    if isinstance(cred_data, str):
        cred_data = json.loads(cred_data)
    try:
        return Credentials(
            token=cred_data.get("token"),
            refresh_token=cred_data.get("refresh_token"),
            token_uri=cred_data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SEC,
            scopes=cred_data.get("scopes", SCOPES_ALL),
        )
    except Exception:
        return None


def _save_creds(cliente_id, creds):
    """Persiste Credentials na coluna google_credentials do cliente."""
    cred_data = {
        "token":         creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri":     creds.token_uri,
        "scopes":        list(creds.scopes) if creds.scopes else SCOPES_ALL,
    }
    query(
        "UPDATE clientes SET google_credentials=%s, metricas_conectado_em=NOW() WHERE id=%s",
        (json.dumps(cred_data), cliente_id), commit=True
    )


def _refresh_creds(cliente_id, creds):
    """Faz refresh do token se expirou. Limpa o banco se o refresh falhar."""
    if not (creds and creds.expired and creds.refresh_token):
        return creds
    try:
        creds.refresh(GoogleAuthRequest())
        _save_creds(cliente_id, creds)
        return creds
    except google.auth.exceptions.RefreshError:
        query(
            "UPDATE clientes SET google_credentials=NULL WHERE id=%s",
            (cliente_id,), commit=True
        )
        return None


def _creds_prontas(cliente_id):
    """Retorna Credentials válidas (com refresh automático) ou None."""
    creds = _get_creds(cliente_id)
    if not creds:
        return None
    return _refresh_creds(cliente_id, creds)


def _oauth_flow(scopes, state=None, code_verifier=None):
    """Cria um Flow OAuth com as configurações do app.
    A redirect URI vem de GOOGLE_REDIRECT_URI no .env (variável GOOGLE_REDIRECT).
    """
    config = {
        "web": {
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SEC,
            "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
            "token_uri":     "https://oauth2.googleapis.com/token",
            "redirect_uris": [GOOGLE_REDIRECT],
        }
    }
    kwargs = dict(scopes=scopes, redirect_uri=GOOGLE_REDIRECT)
    if state:
        kwargs["state"] = state
    flow = Flow.from_client_config(config, **kwargs)
    # Propaga code_verifier para o callback (evita "Missing code verifier")
    if code_verifier is not None:
        flow.code_verifier = code_verifier
    else:
        flow.code_verifier = None  # desabilita PKCE se nao houver verifier
    return flow


# ── Rota principal do cliente ────────────────────────────────────────────

@app.route("/metricas/<cliente_slug>")
def metricas_cliente(cliente_slug):
    cliente = query("SELECT * FROM clientes WHERE slug=%s", (cliente_slug,), one=True)
    if not cliente:
        abort(404)

    if not cliente.get("metricas_ativo"):
        return render_template(
            "portal/metricas_cliente.html",
            cliente=dict(cliente),
            metricas_ativo=False,
            ga4_property_id="",
            gsc_site_url="",
            conectado=False,
        )

    creds     = _get_creds(cliente["id"])
    conectado = bool(creds and creds.refresh_token)

    return render_template(
        "portal/metricas_cliente.html",
        cliente=dict(cliente),
        metricas_ativo=True,
        ga4_property_id=cliente.get("ga4_property_id") or "",
        gsc_site_url=cliente.get("gsc_site_url") or "",
        conectado=conectado,
    )


# ── API GSC ──────────────────────────────────────────────────────────────

@app.route("/api/metricas/<cliente_slug>/gsc")
def api_metricas_gsc(cliente_slug):
    if not GOOGLE_LIBS_OK:
        return jsonify({"erro": "Dependências Google não instaladas no servidor"}), 500

    cliente = query(
        "SELECT id, gsc_site_url, metricas_ativo FROM clientes WHERE slug=%s",
        (cliente_slug,), one=True
    )
    if not cliente:
        return jsonify({"erro": "cliente não encontrado"}), 404
    if not cliente.get("metricas_ativo"):
        return jsonify({"erro": "acesso não liberado"}), 403

    site_url = (cliente.get("gsc_site_url") or "").strip()
    if not site_url:
        return jsonify({"erro": "gsc_site_url não configurado para este cliente"}), 400

    try:
        dias = max(1, min(int(request.args.get("dias", 28)), 90))
    except (ValueError, TypeError):
        dias = 28

    hoje        = datetime.utcnow().date()
    data_inicio = (hoje - timedelta(days=dias)).isoformat()
    data_fim    = hoje.isoformat()

    creds = _creds_prontas(cliente["id"])
    if not creds:
        return jsonify({"erro": "Google não conectado", "conectado": False}), 401

    try:
        svc = google_build("webmasters", "v3", credentials=creds)

        def _query_gsc(dimensions, row_limit=10, order_field="clicks"):
            body = {
                "startDate":  data_inicio,
                "endDate":    data_fim,
                "dimensions": dimensions,
                "rowLimit":   row_limit,
            }
            if dimensions:
                body["orderBy"] = [{"fieldName": order_field, "sortOrder": "DESCENDING"}]
            return svc.searchanalytics().query(siteUrl=site_url, body=body).execute()

        # Totais globais
        r_totais = _query_gsc([], row_limit=1)
        t = r_totais.get("rows", [{}])[0] if r_totais.get("rows") else {}
        cliques    = round(t.get("clicks",      0))
        impressoes = round(t.get("impressions", 0))
        ctr        = round(t.get("ctr",         0) * 100, 2)
        posicao    = round(t.get("position",    0), 1)

        # Top páginas
        r_pag = _query_gsc(["page"], row_limit=10)
        top_paginas = [
            {
                "pagina":     r["keys"][0],
                "cliques":    round(r.get("clicks",      0)),
                "impressoes": round(r.get("impressions", 0)),
                "ctr":        round(r.get("ctr",         0) * 100, 2),
                "posicao":    round(r.get("position",    0), 1),
            }
            for r in r_pag.get("rows", [])
        ]

        # Top keywords
        r_kw = _query_gsc(["query"], row_limit=10)
        top_keywords = [
            {
                "query":      r["keys"][0],
                "cliques":    round(r.get("clicks",      0)),
                "impressoes": round(r.get("impressions", 0)),
                "ctr":        round(r.get("ctr",         0) * 100, 2),
                "posicao":    round(r.get("position",    0), 1),
            }
            for r in r_kw.get("rows", [])
        ]

        # Série temporal diária
        r_serie = _query_gsc(["date"], row_limit=90, order_field="date")
        serie = [
            {
                "data":       r["keys"][0],
                "cliques":    round(r.get("clicks",      0)),
                "impressoes": round(r.get("impressions", 0)),
            }
            for r in r_serie.get("rows", [])
        ]

        return jsonify({
            "ok":           True,
            "cliques":      cliques,
            "impressoes":   impressoes,
            "ctr":          ctr,
            "posicao":      posicao,
            "top_paginas":  top_paginas,
            "top_keywords": top_keywords,
            "serie":        serie,
            "periodo_dias": dias,
        })

    except Exception as e:
        return jsonify({"erro": str(e)}), 500


# ── API GA4 ──────────────────────────────────────────────────────────────

@app.route("/api/metricas/<cliente_slug>/ga4")
def api_metricas_ga4(cliente_slug):
    if not GOOGLE_LIBS_OK:
        return jsonify({"erro": "Dependências Google não instaladas no servidor"}), 500

    cliente = query(
        "SELECT id, ga4_property_id, metricas_ativo FROM clientes WHERE slug=%s",
        (cliente_slug,), one=True
    )
    if not cliente:
        return jsonify({"erro": "cliente não encontrado"}), 404
    if not cliente.get("metricas_ativo"):
        return jsonify({"erro": "acesso não liberado"}), 403

    property_id = (cliente.get("ga4_property_id") or "").strip()
    if not property_id:
        return jsonify({"erro": "ga4_property_id não configurado para este cliente"}), 400
    if not property_id.startswith("properties/"):
        return jsonify({"erro": "ga4_property_id deve ter formato 'properties/XXXXXXXXX'"}), 400

    try:
        dias = max(1, min(int(request.args.get("dias", 28)), 90))
    except (ValueError, TypeError):
        dias = 28

    hoje        = datetime.utcnow().date()
    data_inicio = (hoje - timedelta(days=dias)).isoformat()
    data_fim    = hoje.isoformat()

    creds = _creds_prontas(cliente["id"])
    if not creds:
        return jsonify({"erro": "Google não conectado", "conectado": False}), 401

    try:
        ga = BetaAnalyticsDataClient(credentials=creds)
        dr = DateRange(start_date=data_inicio, end_date=data_fim)

        # Totais
        resp_totais = ga.run_report(RunReportRequest(
            property=property_id,
            date_ranges=[dr],
            metrics=[
                Metric(name="sessions"),
                Metric(name="totalUsers"),
                Metric(name="screenPageViews"),
                Metric(name="bounceRate"),
                Metric(name="averageSessionDuration"),
            ],
        ))
        mv = resp_totais.rows[0].metric_values if resp_totais.rows else None
        sessoes       = int(float(mv[0].value))          if mv else 0
        usuarios      = int(float(mv[1].value))          if mv else 0
        pageviews     = int(float(mv[2].value))          if mv else 0
        bounce_rate   = round(float(mv[3].value) * 100, 1) if mv else 0
        duracao_media = round(float(mv[4].value))        if mv else 0

        # Canais
        resp_canais = ga.run_report(RunReportRequest(
            property=property_id,
            date_ranges=[dr],
            dimensions=[Dimension(name="sessionDefaultChannelGroup")],
            metrics=[Metric(name="sessions")],
            order_bys=[OrderBy(
                metric=OrderBy.MetricOrderBy(metric_name="sessions"),
                desc=True
            )],
            limit=8,
        ))
        canais = [
            {
                "canal":   r.dimension_values[0].value,
                "sessoes": int(float(r.metric_values[0].value)),
            }
            for r in resp_canais.rows
        ]

        # Top páginas
        resp_pag = ga.run_report(RunReportRequest(
            property=property_id,
            date_ranges=[dr],
            dimensions=[Dimension(name="pagePath")],
            metrics=[
                Metric(name="screenPageViews"),
                Metric(name="sessions"),
            ],
            order_bys=[OrderBy(
                metric=OrderBy.MetricOrderBy(metric_name="screenPageViews"),
                desc=True
            )],
            limit=10,
        ))
        top_paginas = [
            {
                "pagina":    r.dimension_values[0].value,
                "pageviews": int(float(r.metric_values[0].value)),
                "sessoes":   int(float(r.metric_values[1].value)),
            }
            for r in resp_pag.rows
        ]

        # Série temporal
        resp_serie = ga.run_report(RunReportRequest(
            property=property_id,
            date_ranges=[dr],
            dimensions=[Dimension(name="date")],
            metrics=[
                Metric(name="sessions"),
                Metric(name="totalUsers"),
            ],
            order_bys=[OrderBy(
                dimension=OrderBy.DimensionOrderBy(dimension_name="date")
            )],
            limit=90,
        ))
        serie = [
            {
                "data":     r.dimension_values[0].value,
                "sessoes":  int(float(r.metric_values[0].value)),
                "usuarios": int(float(r.metric_values[1].value)),
            }
            for r in resp_serie.rows
        ]

        return jsonify({
            "ok":            True,
            "sessoes":       sessoes,
            "usuarios":      usuarios,
            "pageviews":     pageviews,
            "bounce_rate":   bounce_rate,
            "duracao_media": duracao_media,
            "canais":        canais,
            "top_paginas":   top_paginas,
            "serie":         serie,
            "periodo_dias":  dias,
        })

    except Exception as e:
        return jsonify({"erro": str(e)}), 500


# ── API IA Análise ────────────────────────────────────────────────────────

@app.route("/api/metricas/<cliente_slug>/ia-analise", methods=["POST"])
def api_metricas_ia_analise(cliente_slug):
    cliente = query(
        "SELECT id, nome, empresa, metricas_ativo FROM clientes WHERE slug=%s",
        (cliente_slug,), one=True
    )
    if not cliente:
        return jsonify({"erro": "cliente não encontrado"}), 404
    if not cliente.get("metricas_ativo"):
        return jsonify({"erro": "acesso não liberado"}), 403

    d   = request.json or {}
    gsc = d.get("gsc", {})
    ga4 = d.get("ga4", {})

    nome_empresa = (cliente.get("empresa") or cliente.get("nome") or cliente_slug).strip()
    top_kw    = (gsc.get("top_keywords") or [{}])[0].get("keyword",  "N/D")
    top_canal = (ga4.get("canais")       or [{}])[0].get("canal",    "N/D")

    resumo = (
        f"Google Search Console — últimos {gsc.get('periodo_dias', 28)} dias:\n"
        f"  Cliques: {gsc.get('cliques', 'N/D')} | "
        f"Impressões: {gsc.get('impressoes', 'N/D')} | "
        f"CTR: {gsc.get('ctr', 'N/D')}% | "
        f"Posição média: {gsc.get('posicao', 'N/D')}\n"
        f"  Keyword principal: {top_kw}\n\n"
        f"Google Analytics 4 — últimos {ga4.get('periodo_dias', 28)} dias:\n"
        f"  Sessões: {ga4.get('sessoes', 'N/D')} | "
        f"Usuários: {ga4.get('usuarios', 'N/D')} | "
        f"Pageviews: {ga4.get('pageviews', 'N/D')}\n"
        f"  Taxa de rejeição: {ga4.get('bounce_rate', 'N/D')}% | "
        f"Duração média: {ga4.get('duracao_media', 'N/D')}s\n"
        f"  Canal principal: {top_canal}"
    )

    texto, erro = groq_chat(
        system_prompt=(
            "Você é um especialista em SEO e marketing digital da Leanttro. "
            "Analise métricas de sites e dê diagnósticos diretos, acionáveis e encorajadores. "
            "Use linguagem simples — o cliente não é técnico. "
            "Sempre em português do Brasil."
        ),
        user_prompt=(
            f"Analise as métricas do site da empresa '{nome_empresa}':\n\n"
            f"{resumo}\n\n"
            f"Escreva um diagnóstico com:\n"
            f"1. O que está indo bem\n"
            f"2. O principal ponto de atenção\n"
            f"3. Duas ações concretas para o próximo mês\n\n"
            f"Máximo 5 parágrafos curtos. Seja direto e positivo."
        ),
        max_tokens=500,
    )
    if erro:
        return jsonify({"erro": erro}), 500
    return jsonify({"analise": texto})


# ── OAuth Iniciar ───────────────────────────────────────────────────────

@app.route("/api/metricas/<cliente_slug>/oauth/start")
def metricas_oauth_start(cliente_slug):
    if not GOOGLE_LIBS_OK:
        return "Dependências Google não instaladas no servidor.", 500

    cliente = query(
        "SELECT id, metricas_ativo FROM clientes WHERE slug=%s",
        (cliente_slug,), one=True
    )
    if not cliente:
        abort(404)
    if not cliente.get("metricas_ativo"):
        abort(403)

    # servico=gsc|ga4|all  pede todos por padrão (evita reconexão dupla)
    servico = request.args.get("servico", "all")
    scopes  = {"gsc": SCOPES_GSC, "ga4": SCOPES_GA4}.get(servico, SCOPES_ALL)

    flow = _oauth_flow(scopes)
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",  # garante que refresh_token seja sempre retornado
    )

    session["metricas_oauth_state"]      = state
    session["metricas_oauth_slug"]       = cliente_slug
    session["metricas_oauth_scopes"]     = scopes
    session["metricas_oauth_cv"]         = flow.code_verifier  # salva para o callback

    return redirect(auth_url)


# ── OAuth Callback ──────────────────────────────────────────────────────

@app.route("/api/metricas/oauth/callback")
def metricas_oauth_callback():
    if not GOOGLE_LIBS_OK:
        return "Dependências Google não instaladas no servidor.", 500

    error        = request.args.get("error", "")
    state        = request.args.get("state", "")
    code         = request.args.get("code", "")
    cliente_slug = session.get("metricas_oauth_slug", "")
    scopes       = session.get("metricas_oauth_scopes", SCOPES_ALL)

    if error:
        return (
            f'<p style="font-family:sans-serif;padding:2rem">Erro ao conectar com o Google: '
            f'<b>{error}</b>.<br><br>'
            f'<a href="/metricas/{cliente_slug}">← Voltar</a></p>'
        ), 400

    if not cliente_slug:
        return (
            '<p style="font-family:sans-serif;padding:2rem">Sessão expirada. '
            'Feche esta aba e tente conectar novamente.</p>'
        ), 400

    cliente = query("SELECT id FROM clientes WHERE slug=%s", (cliente_slug,), one=True)
    if not cliente:
        abort(404)

    try:
        code_verifier = session.get("metricas_oauth_cv")
        flow = _oauth_flow(scopes, state=state, code_verifier=code_verifier)
        flow.fetch_token(code=code)
        _save_creds(cliente["id"], flow.credentials)
    except Exception as e:
        return (
            f'<p style="font-family:sans-serif;padding:2rem">Erro ao obter token do Google: '
            f'<b>{e}</b>.<br><br>'
            f'<a href="/metricas/{cliente_slug}">← Tentar novamente</a></p>'
        ), 500

    # Limpa variáveis de sessão do OAuth
    for k in ("metricas_oauth_state", "metricas_oauth_slug", "metricas_oauth_scopes", "metricas_oauth_cv"):
        session.pop(k, None)

    return redirect(f"/metricas/{cliente_slug}")


# ── Sync / Refresh forçado ────────────────────────────────────────────────

@app.route("/api/metricas/<cliente_slug>/status")
def api_metricas_status(cliente_slug):
    """Retorna status de conexao Google para o cliente (usado pelo frontend de metricas)."""
    cliente = query(
        "SELECT id, metricas_ativo, ga4_property_id, gsc_site_url, "
        "(google_credentials IS NOT NULL) as google_conectado "
        "FROM clientes WHERE slug=%s",
        (cliente_slug,), one=True
    )
    if not cliente:
        return jsonify({"erro": "cliente nao encontrado"}), 404
    creds_ok = bool(_creds_prontas(cliente["id"])) if GOOGLE_LIBS_OK else False
    return jsonify({
        "ativo": bool(cliente.get("metricas_ativo")),
        "google_conectado": creds_ok,
        "ga4_property_id": cliente.get("ga4_property_id"),
        "gsc_site_url": cliente.get("gsc_site_url"),
    })


@app.route("/api/metricas/<cliente_slug>/sync", methods=["POST"])
def api_metricas_sync(cliente_slug):
    cliente = query("SELECT id FROM clientes WHERE slug=%s", (cliente_slug,), one=True)
    if not cliente:
        return jsonify({"erro": "cliente não encontrado"}), 404

    creds = _creds_prontas(cliente["id"])
    if not creds:
        return jsonify({
            "ok": False, "conectado": False,
            "erro": "credenciais inválidas ou expiradas — reconecte o Google"
        })
    return jsonify({"ok": True, "conectado": True})


# ── Admin  Toggle métricas ───────────────────────────────────────────────

@app.route("/api/admin/clientes/<int:cliente_id>/metricas/toggle", methods=["POST"])
@login_required
def api_admin_metricas_toggle(cliente_id):
    """Liga/desliga o módulo de métricas premium para o cliente."""
    row = query(
        "SELECT metricas_ativo FROM clientes WHERE id=%s",
        (cliente_id,), one=True
    )
    if not row:
        return jsonify({"erro": "cliente não encontrado"}), 404
    novo = not bool(row.get("metricas_ativo"))
    query(
        "UPDATE clientes SET metricas_ativo=%s WHERE id=%s",
        (novo, cliente_id), commit=True
    )
    return jsonify({"ok": True, "ativo": novo})


# ── Admin  Salvar GA4 property + GSC site ────────────────────────────────

@app.route("/api/admin/clientes/<int:cliente_id>/metricas/ga4-property", methods=["POST"])
@login_required
def api_admin_metricas_config(cliente_id):
    """Salva o GA4 Property ID e a URL do GSC para o cliente."""
    d       = request.json or {}
    ga4_id  = (d.get("ga4_property_id") or "").strip()
    gsc_url = (d.get("gsc_site_url")    or "").strip()

    if ga4_id and not ga4_id.startswith("properties/"):
        return jsonify({
            "erro": "ga4_property_id deve começar com 'properties/' "
                    "(ex: properties/123456789)"
        }), 400

    # Garante barra no final da URL do GSC
    if gsc_url and not gsc_url.endswith("/"):
        gsc_url += "/"

    query(
        "UPDATE clientes SET ga4_property_id=%s, gsc_site_url=%s WHERE id=%s",
        (ga4_id or None, gsc_url or None, cliente_id), commit=True
    )
    return jsonify({"ok": True, "ga4_property_id": ga4_id, "gsc_site_url": gsc_url})


# ── Admin  Status do módulo de métricas ─────────────────────────────────

@app.route("/api/admin/clientes/<int:cliente_id>/metricas/status")
@login_required
def api_admin_metricas_status(cliente_id):
    """Retorna o status completo do módulo de métricas para o painel admin."""
    row = query("""
        SELECT metricas_ativo, ga4_property_id, gsc_site_url,
               metricas_conectado_em,
               (google_credentials IS NOT NULL) as google_conectado,
               slug
        FROM clientes WHERE id=%s
    """, (cliente_id,), one=True)
    if not row:
        return jsonify({"erro": "cliente não encontrado"}), 404
    data = dict(row)
    if data.get("metricas_conectado_em"):
        data["metricas_conectado_em"] = data["metricas_conectado_em"].isoformat()
    return jsonify(data)

# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    port  = int(os.getenv("PORT", 5002))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    print(f"🚀 Leanttro Portal — porta {port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
