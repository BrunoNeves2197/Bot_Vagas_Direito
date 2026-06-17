import os
import re
import time
import html
import sqlite3
import requests
from datetime import datetime


def esc(texto):
    return html.escape(str(texto)) if texto else ""


try:
    from bs4 import BeautifulSoup

    BS4_DISPONIVEL = True
except ImportError:
    BS4_DISPONIVEL = False
    print("⚠️ beautifulsoup4 não instalado. Rode: pip install beautifulsoup4")

# --- 1. CONFIGURAÇÕES DE AMBIENTE ---
DIRETORIO_ATUAL = os.path.dirname(os.path.abspath(__file__))
CAMINHO_BANCO = os.path.join(DIRETORIO_ATUAL, 'vagas_direito_docencia.db')


def carregar_env():
    caminho = os.path.join(DIRETORIO_ATUAL, '.env')
    if os.path.exists(caminho):
        with open(caminho) as f:
            for linha in f:
                linha = linha.strip()
                if linha and not linha.startswith('#') and '=' in linha:
                    chave, valor = linha.split('=', 1)
                    os.environ.setdefault(chave.strip(), valor.strip())


carregar_env()

TOKEN   = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID_GRUPO")

# --- 2. PERFIL DOCENTE DIREITO ---

# Palavras que indicam foco em Pós-Graduação ou títulos acadêmicos para destacar no Match
PALAVRAS_POS = [
    "pós", "pos", "mba", "especialização", "mestrado", "doutorado", "stricto", "lato"
]

# Ruídos comuns: Vagas jurídicas corporativas ou operacionais que NÃO são para dar aula
GAPS_ELIMINATORIOS = [
    "advogado", "associado", "assessor", "estagiário", "estágio", "legal counsel",
    "analista jurídico", "paralegal", "controller", "correspondente"
]

# Áreas de Especialidade / Termos Fortes de Docência
STACK_AVANCADO = [
    "professor", "docente", "direito", "jurídica", "jurídico", "magistério"
]

STACK_INTERMEDIARIO = [
    "tutor", "corretor", "banca", "conteudista", "remoto", "ead", "híbrido"
]

_enviados_sessao: set = set()


def _chave_sessao(titulo: str, empresa: str) -> str:
    normalizar = lambda s: re.sub(r'[^a-z0-9]', '', s.lower())
    return normalizar(titulo)[:60] + "|" + normalizar(empresa)[:30]


def tem_gap_eliminatorio(titulo):
    t = titulo.lower()
    # Se contiver "professor" ou "docente", ignora o gap (ex: "Professor - Direito do Trabalho e Prática Advocatícia")
    if "professor" in t or "docente" in t or "tutor" in t:
        return False
    return any(g in t for g in GAPS_ELIMINATORIOS)


def calcular_match(titulo):
    t = titulo.lower()
    techs_av = [s for s in STACK_AVANCADO if s in t]
    techs_int = [s for s in STACK_INTERMEDIARIO if s in t]

    score = len(techs_av) * 3 + len(techs_int)

    # Bônus para Pós-Graduação / MBA
    if any(x in t for x in PALAVRAS_POS):
        score += 6
        nivel = "🔥 Pós-Graduação"
    elif score >= 6:
        nivel = "🟢 Alto"
    elif score >= 3:
        nivel = "🟡 Médio"
    else:
        nivel = "🔵 Padrão"

    return nivel, techs_av + techs_int


# --- 3. BANCO E TELEGRAM ---

def iniciar_banco():
    conn = sqlite3.connect(CAMINHO_BANCO)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS vagas_enviadas (
            link TEXT PRIMARY KEY,
            data_publicacao TEXT,
            titulo TEXT
        )
    ''')
    conn.commit()
    return conn, cursor


def ja_enviada(cursor, link):
    cursor.execute('SELECT 1 FROM vagas_enviadas WHERE link = ?', (link,))
    return cursor.fetchone() is not None


def enviar_telegram(mensagem):
    if not TOKEN or not CHAT_ID: return
    payload = {
        "chat_id": CHAT_ID,
        "text": mensagem,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        print(f"❌ Erro Telegram: {e}")


def registrar_e_enviar(conn, cursor, link, titulo, empresa, data_f, mensagem, nivel_match):
    chave = _chave_sessao(titulo, empresa)
    if chave in _enviados_sessao: return

    _enviados_sessao.add(chave)
    cursor.execute('INSERT OR IGNORE INTO vagas_enviadas VALUES (?, ?, ?)', (link, data_f, titulo))
    conn.commit()
    enviar_telegram(mensagem)
    print(f"    ✅ [{nivel_match}] {titulo[:50]}...")
    time.sleep(2)


def filtros_basicos(titulo):
    if tem_gap_eliminatorio(titulo):
        return True, f"🚫 Gap: {titulo[:55]}"
    return False, ""


# --- 4. BUSCADORES (BRASIL TODO) ---

def buscar_vagas_gupy(conn, cursor):
    print("\n🟣 GUPY DIREITO DOCÊNCIA (Brasil) — Iniciando...")
    url_api = "https://employability-portal.gupy.io/api/v1/jobs"
    headers = {'User-Agent': 'Mozilla/5.0'}

    # Filtros amplos para capturar em todo o território nacional
    filtros = [
        {"nome": "Professor Direito", "params": {'jobName': 'professor direito', 'limit': 20}},
        {"nome": "Docente Direito", "params": {'jobName': 'docente direito', 'limit': 15}},
        {"nome": "Professor Pós", "params": {'jobName': 'professor pós', 'limit': 15}},
    ]

    for filtro in filtros:
        try:
            resp = requests.get(url_api, headers=headers, params=filtro['params'], timeout=15)
            dados = resp.json().get('data', [])
            for vaga in dados:
                link = vaga.get('jobUrl', '')
                titulo = vaga.get('name', '')

                bloqueada, motivo = filtros_basicos(titulo)
                if bloqueada or ja_enviada(cursor, link): continue

                nivel_match, tags = calcular_match(titulo)
                empresa = vaga.get('careerPageName', 'Instituição de Ensino')
                data_iso = vaga.get('publishedDate', '2026-01-01T00:00:00')
                data_f = datetime.strptime(data_iso.split('.')[0], "%Y-%m-%dT%H:%M:%S").strftime("%d/%m/%Y")

                # Identifica tipo de trabalho na Gupy
                tipo_trabalho = vaga.get('workplaceType', '')
                tipo_f = "Remoto" if tipo_trabalho == "remote" else "Híbrido" if tipo_trabalho == "hybrid" else "Presencial/Não Especificado"

                mensagem = (
                    f"🟣 <b>GUPY — Docência Jurídica</b>\n\n"
                    f"💼 <b>Cargo:</b> {esc(titulo)}\n"
                    f"🏢 <b>Instituição:</b> {esc(empresa)}\n"
                    f"📍 <b>Modelo:</b> {tipo_f}\n"
                    f"📅 <b>Postada:</b> {data_f}\n"
                    f"📊 <b>Alvo:</b> {nivel_match}\n\n"
                    f"🔗 <a href='{esc(link)}'>Ver e Candidatar-se</a>"
                )
                registrar_e_enviar(conn, cursor, link, titulo, empresa, data_f, message := mensagem, nivel_match)
        except Exception as e:
            print(f"⚠️ Erro Gupy: {e}")


def buscar_vagas_linkedin(conn, cursor):
    print("\n🔷 LINKEDIN DIREITO DOCÊNCIA (Brasil) — Iniciando...")
    url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

    # f_TPR=r86400 (Últimas 24h)
    filtros = [
        {"keywords": "professor direito", "location": "Brazil", "f_TPR": "r86400"},
        {"keywords": "docente direito", "location": "Brazil", "f_TPR": "r86400"},
        {"keywords": "professor pos graduacao direito", "location": "Brazil", "f_TPR": "r86400"},
        {"keywords": "professor mba direito", "location": "Brazil", "f_TPR": "r86400"}
    ]

    for f_params in filtros:
        try:
            resp = requests.get(url, params=f_params, timeout=15)
            if not BS4_DISPONIVEL: break
            soup = BeautifulSoup(resp.text, 'html.parser')
            for card in soup.find_all('div', class_='base-card'):
                titulo_el = card.find(class_=lambda c: c and 'title' in c)
                link_el = card.find('a', href=True)
                if not titulo_el or not link_el: continue

                titulo = titulo_el.get_text(strip=True)
                link = link_el['href'].split('?')[0]

                bloqueada, motivo = filtros_basicos(titulo)
                if bloqueada or ja_enviada(cursor, link): continue

                nivel_match, tags = calcular_match(titulo)

                # --- CORREÇÃO DA EXTRAÇÃO SEGURA ---
                empresa_el = card.find(class_=lambda c: c and 'subtitle' in c)
                empresa = empresa_el.get_text(strip=True) if empresa_el else "Instituição Não Especificada"

                local_el = card.find(class_=lambda c: c and 'metadata__location' in c)
                local = local_el.get_text(strip=True) if local_el else "Brasil"
                # -------------------------------------

                mensagem = (
                    f"🔷 <b>LINKEDIN — Docência Jurídica</b>\n\n"
                    f"💼 <b>Cargo:</b> {esc(titulo)}\n"
                    f"🏢 <b>Instituição:</b> {esc(empresa)}\n"
                    f"📍 <b>Local:</b> {esc(local)}\n"
                    f"📊 <b>Alvo:</b> {nivel_match}\n\n"
                    f"🔗 <a href='{esc(link)}'>Ver no LinkedIn</a>"
                )
                registrar_e_enviar(conn, cursor, link, titulo, empresa, "Hoje", mensagem, nivel_match)
        except Exception as e:
            print(f"⚠️ Erro LinkedIn: {e}")

# --- EXECUÇÃO ---

def main():
    if not TOKEN or not CHAT_ID:
        print("❌ ERRO: Configure as variáveis de ambiente TELEGRAM_TOKEN e CHAT_ID_GRUPO.")
        return

    conn, cursor = iniciar_banco()
    buscar_vagas_gupy(conn, cursor)
    buscar_vagas_linkedin(conn, cursor)
    conn.close()
    print("\n✅ Busca finalizada!")


if __name__ == '__main__':
    main()