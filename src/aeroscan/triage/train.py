"""Fine-tune a small instruction model (LoRA) to triage rule alerts.

The model reads an alert's last 15 minutes of polls and answers
{"verdict": "real"|"routine", "cause": ...}. It is never graded on free text:
evaluation scores each allowed answer's likelihood, which gives a probability
directly comparable with the baseline (same metrics, same operating points).

    uv run --extra train python -m aeroscan.triage.train            # GPU, Qwen2.5-1.5B
    uv run --extra train python -m aeroscan.triage.train --smoke    # tiny CPU check
"""

from __future__ import annotations

import argparse
import json
import math
import random
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import torch
from peft import LoraConfig, PeftModel, get_peft_model
from transformers import AutoModelForCausalLM, AutoTokenizer

from .baseline import evaluate_scores
from .dataset import CAUSE_CLASSES, Example, load

SYSTEM = (
    "You triage alerts from a flight anomaly detector. Each alert comes with the "
    "aircraft's last 15 minutes of once-a-minute position reports: minutes before the "
    "alert, altitude, vertical speed, groundspeed, and heading change since the first "
    "report. Decide whether the alert reflects a real in-flight emergency or routine "
    "flying, and for real ones the most likely cause. Answer with JSON only."
)
VERDICT_PREFIX = '{"verdict": "'


def flight_of(e: Example) -> str:
    return e.id.rsplit("#", 1)[0]


def select_training(
    dev: list[Example], max_routine: int, val_frac: float, seed: int
) -> tuple[list[Example], list[Example]]:
    """All real alerts plus a source-stratified sample of routine ones, split by flight."""
    rng = random.Random(seed)
    flights = sorted({flight_of(e) for e in dev})
    rng.shuffle(flights)
    val_flights = set(flights[: int(len(flights) * val_frac)])
    real = [e for e in dev if e.label == "real"]
    by_source: dict[str, list[Example]] = defaultdict(list)
    for e in dev:
        if e.label == "routine":
            by_source[e.source].append(e)
    total = sum(len(v) for v in by_source.values())
    routine: list[Example] = []
    for exs in by_source.values():
        # Keep every example of small sources; sample the large one (Barcelona).
        k = min(len(exs), max(40, round(max_routine * len(exs) / total)))
        routine += rng.sample(exs, k)
    chosen = real + routine
    rng.shuffle(chosen)
    train = [e for e in chosen if flight_of(e) not in val_flights]
    val = [e for e in dev if flight_of(e) in val_flights]
    return train, val


def chat(tok: Any, prompt: str) -> str:
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
    return str(tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True))


def encode(tok: Any, e: Example) -> tuple[list[int], list[int]]:
    prefix = tok(chat(tok, e.prompt), add_special_tokens=False)["input_ids"]
    answer = tok(e.target + tok.eos_token, add_special_tokens=False)["input_ids"]
    # Loss only on the answer.
    return prefix + answer, [-100] * len(prefix) + answer


def batches(items: list[tuple[list[int], list[int]]], size: int, pad: int) -> Any:
    for i in range(0, len(items), size):
        chunk = items[i : i + size]
        n = max(len(x) for x, _ in chunk)
        ids = torch.full((len(chunk), n), pad)
        labels = torch.full((len(chunk), n), -100)
        mask = torch.zeros((len(chunk), n), dtype=torch.long)
        for j, (x, y) in enumerate(chunk):
            ids[j, : len(x)] = torch.tensor(x)
            labels[j, : len(y)] = torch.tensor(y)
            mask[j, : len(x)] = 1
        yield ids, labels, mask


@torch.no_grad()
def score_options(
    model: Any, tok: Any, context: str, options: list[str], device: str
) -> list[float]:
    """Total log-probability of each option string following `context`."""
    ctx = tok(context, add_special_tokens=False)["input_ids"]
    seqs = [ctx + tok(o, add_special_tokens=False)["input_ids"] for o in options]
    n = max(map(len, seqs))
    ids = torch.full((len(seqs), n), tok.pad_token_id)
    mask = torch.zeros_like(ids)
    for j, s in enumerate(seqs):
        ids[j, : len(s)] = torch.tensor(s)
        mask[j, : len(s)] = 1
    logits = model(input_ids=ids.to(device), attention_mask=mask.to(device)).logits.float()
    logp = torch.log_softmax(logits, dim=-1)
    out = []
    for j, s in enumerate(seqs):
        total = 0.0
        for pos in range(len(ctx), len(s)):
            total += float(logp[j, pos - 1, s[pos]])
        out.append(total)
    return out


def predict(
    model: Any, tok: Any, examples: list[Example], device: str
) -> tuple[np.ndarray, list[str]]:
    model.eval()
    p_real, causes = [], []
    for e in examples:
        base = chat(tok, e.prompt) + VERDICT_PREFIX
        lp = score_options(model, tok, base, ['real"', 'routine"'], device)
        p_real.append(1 / (1 + math.exp(lp[1] - lp[0])))
        ctx = base + 'real", "cause": "'
        cs = score_options(model, tok, ctx, [c + '"' for c in CAUSE_CLASSES], device)
        causes.append(CAUSE_CLASSES[int(np.argmax(cs))])
    return np.array(p_real), causes


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--model", default="Qwen/Qwen2.5-1.5B-Instruct")
    p.add_argument("--data", type=Path, default=Path("data/triage"))
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--epochs", type=int, default=3)
    p.add_argument("--lr", type=float, default=2e-4)
    p.add_argument("--batch", type=int, default=4, help="examples per forward pass")
    p.add_argument("--grad-accum", type=int, default=2, help="passes per optimiser step")
    p.add_argument("--lora-r", type=int, default=16)
    p.add_argument("--max-routine", type=int, default=900)
    p.add_argument("--val-frac", type=float, default=0.15)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--smoke", action="store_true", help="tiny model, few examples, CPU-friendly")
    args = p.parse_args(argv)

    if args.smoke:
        args.model = "HuggingFaceTB/SmolLM2-135M-Instruct"
        args.epochs = 1
        args.batch, args.grad_accum = 2, 4
    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if device == "cuda" else torch.float32
    torch.manual_seed(args.seed)
    out = args.out or Path("runs") / ("smoke" if args.smoke else args.model.split("/")[-1].lower())
    out.mkdir(parents=True, exist_ok=True)

    dev, test = load(args.data / "dev.jsonl"), load(args.data / "test.jsonl")
    train, val = select_training(dev, args.max_routine, args.val_frac, args.seed)
    if args.smoke:
        rng = random.Random(0)
        train = rng.sample(train, 48)
        val = rng.sample(val, 16)
        test = rng.sample([e for e in test if e.label == "real"], 8) + rng.sample(
            [e for e in test if e.label == "routine"], 24
        )
    print(f"device={device} model={args.model} train={len(train)} val={len(val)} test={len(test)}")

    tok = AutoTokenizer.from_pretrained(args.model)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    model: Any = AutoModelForCausalLM.from_pretrained(args.model, dtype=dtype)
    model = model.to(device)
    # Recompute activations in the backward pass: much less memory for a little more time.
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    model = get_peft_model(
        model,
        LoraConfig(
            r=args.lora_r,
            lora_alpha=2 * args.lora_r,
            lora_dropout=0.05,
            target_modules=[
                "q_proj",
                "k_proj",
                "v_proj",
                "o_proj",
                "gate_proj",
                "up_proj",
                "down_proj",
            ],
            task_type="CAUSAL_LM",
        ),
    )
    model.print_trainable_parameters()

    encoded = [encode(tok, e) for e in train]
    steps = args.epochs * math.ceil(len(encoded) / (args.batch * args.grad_accum))
    opt = torch.optim.AdamW((q for q in model.parameters() if q.requires_grad), lr=args.lr)
    sched = torch.optim.lr_scheduler.LambdaLR(
        opt, lambda s: min(1.0, (s + 1) / max(1, steps // 10)) * max(0.0, 1 - s / steps)
    )
    step, t0 = 0, time.time()
    for epoch in range(args.epochs):
        model.train()
        opt.zero_grad()  # drop any leftover partial accumulation from the previous epoch
        random.Random(args.seed + epoch).shuffle(encoded)
        for k, (ids, labels, mask) in enumerate(batches(encoded, args.batch, tok.pad_token_id)):
            loss = model(
                input_ids=ids.to(device), attention_mask=mask.to(device), labels=labels.to(device)
            ).loss
            (loss / args.grad_accum).backward()
            if (k + 1) % args.grad_accum:
                continue
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            opt.zero_grad()
            step += 1
            if step % 10 == 0 or step == steps:
                print(
                    f"epoch {epoch + 1} step {step}/{steps} loss {loss.item():.4f} "
                    f"({time.time() - t0:.0f}s)",
                    flush=True,
                )
        vp, _ = predict(model, tok, val, device)
        vy = np.array([e.label == "real" for e in val])
        if vy.any() and not vy.all():
            from sklearn.metrics import roc_auc_score

            print(f"epoch {epoch + 1} val roc_auc {roc_auc_score(vy, vp):.3f}", flush=True)

    model.save_pretrained(str(out / "adapter"))
    tok.save_pretrained(str(out / "adapter"))

    p_real, causes = predict(model, tok, test, device)
    report: dict[str, Any] = {
        "model": args.model,
        "train_examples": len(train),
        "verdict": evaluate_scores(test, p_real),
    }
    real_idx = [i for i, e in enumerate(test) if e.label == "real"]
    truth = [test[i].cause for i in real_idx]
    pred = [causes[i] for i in real_idx]
    known = [k for k, c in enumerate(truth) if c != "unknown"]
    report["cause"] = {
        "accuracy": round(float(np.mean([a == b for a, b in zip(pred, truth, strict=True)])), 3),
        "accuracy_known_causes": round(float(np.mean([pred[k] == truth[k] for k in known])), 3)
        if known
        else None,
    }
    (out / "report.json").write_text(json.dumps(report, indent=2))
    with (out / "test_scores.jsonl").open("w") as f:
        for e, pr, c in zip(test, p_real, causes, strict=True):
            f.write(
                json.dumps(
                    {
                        "id": e.id,
                        "source": e.source,
                        "label": e.label,
                        "p_real": float(pr),
                        "cause": c,
                    }
                )
                + "\n"
            )
    print(json.dumps(report, indent=2))


def load_adapter(model_name: str, adapter: Path) -> tuple[Any, Any]:
    tok = AutoTokenizer.from_pretrained(adapter)
    base = AutoModelForCausalLM.from_pretrained(model_name)
    return PeftModel.from_pretrained(base, adapter), tok


if __name__ == "__main__":
    main()
