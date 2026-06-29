"""
generate_chunks.py
==================
One-time preprocessing script.

Reads data/jobs.csv  →  GPT-4o-mini extracts a clean, signal-rich chunk per job
                      →  saves to data/jobs_chunks.csv

Why LLM chunking instead of regex?
  - Job descriptions are unstructured prose with wildly different formats.
  - LLM understands context: it knows "5+ years of Java" is a requirement,
    not a company description, even without a section header.
  - Regex only works on structure. LLM works on meaning.

Cost estimate (500 jobs):
  - Input:  ~500 × 1 200 tokens  = 600 K tokens  @ $0.15/1M = $0.09
  - Output: ~500 ×   150 tokens  =  75 K tokens  @ $0.60/1M = $0.05
  - Total: ~$0.14 for the entire dataset

Run once, commit jobs_chunks.csv, never pay again.

Usage:
    cd meetup
    source venv/bin/activate
    python scripts/generate_chunks.py
"""

import os
import sys
import time
import json
import logging
from pathlib import Path

import pandas as pd
from openai import OpenAI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("generate_chunks")

# ── Paths ──────────────────────────────────────────────────────────────────
ROOT       = Path(__file__).parent.parent
INPUT_CSV  = ROOT / "data" / "jobs.csv"
OUTPUT_CSV = ROOT / "data" / "jobs_chunks.csv"

# ── Config ─────────────────────────────────────────────────────────────────
MODEL        = "gpt-4o-mini"
BATCH_SIZE   = 10          # jobs per log line
RATE_LIMIT_PAUSE = 0.2     # seconds between calls (avoids 429s)
RESUME_FROM  = True        # if output file exists, skip already-processed IDs

# ── Prompt ─────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """\
You are a technical recruiter assistant. Your job is to extract a clean,
concise indexing chunk from a raw job description.

The chunk will be embedded for semantic search — it must contain only
information that helps match a candidate's skills to this role.

Extract and include:
  - Role title and seniority level
  - Company name and domain/industry
  - Required technical skills and tools (languages, frameworks, platforms)
  - Years of experience required
  - Key responsibilities (1-2 lines, specific not generic)
  - Preferred/bonus qualifications if meaningful

Exclude completely:
  - Company boilerplate ("We are a global leader in…")
  - EEO / diversity statements
  - Legal text, agency notes, fake-job warnings
  - Benefits, perks, pay ranges
  - Generic phrases ("strong communication skills", "team player")

Output format: plain prose, no bullet points, no headers, no JSON.
Keep it under 200 words. Be dense and specific.
"""

USER_TEMPLATE = """\
Job Title: {title}
Company: {company}
Location: {location}

Description:
{description}
"""


def extract_chunk(client: OpenAI, row: dict) -> str:
    desc = str(row.get("job_description") or "").strip()
    if not desc:
        # No description — build minimal chunk from structured fields
        return (
            f"{row.get('job_title', '')} at {row.get('company_name', '')}. "
            f"Location: {row.get('location', '')}. Skills: {row.get('skills', '')}."
        ).strip()

    user_msg = USER_TEMPLATE.format(
        title       = row.get("job_title",    ""),
        company     = row.get("company_name", ""),
        location    = row.get("location",     ""),
        description = desc[:4000],           # cap input — enough context for any job
    )

    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
        temperature=0,
        max_tokens=300,
    )
    return resp.choices[0].message.content.strip()


def main():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        log.error("OPENAI_API_KEY not set")
        sys.exit(1)

    client = OpenAI(api_key=api_key)

    df = pd.read_csv(INPUT_CSV)
    log.info("Loaded %d jobs from %s", len(df), INPUT_CSV)

    # Filter to active jobs only
    active = df[df["is_active"] == True].copy().reset_index(drop=True)
    log.info("%d active jobs to process", len(active))

    # Resume support — skip already processed IDs
    done_ids: set = set()
    existing_rows: list = []
    if RESUME_FROM and OUTPUT_CSV.exists():
        existing = pd.read_csv(OUTPUT_CSV)
        done_ids = set(existing["id"].astype(str).tolist())
        existing_rows = existing.to_dict("records")
        log.info("Resuming — %d already done, %d remaining",
                 len(done_ids), len(active) - len(done_ids))

    to_process = active[~active["id"].astype(str).isin(done_ids)]
    log.info("Processing %d jobs…", len(to_process))

    results = list(existing_rows)
    errors  = 0

    for i, (_, row) in enumerate(to_process.iterrows(), 1):
        row = dict(row)
        job_id = str(row["id"])

        try:
            chunk = extract_chunk(client, row)
            results.append({
                "id"         : job_id,
                "job_title"  : row.get("job_title",    ""),
                "company_name": row.get("company_name", ""),
                "location"   : row.get("location",     ""),
                "salary"     : row.get("salary",       ""),
                "apply_link" : row.get("apply_link",   ""),
                "skills"     : row.get("skills",       ""),
                "is_active"  : row.get("is_active",    True),
                "chunk_text" : chunk,
            })

            if i % BATCH_SIZE == 0 or i == len(to_process):
                log.info("  %d/%d  (errors: %d)  last: %s @ %s",
                         i, len(to_process), errors,
                         row.get("job_title", "")[:40],
                         row.get("company_name", ""))
                # Save checkpoint every batch
                pd.DataFrame(results).to_csv(OUTPUT_CSV, index=False)

        except Exception as exc:
            log.warning("  [%s] Error: %s — using fallback chunk", job_id, exc)
            results.append({
                "id"         : job_id,
                "job_title"  : row.get("job_title",    ""),
                "company_name": row.get("company_name", ""),
                "location"   : row.get("location",     ""),
                "salary"     : row.get("salary",       ""),
                "apply_link" : row.get("apply_link",   ""),
                "skills"     : row.get("skills",       ""),
                "is_active"  : row.get("is_active",    True),
                "chunk_text" : f"{row.get('job_title','')} at {row.get('company_name','')}.",
            })
            errors += 1

        time.sleep(RATE_LIMIT_PAUSE)

    # Final save
    output_df = pd.DataFrame(results)
    output_df.to_csv(OUTPUT_CSV, index=False)

    log.info("Done. %d chunks saved to %s  (errors: %d)", len(output_df), OUTPUT_CSV, errors)

    # Print a sample chunk
    sample = output_df[output_df["chunk_text"].str.len() > 50].iloc[0]
    log.info("\n--- Sample chunk ---\n%s\n%s\n---",
             f"{sample['job_title']} @ {sample['company_name']}", sample["chunk_text"])


if __name__ == "__main__":
    main()
