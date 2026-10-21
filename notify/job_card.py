"""One evidence-based presentation shared by Telegram and email."""
import html
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from core.scope_analyzer import _clean, _requirements, _hits


def clean(value, limit=180):
    text = re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def escape(value):
    return html.escape(value, quote=True)


def application_url(job):
    candidates = [job.primary_url] + [source.url for source in job.sources.values()]
    for value in candidates:
        value = html.unescape(value or "").strip()
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return value
    return ""


def _publication_age(value, now=None):
    if not value:
        return ''
    try:
        published = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        elapsed_seconds = ((now or datetime.now(timezone.utc)) - published).total_seconds()
        if elapsed_seconds < 0:
            return ''
        minutes = int(elapsed_seconds // 60)
    except (TypeError, ValueError):
        return ''
    if minutes < 60:
        return f'há {max(1, minutes)} min'
    hours = minutes // 60
    if hours < 24:
        return f'há {hours} h'
    return f'há {hours // 24} d'


def build_card(job):
    """Keep internal scores in storage; never present defaults as confirmed facts."""
    title = clean(job.title)
    company = clean(job.company, 100)
    workplace = {"remote": "Remoto", "hybrid": "Híbrido", "on-site": "Presencial"}.get(
        job.workplace_type, "Modalidade a confirmar")
    location = clean(job.location, 100)
    if location and _clean(location) not in {_clean(workplace), "remote", "remoto", "unknown", "nao informado"}:
        workplace += f" · {location}"
    lines = [f"<b>{escape(title)}</b>", escape(f"{company} · {workplace}")]
    source_labels = {
        "linkedin": "LinkedIn", "gupy": "Gupy", "github": "GitHub",
        "github_issues": "GitHub", "rss": "RSS", "google_news": "Google Notícias",
        "trampos": "Trampos.co", "career_page": "Página de carreiras",
    }
    origins = list(dict.fromkeys(source_labels.get(name.lower(), clean(name, 40)) for name in job.sources))
    origin = ", ".join(origins) if origins else "Não registrada"
    published = _publication_age(getattr(job, 'published_at', ''))
    lines.append(escape(" · ".join(part for part in (origin, published) if part)))
    technologies = list(dict.fromkeys(clean(t, 30) for t in job.technologies if t))[:4]
    if technologies:
        lines.extend(["", escape(" · ".join(technologies))])

    # job_type historically defaults to CLT even for internships. Only show
    # contract labels supported explicitly by the source description.
    contracts = [label for label in ("CLT", "PJ") if re.search(rf"\b{label}\b", job.description or "", re.I)]
    facts = []
    internship = bool(re.search(r"\b(intern|internship|estagio|estagiario|estagiaria)\b", _clean(title)))
    if not internship and len(contracts) == 1 and job.job_type.upper() == contracts[0]:
        facts.append(contracts[0])
    salary = clean(job.salary, 100)
    if salary and _clean(salary) not in {"nao informado", "a combinar", "unknown", "nao divulgado"}:
        facts.append(salary)
    if facts:
        lines.append(escape(" · ".join(facts)))

    analysis = job.analysis or {}
    matched = list(dict.fromkeys(clean(t, 30) for t in analysis.get("matched_evidence", []) if t))[:3]
    reason = ("Aderência: " + ", ".join(matched)) if matched else "Aderência a conferir no anúncio"
    if job.compatibility_category in {"POTENCIALMENTE_COMPATIVEL", "DESAFIADORA_VALIDA"}:
        reason = "Pode ser uma oportunidade de progressão · " + reason
    lines.extend(["", escape(reason)])
    gaps = [clean(item, 100) for item in analysis.get("trainable_gaps", []) if item]
    if gaps:
        lines.append(escape("Atenção: " + "; ".join(gaps[:2]) + " sem evidência no perfil"))
    llm_health = str(analysis.get('llm_health') or '').strip()
    if llm_health:
        lines.extend(['', escape(llm_health)])

    alternatives = analysis.get("duplicate_listings", [])
    if alternatives:
        links = []
        for listing in alternatives[:5]:
            url = (listing.get("url") or "").strip()
            parsed = urlsplit(url)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                label = clean(listing.get("title") or listing.get("location") or "Anúncio alternativo", 70)
                links.append(f'<a href="{escape(url)}">{escape(label)}</a>')
        if links:
            lines.append("Anúncios muito semelhantes: " + " · ".join(links))

    # Select cautions independently of score explanations (which can hide
    # requirements after the first three reasons).
    cautions = []
    for barrier in analysis.get("hard_barriers", []):
        if _hits(_clean(job.description), [_clean(barrier)]):
            cautions.append("Requisito a conferir: " + clean(barrier, 140))
    requirements = _requirements(job.description).get("mandatory", [])
    for requirement in requirements:
        normalized = _clean(requirement)
        if re.search(r"\b(ingles|english|bacharelado|bachelor|graduacao|superior completo)\b", normalized):
            match = re.search(r"\b(ingles|english|bacharelado|bachelor|graduacao|superior completo)\b", normalized)
            # Preserve the context around the requirement, not the start of a
            # long company introduction that happened to share the same line.
            start = max(0, match.start() - 55)
            excerpt = requirement[start:match.end() + 100]
            if start:
                excerpt = "…" + excerpt.split(" ", 1)[-1]
            cautions.append("Confira no anúncio: " + clean(excerpt, 160))
    if job.ranking_evidence.get("risk") or analysis.get("operational_level") == "senior":
        cautions.append("Os requisitos podem estar acima do nível anunciado.")
    if job.evidence_level == "LOW_EVIDENCE":
        cautions.append("Há poucas informações disponíveis sobre os requisitos.")
    cautions = list(dict.fromkeys(cautions))
    if cautions:
        lines.extend(["", "<b>Atenção:</b> " + escape(" ".join(cautions[:2]))])
    return "\n".join(lines), application_url(job)
