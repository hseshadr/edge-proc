"""End-to-end EdgeProc demo: read ``catalog.json``, persist a saved index.

The README quickstart shows the one-line ``LocalVecRuntime.from_texts`` path. This
file is its explicit cousin — the registry wiring made fully visible — and the
producer step for ``run_loop.sh``: it reads ``catalog.json``, encodes each entry
with ``TextEncoder``, builds a FAISS index, and saves it to ``--out`` (default
``./catalog_idx``). ``run_loop.sh`` then publishes that directory, syncs it onto a
consumer dir, and routes a sample task against the synced cache — all using only
the shipped CLI verbs.

Run it directly with ``uv run python examples/quickstart.py`` (or as part of
``bash examples/run_loop.sh``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from shared_libs_python.vector_mgmt.core.types import IndexConfig, VectorEmbedding

from edgeproc.localvec.encoder import TextEncoder
from edgeproc.localvec.faiss_index import FaissVectorIndex

_INDEX_NAME = "catalog_idx"


async def build_and_save_index(catalog: dict[str, str], out: Path) -> None:
    """Encode ``catalog``, persist a ``FaissVectorIndex`` to ``out/``."""
    ids, texts = list(catalog), list(catalog.values())
    encoder = TextEncoder()
    index = FaissVectorIndex(_INDEX_NAME, IndexConfig(dimension=encoder.dim))
    vectors = encoder.encode_texts(texts)
    await index.insert(
        [
            VectorEmbedding(entity_id=entity_id, embedding=vector.tolist())
            for entity_id, vector in zip(ids, vectors, strict=True)
        ]
    )
    out.mkdir(parents=True, exist_ok=True)
    index.save(out)


def _parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    here = Path(__file__).resolve().parent
    parser.add_argument("--catalog", type=Path, default=here / "catalog.json")
    parser.add_argument("--out", type=Path, default=here / _INDEX_NAME)
    return parser.parse_args()


def main() -> None:
    args = _parse()
    catalog: dict[str, str] = json.loads(args.catalog.read_text())
    asyncio.run(build_and_save_index(catalog, args.out))
    print(f"saved {len(catalog)}-doc index to {args.out}")


if __name__ == "__main__":
    main()
