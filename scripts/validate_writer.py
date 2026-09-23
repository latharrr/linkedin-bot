"""Writer quality gate.

    python scripts/validate_writer.py                       # WRITER_PROVIDER + its model from .env
    python scripts/validate_writer.py --provider nvidia --model nvidia/nemotron-3-super-120b-a12b
    python scripts/validate_writer.py --no-repair           # measure the de-AI pass alone
    python scripts/validate_writer.py --voice voice.json    # your real voice_profile JSON

Needs only the writer's API key (no Supabase / Telegram / Tavily): fixed briefs and
fixed research, so runs are comparable across models. For each of 3 briefs it
runs the real outline → draft A + B → de-AI pipeline (15 chat calls in total, well
under the free tier's ~40 req/min), then checks every draft mechanically against the prompt rules
(app/style_check.py) and the number check (app/verify.py), before and after
the de-AI pass, and after the number-repair step. Full drafts go to out/validation-<model>-<time>.md — read them:
the checks catch rule breaks, not flat writing.

Exit code 0 = every final draft passes the hard rules; 1 = at least one doesn't.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from pydantic import SecretStr, ValidationError  # noqa: E402
from pydantic_settings import BaseSettings, SettingsConfigDict  # noqa: E402

from app.groq import GroqClient  # noqa: E402
from app.nvidia import THINKING_OFF, NvidiaClient  # noqa: E402
from app.openrouter import (  # noqa: E402
    ModelScopeClient,
    OpenRouterClient,
    ZenMuxClient,
)
from app.pipeline import fit_shape, polish, repair_if_needed  # noqa: E402
from app.research import format_research, research_corpus  # noqa: E402
from app.style_check import StyleReport, check_draft  # noqa: E402
from app.timeutil import utcnow  # noqa: E402
from app.verify import unverified_numbers  # noqa: E402
from app.writer import Writer  # noqa: E402


class ValidateSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    writer_provider: str = "groq"
    number_repair: bool = True
    groq_api_key: SecretStr | None = None
    groq_chat_model: str = "openai/gpt-oss-120b"
    groq_reasoning_effort: str = "low"
    openrouter_api_key: SecretStr | None = None
    openrouter_chat_model: str = "dots-studio/dots-3-note-preview:free"
    modelscope_api_key: SecretStr | None = None
    modelscope_base_url: str = "https://api-inference.modelscope.ai/v1"
    modelscope_chat_model: str = "deepseek-ai/DeepSeek-V4-Pro"
    zenmux_api_key: SecretStr | None = None
    zenmux_chat_model: str = "dots-studio/dots3-note-prev"
    nvidia_api_key: SecretStr | None = None
    nvidia_chat_model: str = "deepseek-ai/deepseek-v4.1-flash"
    nvidia_max_tokens: int = 4096
    nvidia_disable_thinking: bool = True
    draft_temperature: float = 0.85
    llm_timeout: float = 180.0


SAMPLE_VOICE = {
    "current_role": "Founder of a small edtech startup, building AI tutors for college students",
    "avg_length_words": 210,
    "hook_archetypes": ["opens with a specific number", "opens with a concrete failure", "opens mid-scene"],
    "line_style": "one sentence per line, blank line between beats",
    "sentence_rhythm": "mostly short, one longer sentence per section",
    "emoji_use": "none",
    "hashtag_use": "2-3 lowercase-first hashtags at the very end",
    "cta_style": "ends with one direct question to peers",
    "vocab_notes": "plain words; avoids corporate jargon and hype",
    "tone": "direct, dry, candid",
}


def _r(title: str, url: str, content: str) -> dict[str, str]:
    return {"title": title, "url": url, "content": content}


CASES: list[dict] = [
    {
        "brief": "Why most student founders quit in their first year",
        "research": [
            _r("Startup Genome report", "https://example.org/genome", "90% of startups fail; 20% of new businesses close within the first year."),
            _r("Student founder survey 2026", "https://example.org/survey", "Among 1,200 student founders surveyed, 47% cited running out of time as the main reason they stopped."),
            _r("Campus incubator data", "https://example.org/incubator", "Teams with a co-founder were 2x more likely to reach a second funding round."),
        ],
    },
    {
        "brief": "AI agents are overhyped for small businesses right now",
        "research": [
            _r("SMB AI adoption study", "https://example.org/smb", "Only 18% of small businesses using AI agents reported measurable time savings after 6 months."),
            _r("Agent reliability benchmark", "https://example.org/bench", "Top agents completed 38 percent of multi-step office tasks end to end without human help."),
            _r("Cost analysis", "https://example.org/cost", "Average monthly spend on AI tooling for a 10-person business reached $1,400."),
        ],
    },
    {
        "brief": "What I learned shipping an edtech product to 10,000 students in India",
        "research": [
            _r("India edtech market", "https://example.org/india", "India's edtech market was valued at ₹25,000 crore, with 62% of users on mobile only."),
            _r("Retention benchmarks", "https://example.org/retention", "Median 30-day retention for learning apps is 11%."),
            _r("Pricing study", "https://example.org/pricing", "Students were 3x more likely to pay for courses priced under ₹500."),
        ],
    },
]


def _flags(report: StyleReport) -> str:
    return "; ".join(report.hard) or "clean"


async def run_case(writer: Writer, case: dict, voice: dict, repair: bool = True) -> list[dict]:
    research = {"results": case["research"]}
    facts = format_research(research)
    corpus = research_corpus(research, case["brief"])
    outline = await writer.outline(case["brief"], facts, "none", [], voice)
    raw_a, raw_b = await asyncio.gather(
        writer.draft("a", case["brief"], outline, facts, voice, []),
        writer.draft("b", case["brief"], outline, facts, voice, []),
    )
    deai_a, deai_b = await asyncio.gather(writer.deai(raw_a, facts, voice), writer.deai(raw_b, facts, voice))
    final_a, final_b = deai_a, deai_b
    if repair:  # the production steps after the de-AI pass: 7b number repair, 7d polish, 7c shape
        final_a, final_b = await asyncio.gather(
            repair_if_needed(writer, deai_a, facts, corpus), repair_if_needed(writer, deai_b, facts, corpus)
        )
        final_a, final_b = await asyncio.gather(polish(writer, final_a, corpus), polish(writer, final_b, corpus))
        hooks = [str(h) for h in outline.get("hooks") or []]
        final_a, final_b = await asyncio.gather(
            fit_shape(writer, final_a, corpus, hooks), fit_shape(writer, final_b, corpus, hooks)
        )
    rows = []
    for which, raw, deai, final in (("A", raw_a, deai_a, final_a), ("B", raw_b, deai_b, final_b)):
        rows.append(
            {
                "which": which,
                "raw": raw,
                "final": final,
                "raw_report": check_draft(raw),
                "deai_unverified": unverified_numbers(deai, corpus),
                "repaired": final != deai,
                "final_report": check_draft(final),
                "unverified": unverified_numbers(final, corpus),
                "template": outline["sub_template"],
            }
        )
    return rows


def render_markdown(model: str, results: list[tuple[dict, list[dict]]]) -> str:
    out = [f"# Writer validation — {model}", f"_{utcnow().isoformat(timespec='seconds')}_", ""]
    for case, rows in results:
        out.append(f"## Brief: {case['brief']}")
        for r in rows:
            fr = r["final_report"]
            out += [
                f"### Draft {r['which']} ({r['template']}) — {'PASS' if fr.passed and not r['unverified'] else 'FAIL'}",
                f"- Before de-AI: {_flags(r['raw_report'])}",
                f"- After de-AI: unverified numbers {', '.join(r['deai_unverified']) or 'none'}"
                + (" → number repair applied" if r["repaired"] else ""),
                f"- Final: {_flags(fr)}",
                f"- Warnings: {'; '.join(fr.soft) or 'none'}",
                f"- Unverified numbers: {', '.join(r['unverified']) or 'none'}",
                "",
                "**Final draft**",
                "",
                "```text",
                r["final"],
                "```",
                "",
                "<details><summary>Raw draft (before de-AI)</summary>",
                "",
                "```text",
                r["raw"],
                "```",
                "</details>",
                "",
            ]
    return "\n".join(out)


def build_chat(s: ValidateSettings, provider: str, http: httpx.AsyncClient) -> tuple[object, str]:
    if provider == "groq":
        if not s.groq_api_key:
            sys.exit("GROQ_API_KEY is not set. Add it to .env (or export it) and re-run.")
        client = GroqClient(
            s.groq_api_key.get_secret_value(), http, chat_timeout=s.llm_timeout, browse_timeout=s.llm_timeout,
            reasoning_effort=s.groq_reasoning_effort,
        )
        return client, s.groq_chat_model
    compat = {
        "openrouter": (s.openrouter_api_key, OpenRouterClient, {}, s.openrouter_chat_model),
        "modelscope": (s.modelscope_api_key, ModelScopeClient, {"base_url": s.modelscope_base_url}, s.modelscope_chat_model),
        "zenmux": (s.zenmux_api_key, ZenMuxClient, {}, s.zenmux_chat_model),
    }
    if provider in compat:
        key, cls, extra, model = compat[provider]
        if not key:
            sys.exit(f"{provider.upper()}_API_KEY is not set. Add it to .env (or export it) and re-run.")
        return cls(key.get_secret_value(), http, chat_timeout=s.llm_timeout, **extra), model
    if not s.nvidia_api_key:
        sys.exit("NVIDIA_API_KEY is not set. Add it to .env (or export it) and re-run.")
    client = NvidiaClient(
        s.nvidia_api_key.get_secret_value(), http, chat_timeout=s.llm_timeout, embed_timeout=30, image_timeout=30,
        chat_template_kwargs=THINKING_OFF if s.nvidia_disable_thinking else None,
    )
    return client, s.nvidia_chat_model


async def main(provider: str | None, model_override: str | None, voice_path: str | None, repair: bool | None) -> int:
    try:
        s = ValidateSettings()
    except ValidationError as exc:
        sys.exit(f"Bad settings: {exc}")
    provider = provider or s.writer_provider
    repair = s.number_repair if repair is None else repair
    voice = json.loads(Path(voice_path).read_text()) if voice_path else SAMPLE_VOICE
    async with httpx.AsyncClient() as http:
        chat, default_model = build_chat(s, provider, http)
        model = model_override or default_model
        writer = Writer(chat, model, s.draft_temperature, s.nvidia_max_tokens)  # type: ignore[arg-type]
        results = []
        for i, case in enumerate(CASES, 1):
            print(f"[{i}/{len(CASES)}] {case['brief']}", flush=True)
            results.append((case, await run_case(writer, case, voice, repair)))
    failed = 0
    label = f"{provider}:{model}" + ("" if repair else " (no repair)")
    print(f"\n{label}\n{'Draft':<7}{'Before de-AI':<30}{'After de-AI: unverified':<26}{'Final':<30}{'Words':>6}  Unverified")
    for i, (_case, rows) in enumerate(results, 1):
        for r in rows:
            fr = r["final_report"]
            ok = fr.passed and not r["unverified"]
            failed += not ok
            deai = (", ".join(r["deai_unverified"]) or "-") + (" →repaired" if r["repaired"] else "")
            print(
                f"{i}{r['which']:<6}{_flags(r['raw_report'])[:28]:<30}{deai[:24]:<26}{_flags(fr)[:28]:<30}"
                f"{fr.words:>6}  {', '.join(r['unverified']) or '-'}{'' if ok else '   ✗'}"
            )
    out = Path("out")
    out.mkdir(exist_ok=True)
    path = out / f"validation-{provider}-{model.replace('/', '_')}-{utcnow().strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text(render_markdown(label, results), encoding="utf-8")
    total = sum(len(rows) for _, rows in results)
    print(f"\n{total - failed}/{total} final drafts pass the hard rules. Full drafts: {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", choices=["groq", "openrouter", "modelscope", "zenmux", "nvidia"], help="override WRITER_PROVIDER for this run")
    parser.add_argument("--model", help="override the provider's chat model for this run")
    parser.add_argument("--voice", help="path to a voice_profile JSON (defaults to a built-in sample)")
    parser.add_argument("--no-repair", dest="repair", action="store_false", default=None, help="skip the number-repair step")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.provider, args.model, args.voice, args.repair)))
