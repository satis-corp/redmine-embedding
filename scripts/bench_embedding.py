#!/usr/bin/env python3
"""Benchmark Ruri v3 embedding time / peak memory on a synthetic Japanese corpus.

Each model runs in its own subprocess so ru_maxrss (peak RSS) is per model.

    UV_PROJECT_ENVIRONMENT=/private/tmp/claude-501/bench-venv \
        uv run --extra batch python scripts/bench_embedding.py --chunks 256

The corpus is fully deterministic (seeded) and contains no real Redmine data.
"""

from __future__ import annotations

import argparse
import json
import random
import resource
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_MODELS = ["cl-nagoya/ruri-v3-130m", "cl-nagoya/ruri-v3-310m"]
RESULT_MARK = "@@RESULT@@"

_SUBJECTS = ["ログイン画面", "チケット一覧", "バッチ処理", "検索API", "添付ファイルのアップロード",
             "通知メール", "ガントチャート", "ユーザー権限設定", "データ移行スクリプト", "管理画面のエクスポート機能"]
_SYMPTOMS = ["が突然表示されなくなりました", "で500エラーが発生します", "の応答が極端に遅くなっています",
             "が期待通りに動作しません", "で文字化けが発生しています", "がタイムアウトします",
             "の結果が前回と異なります", "でデータが重複して登録されます"]
_STEPS = ["ブラウザのキャッシュをクリアして再度アクセスする", "プロジェクトの設定画面で対象のトラッカーを有効化する",
          "管理者アカウントでログインし、ロールの権限を確認する", "サーバーを再起動してログを確認する",
          "対象のチケットを開いて、関連チケットを追加する", "CSVファイルをUTF-8で保存し直してインポートする",
          "cron の設定を見直し、実行ユーザーを変更する", "データベースのバックアップを取得してから再実行する"]
_CAUSES = ["原因はプラグインとのバージョン不整合と思われます。", "タイムゾーンの設定が環境ごとに異なっていたことが原因でした。",
           "インデックスが不足しており、検索時にフルスキャンが走っていました。",
           "メモリ不足によりプロセスが強制終了されていた可能性があります。",
           "文字コードの指定漏れが原因で、一部の文字が正しく保存されていませんでした。"]
_ENGLISH = ["ActiveRecord::StatementInvalid: PG::UndefinedColumn: ERROR: column issues.foo does not exist",
            "ConnectionError: HTTPSConnectionPool(host='redmine.example.com', port=443): Max retries exceeded",
            "NoMethodError (undefined method `name' for nil:NilClass)",
            "UnicodeDecodeError: 'utf-8' codec can't decode byte 0x83 in position 12: invalid start byte",
            "OperationalError: (2006, 'MySQL server has gone away')"]
_TRACE = ["Traceback (most recent call last):",
          '  File "/opt/app/batch/run.py", line {n}, in main',
          "    result = process(items)",
          '  File "/opt/app/batch/process.py", line {m}, in process',
          "    raise RuntimeError(\"failed to process item\")",
          "RuntimeError: failed to process item"]


def _paragraph(rng: random.Random) -> str:
    kind = rng.random()
    if kind < 0.35:
        return f"{rng.choice(_SUBJECTS)}{rng.choice(_SYMPTOMS)}。{rng.choice(_CAUSES)}"
    if kind < 0.60:
        steps = "\n".join(f"{i + 1}. {rng.choice(_STEPS)}" for i in range(rng.randint(2, 4)))
        return f"再現手順は以下の通りです。\n{steps}"
    if kind < 0.80:
        return f"以下のエラーメッセージが出力されました。\n{rng.choice(_ENGLISH)}\n{rng.choice(_CAUSES)}"
    if kind < 0.90:
        tr = "\n".join(t.format(n=rng.randint(10, 400), m=rng.randint(10, 400)) for t in _TRACE)
        return f"ログは次の通りです。\n{tr}"
    return (f"{rng.choice(_SUBJECTS)}の件、{rng.choice(_STEPS)}ことで改善しました。"
            f"念のためバージョン {rng.randint(3, 6)}.{rng.randint(0, 9)}.{rng.randint(0, 9)} でも確認をお願いします。")


def build_corpus(n: int, chunk_chars: int, seed: int = 20261028) -> list[str]:
    """n chunks with lengths spread over ~30%..100% of chunk_chars (deterministic)."""
    rng = random.Random(seed)
    out: list[str] = []
    for _ in range(n):
        target = int(chunk_chars * rng.uniform(0.3, 1.0))
        text = ""
        while len(text) < target:
            text += ("\n\n" if text else "") + _paragraph(rng)
        out.append(text[:chunk_chars])
    return out


def _peak_rss_mb() -> float:
    v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux reports kilobytes.
    return v / (1024 * 1024) if sys.platform == "darwin" else v / 1024


def worker(args: argparse.Namespace) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from redmine_embedding.common.embedding.ruri import RuriV3Embedder

    texts = build_corpus(args.chunks, args.chunk_chars)
    t0 = time.perf_counter()
    emb = RuriV3Embedder(args.worker, device=args.device, batch_size=args.batch_size)
    load_s = time.perf_counter() - t0
    emb.embed_documents(texts[: min(8, len(texts))])  # warm-up, not timed
    t0 = time.perf_counter()
    vecs = emb.embed_documents(texts)
    embed_s = time.perf_counter() - t0
    assert len(vecs) == len(texts)
    avg = sum(map(len, texts)) / len(texts)
    print(RESULT_MARK + json.dumps({
        "model": args.worker, "dim": emb.dimension, "load_s": load_s, "embed_s": embed_s,
        "chunks": len(texts), "avg_chars": avg, "peak_rss_mb": _peak_rss_mb(),
    }))


def fmt_dur(sec: float) -> str:
    return f"{sec / 60:.1f}分" if sec < 7200 else f"{sec / 3600:.1f}時間"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", action="append", help="repeatable; default: ruri-v3-130m and 310m")
    ap.add_argument("--chunks", type=int, default=256)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--chunk-chars", type=int, action="append",
                    help="max chars per chunk; repeatable (e.g. 500 1000 2000); default 1000")
    ap.add_argument("--worker", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.worker:
        args.chunk_chars = args.chunk_chars[0]
        worker(args)
        return

    models = args.model or DEFAULT_MODELS
    sizes = args.chunk_chars or [1000]
    rows = []
    for cc in sizes:
        for m in models:
            cmd = [sys.executable, __file__, "--worker", m, "--chunks", str(args.chunks),
                   "--batch-size", str(args.batch_size), "--device", args.device, "--chunk-chars", str(cc)]
            print(f"[run] {m} chunk_chars={cc}", file=sys.stderr, flush=True)
            p = subprocess.run(cmd, capture_output=True, text=True)
            line = next((l for l in p.stdout.splitlines() if l.startswith(RESULT_MARK)), None)
            if p.returncode != 0 or line is None:
                sys.stderr.write(p.stderr[-3000:])
                sys.exit(f"worker failed for {m} (exit {p.returncode})")
            r = json.loads(line[len(RESULT_MARK):])
            r["chunk_chars"] = cc
            rows.append(r)

    print(f"\nchunks={args.chunks} batch_size={args.batch_size} device={args.device}\n")
    print("| model | max chars | avg chars | dim | load (s) | embed (s) | chunks/s | ms/chunk | peak RSS (MB) |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        cps = r["chunks"] / r["embed_s"]
        print(f"| {r['model']} | {r['chunk_chars']} | {r['avg_chars']:.0f} | {r['dim']} | {r['load_s']:.1f} | "
              f"{r['embed_s']:.2f} | {cps:.1f} | {1000 / cps:.1f} | {r['peak_rss_mb']:.0f} |")
    print()
    for r in rows:
        cps = r["chunks"] / r["embed_s"]
        print(f"- extrapolation {r['model']} (max {r['chunk_chars']} chars): "
              f"10k chunks = {fmt_dur(10_000 / cps)}, 100k chunks = {fmt_dur(100_000 / cps)}")


if __name__ == "__main__":
    main()
