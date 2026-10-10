"""Modelo de costes y rentabilidad (Fase 3).

Combina las tarifas editables (tabla `tariffs`, en `metrics.sqlite3`) con los
eventos medidos para estimar, por tenant y período (YYYY-MM):

  - Coste de IA: a partir de `usage_events` (prompt/completion tokens) cruzados
    con la tarifa del proveedor y modelo que respondió.
  - Coste de WhatsApp: a partir de `whatsapp_message_status` (estados de entrega
    con la categoría de precios de Meta). Solo se factura lo *entregado* y
    facturable; se aplica el tier de 1.000 mensajes de servicio gratis por
    número de negocio y mes.
  - Ingresos: de `monthly_revenue`.

Reglas:
  - Nunca se estiman tokens ni se inventan precios. Si faltan tokens o tarifa,
    el evento se cuenta como "desconocido" y queda FUERA del coste (no se
    subestima ni se sobreestima en silencio).
  - Un evento medido cuenta una sola vez (no se mezclan `message_sent` y
    `whatsapp_message_status`: el coste usa solo el segundo).
  - No se calcula margen si no hay ingreso configurado para ese tenant/período.

El costeo es de solo lectura sobre los eventos; las tarifas por defecto se
siembran (si faltan) con `seed_default_tariffs()`, que nunca pisa filas
existentes para no perder ediciones manuales hechas sobre la BD.
"""

import os
import sqlite3

import metrics

# Tier gratuito de Meta: 1.000 mensajes de servicio por número de negocio al
# mes (sin rollover). Se aplica por (tenant, phone_number_id).
FREE_WHATSAPP_SERVICE_PER_MONTH = 1000

# Mercado por defecto para el costeo de WhatsApp. RD (+1 809/829/849, ISO DO)
# no es un mercado standalone: cae en "Rest of Latin America".
DEFAULT_WHATSAPP_MARKET = "rest_of_latam"

# Tarifas por defecto. Son una SEMILLA: se insertan solo si no existen. Las
# fuentes y fechas quedan en la propia fila (`source_url`, `notes`,
# `effective_from`). Ver AGENTS.md para el detalle y las advertencias.
DEFAULT_TARIFFS = [
    {
        "kind": "llm",
        "provider": "groq",
        "model": "openai/gpt-oss-120b",
        "rate_input": 0.15,
        "rate_output": 0.60,
        "unit": "per_1m_tokens",
        "effective_from": "2025-08-05",
        "source_url": "https://console.groq.com/docs/model/openai/gpt-oss-120b",
        "notes": "Precios oficiales Groq. Cache read $0.075/1M (no usado).",
    },
    {
        "kind": "llm",
        "provider": "openrouter",
        "model": "meta-llama/llama-3.3-70b-instruct",
        "rate_input": 0.22,
        "rate_output": 0.50,
        "unit": "per_1m_tokens",
        "effective_from": "2026-10-04",
        "source_url": "https://openrouter.ai/meta-llama/llama-3.3-70b-instruct",
        "notes": (
            "La ficha de la página mostraba $0.10/$0.32; la API pública reporta "
            "$0.22/$0.50 efectivo 2026-10-04. Valor conservador. VERIFICAR."
        ),
    },
    {
        "kind": "llm",
        "provider": "gemini",
        "model": "gemini-3.6-flash",
        "rate_input": 0.75,
        "rate_output": 3.75,
        "unit": "per_1m_tokens",
        "effective_from": "2026-10-10",
        "source_url": "https://ai.google.dev/gemini-api/docs/pricing",
        "notes": (
            "Precio introductorio vigente hasta 2026-12-31; desde 2027-01-01 "
            "sube a $1.50/$7.50. Cache $0.075/1M (no usado). Capturada 2026-10-10."
        ),
    },
    {
        "kind": "whatsapp",
        "provider": "meta",
        "market": "rest_of_latam",
        "category": "service",
        "rate": 0.0113,
        "unit": "per_message",
        "effective_from": "2026-10-01",
        "source_url": (
            "https://developers.facebook.com/documentation/business-messaging/"
            "whatsapp/pricing/"
        ),
        "notes": (
            "Rest of Latin America. 1.000 mensajes de servicio gratis/mes por "
            "número de negocio. Rate card oficial (fetch directo 400; cifras "
            "republicadas). VERIFICAR contra CSV antes de uso comercial."
        ),
    },
    {
        "kind": "whatsapp",
        "provider": "meta",
        "market": "rest_of_latam",
        "category": "utility",
        "rate": 0.0113,
        "unit": "per_message",
        "effective_from": "2026-07-01",
        "source_url": (
            "https://developers.facebook.com/documentation/business-messaging/"
            "whatsapp/pricing/"
        ),
        "notes": "Rest of Latin America. Rate card oficial. VERIFICAR contra CSV.",
    },
    {
        "kind": "whatsapp",
        "provider": "meta",
        "market": "rest_of_latam",
        "category": "authentication",
        "rate": 0.0113,
        "unit": "per_message",
        "effective_from": "2026-07-01",
        "source_url": (
            "https://developers.facebook.com/documentation/business-messaging/"
            "whatsapp/pricing/"
        ),
        "notes": "Rest of Latin America. Rate card oficial. VERIFICAR contra CSV.",
    },
    {
        "kind": "whatsapp",
        "provider": "meta",
        "market": "rest_of_latam",
        "category": "marketing",
        "rate": 0.0740,
        "unit": "per_message",
        "effective_from": "2026-07-01",
        "source_url": (
            "https://developers.facebook.com/documentation/business-messaging/"
            "whatsapp/pricing/"
        ),
        "notes": "Rest of Latin America. Rate card oficial. VERIFICAR contra CSV.",
    },
]


def seed_default_tariffs() -> None:
    """Siembra las tarifas por defecto que falten (idempotente, insert-only)."""
    for tariff in DEFAULT_TARIFFS:
        metrics.upsert_tariff(**tariff)


def llm_cost_usd(
    prompt_tokens: int | None,
    completion_tokens: int | None,
    rate_input: float,
    rate_output: float,
) -> float | None:
    """Coste de una llamada al LLM en USD, o None si faltan tokens.

    Fórmula: prompt*rate_input/1e6 + completion*rate_output/1e6. Si falta
    cualquiera de los dos conteos devuelve None (no se estima a mano).
    """
    if prompt_tokens is None or completion_tokens is None:
        return None
    return (prompt_tokens * rate_input + completion_tokens * rate_output) / 1_000_000


def _pick_llm_tariff(
    tariffs: list[dict], provider: str | None, model: str | None, day: str
) -> tuple[dict | None, bool]:
    """Elige la tarifa LLM vigente para (provider, model) en `day` (YYYY-MM-DD).

    Devuelve (tarifa, asumida). `asumida=True` cuando no hay tarifa con
    effective_from <= day y se usa la más antigua conocida como aproximación.
    """
    if not provider or not model:
        return None, False
    candidates = [
        t for t in tariffs
        if t["kind"] == "llm" and t["provider"] == provider and t["model"] == model
    ]
    if not candidates:
        return None, False
    eligible = [t for t in candidates if (t["effective_from"] or "") <= day]
    if eligible:
        return max(eligible, key=lambda t: t["effective_from"] or ""), False
    return min(candidates, key=lambda t: t["effective_from"] or ""), True


def _pick_wa_tariff(
    tariffs: list[dict], market: str, category: str, day: str
) -> tuple[dict | None, bool]:
    """Tarifa de WhatsApp vigente para (market, category) en `day`.

    Devuelve (tarifa, asumida). `asumida=True` cuando no hay tarifa con
    effective_from <= day y se usa la más antigua conocida como aproximación
    (nunca se presenta como confirmada).
    """
    candidates = [
        t for t in tariffs
        if t["kind"] == "whatsapp"
        and t["market"] == market
        and (t["category"] or "").lower() == category
    ]
    if not candidates:
        return None, False
    eligible = [t for t in candidates if (t["effective_from"] or "") <= day]
    if eligible:
        return max(eligible, key=lambda t: t["effective_from"] or ""), False
    return min(candidates, key=lambda t: t["effective_from"] or ""), True


def _read_conn() -> sqlite3.Connection | None:
    """Conexión de SOLO LECTURA a la BD de métricas, o None si no existe."""
    path = metrics.METRICS_DB_PATH
    if not os.path.exists(path):
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _blank_tenant() -> dict:
    return {
        "ai_cost_usd": 0.0,
        "ai_events": 0,
        "ai_events_unknown_tokens": 0,
        "ai_events_unknown_tariff": 0,
        "ai_events_assumed_tariff": 0,
        "wa_cost_usd": 0.0,
        "wa_delivered": 0,
        "wa_failed": 0,
        "wa_free_delivered": 0,
        "wa_unknown_pricing": 0,
        "wa_unknown_rate": 0,
        "wa_assumed_rate": 0,
        "total_cost_usd": 0.0,
        "cost_partial": False,
        "revenue": None,
        "currency": "USD",
        "profit_usd": None,
        "margin_pct": None,
    }


def _cost_is_partial(row: dict) -> bool:
    """True si el coste del tenant no es definitivo (hay eventos excluidos o
    tarifas asumidas). En ese caso beneficio/margen no pueden determinarse."""
    return bool(
        row["ai_events_unknown_tokens"]
        or row["ai_events_unknown_tariff"]
        or row["ai_events_assumed_tariff"]
        or row["wa_unknown_pricing"]
        or row["wa_unknown_rate"]
        or row["wa_assumed_rate"]
    )


def build_report(
    period: str,
    *,
    tenant_id: str | None = None,
    market: str | None = None,
) -> dict:
    """Construye el informe de costes/rentabilidad de un período (YYYY-MM).

    No escribe nada: abre la BD en modo lectura. Si la BD no existe o no hay
    eventos, devuelve un informe vacío con la estructura esperada.
    """
    market = market or os.getenv("WHATSAPP_MARKET") or DEFAULT_WHATSAPP_MARKET
    tenants: dict[str, dict] = {}

    def tenant(t: str) -> dict:
        return tenants.setdefault(t or "desconocido", _blank_tenant())

    conn = _read_conn()
    if conn is None:
        return {
            "period": period,
            "market": market,
            "tenants": {},
            "totals": _totals({}, market),
            "db_missing": True,
        }
    try:
        tariffs = [dict(r) for r in conn.execute("SELECT * FROM tariffs")]
        _accumulate_ai(conn, period, tenant_id, tariffs, tenant)
        _accumulate_whatsapp(conn, period, tenant_id, market, tariffs, tenant)
        _accumulate_revenue(conn, period, tenant_id, tenant)
    finally:
        conn.close()

    for row in tenants.values():
        row["total_cost_usd"] = row["ai_cost_usd"] + row["wa_cost_usd"]
        row["cost_partial"] = _cost_is_partial(row)
        if row["revenue"] is not None:
            row["profit_usd"] = row["revenue"] - row["total_cost_usd"]
            row["margin_pct"] = (
                row["profit_usd"] / row["revenue"] * 100
                if row["revenue"] else None
            )
    return {
        "period": period,
        "market": market,
        "tenants": tenants,
        "totals": _totals(tenants, market),
        "db_missing": False,
    }


def _accumulate_ai(conn, period, tenant_id, tariffs, tenant) -> None:
    query = (
        "SELECT tenant_id, provider, model, prompt_tokens, completion_tokens, "
        "event_time FROM usage_events "
        "WHERE substr(event_time, 1, 7) = ? "
        "AND event_type IN ('chat_reply', 'quote_extraction')"
    )
    params: list = [period]
    if tenant_id:
        query += " AND tenant_id = ?"
        params.append(tenant_id)
    for event in conn.execute(query, params):
        row = tenant(event["tenant_id"])
        if event["prompt_tokens"] is None or event["completion_tokens"] is None:
            row["ai_events_unknown_tokens"] += 1
            continue
        day = (event["event_time"] or "")[:10]
        tariff, assumed = _pick_llm_tariff(
            tariffs, event["provider"], event["model"], day
        )
        if tariff is None:
            row["ai_events_unknown_tariff"] += 1
            continue
        rate_input = tariff["rate_input"]
        rate_output = tariff["rate_output"]
        if rate_input is None or rate_output is None:
            row["ai_events_unknown_tariff"] += 1
            continue
        cost = llm_cost_usd(
            event["prompt_tokens"],
            event["completion_tokens"],
            rate_input,
            rate_output,
        )
        if cost is None:
            row["ai_events_unknown_tokens"] += 1
            continue
        row["ai_cost_usd"] += cost
        row["ai_events"] += 1
        if assumed:
            row["ai_events_assumed_tariff"] += 1


def _accumulate_whatsapp(conn, period, tenant_id, market, tariffs, tenant) -> None:
    query = (
        "SELECT tenant_id, phone_number_id, status, delivered, failed, "
        "pricing_billable, pricing_category, event_time "
        "FROM whatsapp_message_status WHERE substr(event_time, 1, 7) = ?"
    )
    params: list = [period]
    if tenant_id:
        query += " AND tenant_id = ?"
        params.append(tenant_id)

    # Categorías facturables entregadas, agregadas por (tenant, phone, categoría);
    # y conteo de servicio por (tenant, phone) para el tier gratuito.
    billable: dict[tuple, int] = {}
    service_by_phone: dict[tuple, int] = {}
    day = f"{period}-01"

    for st in conn.execute(query, params):
        row = tenant(st["tenant_id"])
        failed = bool(st["failed"]) or st["status"] == "failed"
        if failed:
            row["wa_failed"] += 1
            continue
        delivered = bool(st["delivered"]) or st["status"] in ("delivered", "read")
        if not delivered:
            continue
        row["wa_delivered"] += 1
        if st["pricing_billable"] is None:
            row["wa_unknown_pricing"] += 1
            continue
        if not st["pricing_billable"]:
            row["wa_free_delivered"] += 1
            continue
        category = (st["pricing_category"] or "unknown").lower()
        key = (st["tenant_id"] or "desconocido", st["phone_number_id"] or "", category)
        billable[key] = billable.get(key, 0) + 1
        if category == "service":
            phone_key = (st["tenant_id"] or "desconocido", st["phone_number_id"] or "")
            service_by_phone[phone_key] = service_by_phone.get(phone_key, 0) + 1

    # Servicio: aplica el tier gratuito por número de negocio y luego tarifa.
    for (t, phone), total in service_by_phone.items():
        chargeable = max(0, total - FREE_WHATSAPP_SERVICE_PER_MONTH)
        if not chargeable:
            continue
        row = tenant(t)
        tariff, assumed = _pick_wa_tariff(tariffs, market, "service", day)
        if tariff is None or tariff["rate"] is None:
            row["wa_unknown_rate"] += chargeable
            continue
        row["wa_cost_usd"] += chargeable * tariff["rate"]
        if assumed:
            row["wa_assumed_rate"] += chargeable

    # Resto de categorías facturables (sin tier gratuito).
    for (t, _phone, category), count in billable.items():
        if category == "service":
            continue
        row = tenant(t)
        tariff, assumed = _pick_wa_tariff(tariffs, market, category, day)
        if tariff is None or tariff["rate"] is None:
            row["wa_unknown_rate"] += count
            continue
        row["wa_cost_usd"] += count * tariff["rate"]
        if assumed:
            row["wa_assumed_rate"] += count


def _accumulate_revenue(conn, period, tenant_id, tenant) -> None:
    query = "SELECT tenant_id, amount, currency FROM monthly_revenue WHERE period = ?"
    params: list = [period]
    if tenant_id:
        query += " AND tenant_id = ?"
        params.append(tenant_id)
    for rev in conn.execute(query, params):
        row = tenant(rev["tenant_id"])
        row["revenue"] = rev["amount"]
        row["currency"] = rev["currency"] or "USD"


def _totals(tenants: dict, market: str) -> dict:
    totals = {
        "ai_cost_usd": 0.0,
        "wa_cost_usd": 0.0,
        "total_cost_usd": 0.0,
        "ai_events": 0,
        "ai_events_unknown_tokens": 0,
        "ai_events_unknown_tariff": 0,
        "ai_events_assumed_tariff": 0,
        "wa_delivered": 0,
        "wa_failed": 0,
        "wa_free_delivered": 0,
        "wa_unknown_pricing": 0,
        "wa_unknown_rate": 0,
        "wa_assumed_rate": 0,
        "cost_partial": False,
        "revenue": 0.0,
        "cost_with_revenue_usd": 0.0,
        "profit_usd": None,
        "margin_pct": None,
        "currency": "USD",
        "market": market,
        "tenants_without_revenue": [],
    }
    if not tenants:
        return totals
    for name, row in sorted(tenants.items()):
        totals["ai_cost_usd"] += row["ai_cost_usd"]
        totals["wa_cost_usd"] += row["wa_cost_usd"]
        totals["ai_events"] += row["ai_events"]
        totals["ai_events_unknown_tokens"] += row["ai_events_unknown_tokens"]
        totals["ai_events_unknown_tariff"] += row["ai_events_unknown_tariff"]
        totals["ai_events_assumed_tariff"] += row["ai_events_assumed_tariff"]
        totals["wa_delivered"] += row["wa_delivered"]
        totals["wa_failed"] += row["wa_failed"]
        totals["wa_free_delivered"] += row["wa_free_delivered"]
        totals["wa_unknown_pricing"] += row["wa_unknown_pricing"]
        totals["wa_unknown_rate"] += row["wa_unknown_rate"]
        totals["wa_assumed_rate"] += row["wa_assumed_rate"]
        totals["cost_partial"] = totals["cost_partial"] or row["cost_partial"]
        if row["revenue"] is not None:
            totals["revenue"] += row["revenue"]
            totals["cost_with_revenue_usd"] += row["total_cost_usd"]
        else:
            totals["tenants_without_revenue"].append(name)
    totals["total_cost_usd"] = totals["ai_cost_usd"] + totals["wa_cost_usd"]
    if totals["revenue"]:
        totals["profit_usd"] = totals["revenue"] - totals["cost_with_revenue_usd"]
        totals["margin_pct"] = totals["profit_usd"] / totals["revenue"] * 100
    return totals


def format_report(report: dict) -> str:
    """Renderiza el informe como texto plano (para la terminal)."""
    period = report["period"]
    market = report["market"]
    lines = [f"Informe de costes y rentabilidad — período {period}", f"Mercado WhatsApp: {market}", ""]
    if report.get("db_missing"):
        lines.append("No hay base de métricas (metrics.sqlite3) todavía: sin datos.")
        return "\n".join(lines)

    tenants = report["tenants"]
    if not tenants:
        lines.append("Sin eventos ni ingresos para este período.")
        return "\n".join(lines)

    header = (
        f"{'tenant':<18}{'coste IA':>12}{'coste WA':>12}{'coste total':>14}"
        f"{'ingreso':>12}{'beneficio':>12}{'margen':>10}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for name, row in sorted(tenants.items()):
        revenue = f"{row['revenue']:.2f}" if row["revenue"] is not None else "—"
        profit = f"{row['profit_usd']:.2f}" if row["profit_usd"] is not None else "—"
        margin = f"{row['margin_pct']:.1f}%" if row["margin_pct"] is not None else "—"
        label = f"{name}*" if row["cost_partial"] else name
        lines.append(
            f"{label:<18}{row['ai_cost_usd']:>12.4f}{row['wa_cost_usd']:>12.4f}"
            f"{row['total_cost_usd']:>14.4f}{revenue:>12}{profit:>12}{margin:>10}"
        )
    t = report["totals"]
    lines.append("-" * len(header))
    profit = f"{t['profit_usd']:.2f}" if t["profit_usd"] is not None else "—"
    margin = f"{t['margin_pct']:.1f}%" if t["margin_pct"] is not None else "—"
    total_label = f"{'TOTAL (con ingreso)*':<18}" if t["cost_partial"] else f"{'TOTAL (con ingreso)':<18}"
    lines.append(
        f"{total_label}{t['ai_cost_usd']:>12.4f}"
        f"{t['wa_cost_usd']:>12.4f}{t['total_cost_usd']:>14.4f}"
        f"{t['revenue']:>12.2f}{profit:>12}{margin:>10}"
    )
    lines.append("")
    lines.append("Notas:")
    lines.append(
        f"  - IA: {t['ai_events']} eventos con coste; "
        f"{t['ai_events_unknown_tokens']} sin tokens, "
        f"{t['ai_events_unknown_tariff']} sin tarifa (excluidos); "
        f"{t['ai_events_assumed_tariff']} con tarifa asumida "
        "(la más antigua conocida; provisional)."
    )
    lines.append(
        f"  - WhatsApp: {t['wa_delivered']} entregados, {t['wa_failed']} fallidos, "
        f"{t['wa_free_delivered']} gratis (tier 1.000 servicio/número/mes); "
        f"{t['wa_unknown_pricing']} sin pricing (excluidos), "
        f"{t['wa_unknown_rate']} sin tarifa (excluidos), "
        f"{t['wa_assumed_rate']} con tarifa asumida (provisional)."
    )
    if t["tenants_without_revenue"]:
        lines.append(
            "  - Sin ingreso configurado (no se calcula margen): "
            + ", ".join(t["tenants_without_revenue"])
        )
    if t["cost_partial"]:
        lines.append(
            "  - * El coste es PARCIAL: hay eventos sin tokens/tarifa o con tarifa "
            "asumida; beneficio y margen NO son definitivos."
        )
    lines.append(
        "  - El beneficio/margen global considera solo tenants con ingreso; "
        f"coste de esos: {t['cost_with_revenue_usd']:.4f} USD."
    )
    return "\n".join(lines)
