"""CLI de informe de costes y rentabilidad (Fase 3).

Uso:
    python cost_report.py --period 2026-10
    python cost_report.py --period 2026-10 --tenant chatbot
    python cost_report.py --period 2026-10 --market rest_of_latam --no-seed

Es de solo lectura sobre los eventos. Antes de calcular siembra las tarifas por
defecto que falten (salvo --no-seed); nunca pisa tarifas existentes.
"""

import argparse
import os
import re
import sys

import costs
import metrics


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Informe de costes y rentabilidad por período (YYYY-MM)."
    )
    parser.add_argument(
        "--period", required=True, help="Período en formato YYYY-MM."
    )
    parser.add_argument(
        "--tenant", default=None, help="Filtra por un tenant concreto."
    )
    parser.add_argument(
        "--market",
        default=None,
        help=(
            "Mercado de WhatsApp (por defecto WHATSAPP_MARKET o "
            f"'{costs.DEFAULT_WHATSAPP_MARKET}')."
        ),
    )
    parser.add_argument(
        "--db", default=None, help="Ruta alternativa a metrics.sqlite3."
    )
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="No siembra las tarifas por defecto antes de calcular.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if not re.fullmatch(r"\d{4}-\d{2}", args.period):
        print(f"Período inválido: {args.period!r}. Usa el formato YYYY-MM.")
        return 2

    if args.db:
        metrics.METRICS_DB_PATH = args.db

    if not os.path.exists(metrics.METRICS_DB_PATH):
        print(
            f"No existe la base de métricas ({metrics.METRICS_DB_PATH}): sin datos."
        )
        return 0

    if not args.no_seed:
        costs.seed_default_tariffs()

    report = costs.build_report(
        args.period, tenant_id=args.tenant, market=args.market
    )
    print(costs.format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
