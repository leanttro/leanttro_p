"""
LEANTTRO PORTAL — app.py
Portal de propostas/projetos para clientes da Leanttro
"""

from flask import (
    Flask, render_template, request, jsonify,
    redirect, session, g, abort, url_for
)
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
from dotenv import load_dotenv

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
GOOGLE_REDIRECT   = os.getenv("GOOGLE_REDIRECT_URI", "https://portal.leanttro.com/oauth/callback")
BASE_URL          = os.getenv("BASE_URL", "http://localhost:5002")

# ═══════════════════════════════════════════════════════════
#  BANCO
# ═══════════════════════════════════════════════════════════

def get_db():
    if "db" not in g:
        g.db = psycopg2.connect(
            dsn=os.getenv("DATABASE_URL", ""),
            cursor_factory=psycopg2.extras.RealDictCursor
        ) if os.getenv("DATABASE_URL") else psycopg2.connect(
            host     = os.getenv("DB_HOST", "213.199.56.207"),
            port     = int(os.getenv("DB_PORT", 5453)),
            dbname   = os.getenv("DB_NAME", "postgres"),
            user     = os.getenv("DB_USER", "leanttro"),
            password = os.getenv("DB_PASS", "Fin@2021"),
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
#  GROQ — IA
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
#  ROTAS — AUTH ADMIN
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
#  ROTAS — PAINEL ADMIN
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
#  API — CLIENTES
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
    row = query(
        "INSERT INTO clientes (nome, empresa, email, whatsapp) VALUES (%s,%s,%s,%s) RETURNING id",
        (d["nome"], d.get("empresa"), d.get("email"), d.get("whatsapp")), commit=True
    )
    return jsonify({"ok": True, "id": row["id"] if row else None})

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
    query("UPDATE clientes SET nome=%s, empresa=%s, email=%s, whatsapp=%s WHERE id=%s",
          (d["nome"], d.get("empresa"), d.get("email"), d.get("whatsapp"), cid), commit=True)
    return jsonify({"ok": True})

@app.route("/api/clientes/<int:cid>", methods=["DELETE"])
@login_required
def api_cliente_deletar(cid):
    query("DELETE FROM clientes WHERE id = %s", (cid,), commit=True)
    return jsonify({"ok": True})

# ═══════════════════════════════════════════════════════════
#  API — PROPOSTAS
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
    row = query("""
        INSERT INTO propostas
            (cliente_id, titulo, descricao, validade, prazo_entrega,
             forma_pagamento, status, cor_tema, mensagem_final, token)
        VALUES (%s,%s,%s,%s,%s,%s,'rascunho',%s,%s,%s) RETURNING id, token
    """, (
        d["cliente_id"], d["titulo"], d.get("descricao"),
        d.get("validade") or None, d.get("prazo_entrega") or None,
        d.get("forma_pagamento"), d.get("cor_tema", "#c17f3a"),
        d.get("mensagem_final"), token
    ), commit=True)
    return jsonify({"ok": True, "id": row["id"], "token": row["token"]})

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
    # Serializa datas pra string
    for k, v in data.items():
        if hasattr(v, 'isoformat'):
            data[k] = v.isoformat()
    return jsonify(data)

@app.route("/api/propostas/<int:pid>", methods=["PUT"])
@login_required
def api_proposta_editar(pid):
    d = request.json or {}
    query("""
        UPDATE propostas SET
            cliente_id=%s, titulo=%s, descricao=%s,
            validade=%s, prazo_entrega=%s, forma_pagamento=%s,
            status=%s, cor_tema=%s, mensagem_final=%s,
            atualizado_em=NOW()
        WHERE id=%s
    """, (
        d["cliente_id"], d["titulo"], d.get("descricao"),
        d.get("validade") or None, d.get("prazo_entrega") or None,
        d.get("forma_pagamento"), d.get("status", "rascunho"),
        d.get("cor_tema", "#c17f3a"), d.get("mensagem_final"), pid
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
    row = query("""
        INSERT INTO propostas
            (cliente_id,titulo,descricao,validade,prazo_entrega,
             forma_pagamento,cor_tema,mensagem_final,token,status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'rascunho') RETURNING id
    """, (p["cliente_id"], f"[Cópia] {p['titulo']}", p["descricao"],
          p["validade"], p["prazo_entrega"], p["forma_pagamento"],
          p["cor_tema"], p["mensagem_final"], token), commit=True)
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
#  API — TAREFAS (atualização de status pelo cliente/admin)
# ═══════════════════════════════════════════════════════════

@app.route("/api/tarefas/<int:tid>/resposta", methods=["POST"])
def api_tarefa_resposta(tid):
    """Cliente entrega uma tarefa — não precisa de login admin"""
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
#  API — MENSAGENS
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
#  API — MÉTRICAS
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
#  API — IA (GROQ)
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

# ═══════════════════════════════════════════════════════════
#  API — TEMPLATES DO PORTAL
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
    p = query("""
        SELECT p.*, c.nome as cliente_nome, c.empresa as cliente_empresa,
               c.email as cliente_email, c.whatsapp as cliente_whatsapp
        FROM propostas p JOIN clientes c ON c.id = p.cliente_id
        WHERE p.token = %s
    """, (token,), one=True)
    if not p: abort(404)

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

    # Template dinâmico — se não existe usa o padrão
    template_slug = p.get("template") or "portal_padrao"
    template_path = f"portal/{template_slug}.html"
    pasta = os.path.join(app.root_path, "templates", "portal", f"{template_slug}.html")
    if not os.path.exists(pasta):
        template_path = "portal/portal_padrao.html"

    return render_template(template_path,
        proposta=dict(p),
        cliente={"nome": p["cliente_nome"], "empresa": p["cliente_empresa"],
                 "email": p["cliente_email"], "whatsapp": p["cliente_whatsapp"]},
        servicos=[dict(s) for s in servicos],
        tarefas_eu=[dict(t) for t in tarefas_eu],
        tarefas_cliente=[dict(t) for t in tarefas_cliente],
        midias=[dict(m) for m in midias],
        links=[dict(l) for l in links_],
        mensagens=[dict(m) for m in mensagens],
        metricas=[dict(m) for m in metricas],
        total=total,
        token=token
    )

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

# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    port  = int(os.getenv("PORT", 5002))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    print(f"🚀 Leanttro Portal — porta {port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
