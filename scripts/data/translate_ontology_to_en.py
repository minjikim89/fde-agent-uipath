"""One-time: translate the UI-surfaced ontology string fields to English.

Global hackathon → no Korean may surface in the demo. The mitigation text shown as
"Recommended Mitigation" comes from the ontology YAML (mitigation_options.*.action),
not an LLM, so it can't be fixed by a runtime prompt. This script translates the
Korean/mixed values in the UI-surfaced fields ONCE (Vertex Gemini), preserving
technical terms/IDs/code tokens, and rewrites those lines in place — structure,
keys, comments, and all non-surfaced content are byte-preserved.

Surfaced fields (see serve/app.py _node_why / _pick_mitigation / _frameworks_controls):
  action, relevance, note, description

    GOOGLE_CLOUD_PROJECT=fde-agent GOOGLE_CLOUD_LOCATION=global \
    .venv-adk/bin/python scripts/data/translate_ontology_to_en.py [--apply]
Without --apply it's a dry-run (counts + shows sample translations).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

YAML = Path(__file__).resolve().parent / "mapping-ontology-v0.1.yaml"
FIELDS = ("action", "relevance", "note", "description")
_HANGUL = re.compile(r"[가-힣]")
# key: value  (value may be quoted). Capture indent, key, raw value.
_LINE = re.compile(r'^(?P<indent>\s*)(?P<key>%s):\s*(?P<val>.+?)\s*$' % "|".join(FIELDS))


def _strip_quotes(v: str):
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1], v[0]
    return v, None


def _translate_batch(strings: list[str]) -> list[str]:
    from google import genai
    client = genai.Client(vertexai=True, location="global")
    prompt = (
        "Translate each string in this JSON array from Korean (or Korean-English mixed) "
        "into fluent, professional English for an AI-risk consulting tool.\n"
        "STRICT RULES:\n"
        "- Preserve ALL technical terms, acronyms and IDs EXACTLY: OWASP, LLM01-LLM10, "
        "MITRE ATLAS, AML.*, NIST, EU AI Act, K-PIPA, JSON, PII, RAG, HITL, API, OCR, "
        "ACS, KCB, NICE, code tokens, numbers, file paths, and any English already present.\n"
        "- Keep the meaning and technical precision identical; do NOT add or drop content.\n"
        "- Return ONLY a JSON array of the SAME length and order, no prose.\n\n"
        + json.dumps(strings, ensure_ascii=False)
    )
    resp = client.models.generate_content(model="gemini-3.1-pro-preview", contents=prompt)
    text = resp.text.strip()
    if text.startswith("```"):
        text = text.split("```")[1].lstrip("json").strip()
    out = json.loads(text)
    if len(out) != len(strings):
        raise SystemExit(f"translation count mismatch: {len(out)} != {len(strings)}")
    return out


def main() -> int:
    apply = "--apply" in sys.argv
    lines = YAML.read_text(encoding="utf-8").splitlines(keepends=False)

    targets = []  # (line_idx, indent, key, inner_text, quote_char)
    for i, line in enumerate(lines):
        m = _LINE.match(line)
        if not m or not _HANGUL.search(m.group("val")):
            continue
        inner, q = _strip_quotes(m.group("val"))
        targets.append((i, m.group("indent"), m.group("key"), inner, q))

    print(f"surfaced fields {FIELDS}: {len(targets)} Korean-bearing lines")
    if not targets:
        return 0
    if not apply:
        print("[dry-run] sample (first 3):")
        for t in targets[:3]:
            print(f"  L{t[0]+1} {t[2]}: {t[3][:90]}")
        print("\n[dry-run] re-run with --apply to translate via Gemini + rewrite in place.")
        return 0

    translations = _translate_batch([t[3] for t in targets])
    for (i, indent, key, _inner, q), en in zip(targets, translations):
        en = en.replace("\n", " ").strip()
        qc = q or '"'                      # always quote (safe for ':' etc.)
        en_q = en.replace("\\", "\\\\").replace(qc, "\\" + qc) if qc == '"' else en
        lines[i] = f'{indent}{key}: {qc}{en_q}{qc}'

    out_text = "\n".join(lines) + "\n"
    # Verify it still parses + no Korean left in surfaced fields.
    import yaml
    yaml.safe_load(out_text)
    remaining = sum(1 for ln in out_text.splitlines() if _LINE.match(ln) and _HANGUL.search(ln))
    YAML.write_text(out_text, encoding="utf-8")
    print(f"✅ applied {len(targets)} translations. remaining Korean in surfaced fields: {remaining}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
