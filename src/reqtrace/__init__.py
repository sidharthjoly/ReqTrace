"""ATS job aggregator — a search index over AU data/analytics roles pulled
straight from employers' ATS JSON feeds."""


def main() -> int:
    import asyncio

    from .run import main as _run
    return asyncio.run(_run())
