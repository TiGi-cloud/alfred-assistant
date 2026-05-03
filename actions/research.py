"""
/research — deep research via N parallel Claude API calls.

  1. Decompose the user's topic into N sub-questions (1 API call)
  2. Run N parallel Claude calls, each researching one sub-question
  3. Synthesise the results into a final report (1 API call)

Defaults to 15 parallel agents on Haiku for the research pass + Sonnet for
the synthesis. Total cost: a few cents per /research; warn the user that
this is significantly more expensive than a normal turn.

Requires ANTHROPIC_API_KEY in the environment and `pip install anthropic`.
"""
from __future__ import annotations

import asyncio
import logging
import os

from kernel.runner import Context

logger = logging.getLogger("alfred.actions.research")


_DEFAULT_AGENT_MODEL = "claude-haiku-4-5-20251001"
_DEFAULT_SYNTH_MODEL = "claude-sonnet-4-6"
_DEFAULT_NUM_AGENTS = 15

_DECOMPOSE_PROMPT = """Decompose this research topic into {n} concrete, non-overlapping sub-questions a researcher could investigate independently. Output ONLY a numbered list, one question per line, no preamble.

Topic: {topic}"""

_AGENT_PROMPT = """Research question: {question}

Original topic for context: {topic}

Write a focused, factual brief (max ~400 words) that directly addresses this question. Cite concrete facts, numbers, and named sources when you can. Avoid filler. If you don't know, say so."""

_SYNTH_PROMPT = """You are synthesising the findings from {n} parallel research agents into a single comprehensive report.

Topic: {topic}

Agent briefs (one per question):

{briefs}

Write a coherent report that:
  • Opens with a 2-3 sentence executive summary
  • Has clear section headings
  • Integrates the findings (don't just list them)
  • Notes contradictions / gaps where they exist
  • Is at most 1500 words"""


def _import_anthropic():
    try:
        import anthropic  # type: ignore[import]
        return anthropic
    except ImportError as e:
        raise RuntimeError(
            "Research requires the Anthropic SDK. Install with:\n"
            "  pip install anthropic"
        ) from e


async def _decompose(client, topic: str, n: int, model: str) -> list[str]:
    resp = await client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": _DECOMPOSE_PROMPT.format(n=n, topic=topic)}],
    )
    text = resp.content[0].text
    questions: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Strip "1.", "1)", "•" prefixes
        for prefix in (".", ")"):
            if len(line) > 2 and line[0].isdigit() and line[line.find(prefix) + 1:].strip():
                line = line[line.find(prefix) + 1:].strip()
                break
        if line.startswith(("•", "-", "*")):
            line = line[1:].strip()
        if line:
            questions.append(line)
    return questions[:n]


async def _agent(client, topic: str, question: str, model: str) -> str:
    try:
        resp = await client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": _AGENT_PROMPT.format(
                topic=topic, question=question
            )}],
        )
        return resp.content[0].text
    except Exception as e:
        logger.exception("research agent failed: %s", question[:60])
        return f"(agent failed: {e})"


async def _synthesize(client, topic: str, briefs: list[str], model: str) -> str:
    body = "\n\n---\n\n".join(briefs)
    resp = await client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": _SYNTH_PROMPT.format(
            topic=topic, n=len(briefs), briefs=body,
        )}],
    )
    return resp.content[0].text


async def cmd_research(ctx: Context) -> None:
    """Run N parallel Claude agents on a topic and synthesise a report."""
    msg = ctx.message
    topic = (msg.command_args or "").strip() if msg else ""
    if not topic:
        await ctx.reply(
            "/research <topic>\n\n"
            "Decomposes into 15 sub-questions, runs them in parallel, then "
            "synthesises a report. Costs roughly $0.05-$0.20 per call.\n\n"
            "Example: /research the economic impact of LLMs on white-collar work"
        )
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        await ctx.reply(
            "❌ ANTHROPIC_API_KEY not set in .env.\n"
            "Get a key from https://console.anthropic.com/settings/keys"
        )
        return

    try:
        anthropic = _import_anthropic()
    except RuntimeError as e:
        await ctx.reply(str(e))
        return

    n_agents = _DEFAULT_NUM_AGENTS
    agent_model = os.environ.get("RESEARCH_AGENT_MODEL", _DEFAULT_AGENT_MODEL)
    synth_model = os.environ.get("RESEARCH_SYNTH_MODEL", _DEFAULT_SYNTH_MODEL)

    progress = await ctx.adapter.send_text(
        ctx.chat_id,
        f"🔬 Research started\n  topic: {topic}\n  decomposing into {n_agents} questions…",
    )

    client = anthropic.AsyncAnthropic(api_key=api_key)
    try:
        questions = await _decompose(client, topic, n_agents, agent_model)
    except Exception as e:
        await ctx.adapter.edit_text(progress, f"❌ decompose failed: {e}")
        return
    if len(questions) < 2:
        await ctx.adapter.edit_text(progress, "❌ couldn't decompose topic")
        return

    try:
        await ctx.adapter.edit_text(
            progress,
            f"🔬 Research\n  topic: {topic}\n  ✓ decomposed into {len(questions)} questions\n  running {len(questions)} agents in parallel…",
        )
    except Exception:
        pass

    briefs = await asyncio.gather(*(
        _agent(client, topic, q, agent_model) for q in questions
    ))

    try:
        await ctx.adapter.edit_text(
            progress,
            f"🔬 Research\n  topic: {topic}\n  ✓ {len(questions)} agents finished\n  synthesising final report…",
        )
    except Exception:
        pass

    try:
        report = await _synthesize(client, topic, briefs, synth_model)
    except Exception as e:
        await ctx.adapter.edit_text(progress, f"❌ synthesis failed: {e}")
        return

    # Write the full report to a file too — chat platforms have message size limits
    import tempfile
    fd, path = tempfile.mkstemp(prefix="alfred-research-", suffix=".md")
    os.close(fd)
    with open(path, "w") as f:
        f.write(f"# {topic}\n\n")
        f.write(report)
        f.write("\n\n---\n\n## Agent briefs\n\n")
        for q, b in zip(questions, briefs):
            f.write(f"### {q}\n\n{b}\n\n")

    head = report[:3000]
    if len(report) > 3000:
        head += f"\n\n…(report continues — full markdown attached)"
    try:
        await ctx.adapter.edit_text(progress, head)
    except Exception:
        await ctx.adapter.send_text(ctx.chat_id, head)

    try:
        await ctx.adapter.send_document(
            ctx.chat_id, path, filename="research.md",
            caption=f"📄 full report on: {topic}",
        )
    except Exception:
        pass
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def register(dispatcher) -> None:
    dispatcher.command("research", cmd_research)
