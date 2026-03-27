"""Gerador de PDF no padrao Harvard usando fpdf2."""

import io
import re
import logging

import fitz  # PyMuPDF
from fpdf import FPDF

logger = logging.getLogger(__name__)

# --- Sanitizacao de texto para Latin-1 (fpdf2) ---

_SUBS: dict[str, str] = {
    "\u2022": "-", "\u2013": "-", "\u2014": "-",
    "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"',
    "\u00b7": "-", "\u2026": "...",
}


def sanitize(text: str) -> str:
    """Remove caracteres incompativeis com Latin-1 para o fpdf2."""
    if not text:
        return ""
    text = str(text).replace("\t", " ")
    for c, r in _SUBS.items():
        text = text.replace(c, r)
    return text.encode("latin-1", "ignore").decode("latin-1")


def clean_null_value(val: object) -> str:
    """Converte valores nulos ou 'None' em string vazia."""
    if val is None:
        return ""
    v_str = str(val).strip()
    if v_str.lower() in ("none", "null"):
        return ""
    return v_str


def extrair_texto_arquivo(file_bytes: bytearray, filename: str) -> str:
    """Extrai texto de PDF (via PyMuPDF) ou texto puro."""
    if filename.lower().endswith(".pdf"):
        try:
            doc = fitz.open("pdf", file_bytes)
            return chr(12).join([page.get_text("text") for page in doc])
        except Exception as e:
            logger.error("Falha na extracao de PDF via PyMuPDF: %s", e)
            return ""
    for enc in ["utf-8", "latin-1", "cp1252"]:
        try:
            return file_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return file_bytes.decode("utf-8", errors="ignore")


def slug(text: str) -> str:
    """Gera slug seguro para nomes de arquivo."""
    return re.sub(r"[^\w\-]", "_", text.strip())[:40]


# --- Classe PDF Harvard ---

class CurriculoHarvard(FPDF):
    """Gerador de curriculo em PDF no formato Harvard."""

    def __init__(self) -> None:
        super().__init__()
        self.set_margins(20, 20, 20)
        self.add_page()
        self.set_auto_page_break(True, margin=15)

    def _secao(self, titulo: str) -> None:
        self.ln(2)
        self.set_font("helvetica", "B", 11)
        self.cell(0, 7, sanitize(titulo.upper()), new_x="LMARGIN", new_y="NEXT")
        self.set_line_width(0.5)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(2)

    def _linha(self, txt: str, size: int = 10, bold: bool = False, italic: bool = False, align: str = "L") -> None:
        style = ("B" if bold else "") + ("I" if italic else "")
        self.set_font("helvetica", style, size)
        self.multi_cell(0, 5, sanitize(txt), align=align, new_x="LMARGIN", new_y="NEXT")

    def _bullet(self, txt: str, prefixo: str = "-") -> None:
        self.set_font("helvetica", "", 10)
        self.multi_cell(0, 5, sanitize(f"{prefixo} {txt}"), new_x="LMARGIN", new_y="NEXT")

    def _flatten_item(self, item: object) -> str:
        if isinstance(item, dict):
            parts = [clean_null_value(v) for v in item.values() if clean_null_value(v)]
            return " - ".join(parts)
        return str(item)

    def bloco_cabecalho(self, ident: dict) -> None:
        nome = clean_null_value(ident.get("nome"))
        titulo = clean_null_value(ident.get("titulo"))
        self.set_font("helvetica", "B", 18)
        self.multi_cell(0, 10, sanitize(nome), align="C", new_x="LMARGIN", new_y="NEXT")
        if titulo:
            self.set_font("helvetica", "I", 11)
            self.multi_cell(0, 6, sanitize(titulo), align="C", new_x="LMARGIN", new_y="NEXT")
        campos = ["email", "telefone", "linkedin", "localizacao", "github", "portfolio"]
        contatos = [clean_null_value(ident.get(c)) for c in campos if clean_null_value(ident.get(c))]
        if contatos:
            self.set_font("helvetica", "", 9)
            self.multi_cell(0, 5, sanitize(" | ".join(contatos)), align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(4)

    def bloco_resumo(self, titulo: str, texto: str) -> None:
        txt = clean_null_value(texto)
        if not txt:
            return
        self._secao(titulo)
        self._linha(txt)

    def bloco_competencias(self, titulo: str, lista: list) -> None:
        if not lista:
            return
        self._secao(titulo)
        itens = [sanitize(self._flatten_item(i)) for i in lista if clean_null_value(self._flatten_item(i))]
        self.set_font("helvetica", "", 10)
        self.multi_cell(0, 5, " | ".join(itens), new_x="LMARGIN", new_y="NEXT")

    def bloco_experiencias(self, titulo: str, exps: list) -> None:
        if not exps:
            return
        self._secao(titulo)
        for exp in exps:
            if not isinstance(exp, dict):
                continue
            cargo = clean_null_value(exp.get("cargo"))
            empresa = clean_null_value(exp.get("empresa"))
            local_exp = clean_null_value(exp.get("localizacao"))
            inicio = clean_null_value(exp.get("data_inicio"))
            fim = clean_null_value(exp.get("data_fim"))
            desc_emp = clean_null_value(exp.get("descricao_empresa"))

            self.set_font("helvetica", "B", 11)
            linha_cargo = cargo
            if empresa:
                linha_cargo += f" - {empresa}"
            self.cell(0, 6, sanitize(linha_cargo), new_x="LMARGIN", new_y="NEXT")

            meta = ""
            if inicio and fim:
                meta = f"{inicio} - {fim}"
            elif inicio:
                meta = f"{inicio} - Atual"
            elif fim:
                meta = fim
            if local_exp:
                meta = f"{meta} | {local_exp}" if meta else local_exp
            if meta:
                self.set_font("helvetica", "I", 9)
                self.cell(0, 5, sanitize(meta), new_x="LMARGIN", new_y="NEXT")
            if desc_emp:
                self.set_font("helvetica", "I", 9)
                self.multi_cell(0, 4, sanitize(desc_emp), new_x="LMARGIN", new_y="NEXT")
            self.ln(1)

            resps = exp.get("responsabilidades", [])
            if isinstance(resps, str):
                resps = [resps]
            for r in resps:
                val = clean_null_value(self._flatten_item(r))
                if val:
                    self._bullet(val, "-")

            conquistas = exp.get("conquistas", [])
            if isinstance(conquistas, str):
                conquistas = [conquistas]
            for c in conquistas:
                val = clean_null_value(self._flatten_item(c))
                if val:
                    self.set_font("helvetica", "B", 10)
                    self.multi_cell(0, 5, sanitize(f">> {val}"), new_x="LMARGIN", new_y="NEXT")
            self.ln(3)

    def bloco_educacao(self, titulo: str, edus: list) -> None:
        if not edus:
            return
        self._secao(titulo)
        for edu in edus:
            if not isinstance(edu, dict):
                continue
            grau = clean_null_value(edu.get("grau"))
            curso = clean_null_value(edu.get("curso"))
            inst = clean_null_value(edu.get("instituicao"))
            ini = clean_null_value(edu.get("ano_inicio"))
            fim = clean_null_value(edu.get("ano_fim"))

            cabecalho = f"{grau} em {curso}" if grau else curso
            self.set_font("helvetica", "B", 11)
            self.cell(0, 6, sanitize(cabecalho), new_x="LMARGIN", new_y="NEXT")

            meta = ""
            if ini and fim:
                meta = f"{ini} - {fim}"
            elif ini:
                meta = ini
            elif fim:
                meta = fim
            if inst:
                meta = f"{inst} | {meta}" if meta else inst
            if meta:
                self.set_font("helvetica", "I", 10)
                self.cell(0, 5, sanitize(meta), new_x="LMARGIN", new_y="NEXT")
            self.ln(3)

    def bloco_lista_simples(self, titulo: str, itens: list) -> None:
        if not itens:
            return
        self._secao(titulo)
        self.set_font("helvetica", "", 10)
        for item in itens:
            val = clean_null_value(self._flatten_item(item))
            if val:
                self.multi_cell(0, 5, sanitize(f"- {val}"), new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def bloco_projetos(self, titulo: str, projetos: list) -> None:
        if not projetos:
            return
        self._secao(titulo)
        for proj in projetos:
            if not isinstance(proj, dict):
                continue
            self.set_font("helvetica", "B", 10)
            self.cell(0, 6, sanitize(clean_null_value(proj.get("nome"))), new_x="LMARGIN", new_y="NEXT")
            self.set_font("helvetica", "", 10)
            self.multi_cell(0, 5, sanitize(clean_null_value(proj.get("descricao"))), new_x="LMARGIN", new_y="NEXT")
            self.ln(2)

    def bloco_keywords_ocultas(self, keywords: list) -> None:
        if not keywords:
            return
        termos = [sanitize(str(k)) for k in keywords if clean_null_value(k)]
        if not termos:
            return
        self.set_text_color(255, 255, 255)
        self.set_font("helvetica", "", 1)
        self.multi_cell(0, 1, " ".join(termos), new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)


def gerar_pdf(cv: dict, idioma: str = "Portugues") -> io.BytesIO:
    """Gera o PDF do curriculo a partir do dict do LLM."""
    from src.services.llm import get_cabecalhos

    if not isinstance(cv, dict):
        cv = {}
    cab = get_cabecalhos(idioma)
    pdf = CurriculoHarvard()
    pdf.bloco_cabecalho(cv.get("identificacao", {}))
    pdf.bloco_resumo(cab["resumo"], cv.get("resumo", ""))
    pdf.bloco_competencias(cab["competencias"], cv.get("competencias", []))
    pdf.bloco_experiencias(cab["experiencias"], cv.get("experiencias", []))
    pdf.bloco_educacao(cab["educacao"], cv.get("educacao", []))
    pdf.bloco_lista_simples(cab["certificacoes"], cv.get("certificacoes", []))
    pdf.bloco_projetos(cab["projetos"], cv.get("projetos", []))
    pdf.bloco_lista_simples(cab["idiomas"], cv.get("idiomas", []))
    pdf.bloco_keywords_ocultas(cv.get("keywords_ocultas", []))
    buf = io.BytesIO()
    pdf.output(buf)
    buf.seek(0)
    return buf
